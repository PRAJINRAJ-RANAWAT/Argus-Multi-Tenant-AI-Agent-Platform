"""Structure-aware chunking with tree-sitter.

Code files (Python, JavaScript, TypeScript/TSX, Go) are split along definitions:

* each function is one chunk; each method is its own chunk named ``Class.method``;
* a class contributes a header chunk (its lines before the first method);
* top-level code between definitions (imports, constants) becomes ``module`` chunks;
* anything longer than ``MAX_LINES`` is split into consecutive parts.

Call expressions inside a chunk are collected for the "find callers" graph. Docs and
unknown text files get overlapping line windows. A chunk's ``content_hash`` covers the
text that is embedded (path, symbol, content), not line numbers, so code that only
moved is not re-embedded.
"""

import hashlib
import posixpath
import re
from collections.abc import Callable
from dataclasses import dataclass, field

import tree_sitter_go
import tree_sitter_javascript
import tree_sitter_python
import tree_sitter_typescript
from tree_sitter import Language, Node, Parser

MAX_LINES = 120
DOC_WINDOW, DOC_OVERLAP = 40, 10
TEXT_WINDOW = 60
MAX_FILE_BYTES = 300_000

SKIP_DIRS = frozenset(
    {
        ".git",
        "node_modules",
        "vendor",
        "dist",
        "build",
        "__pycache__",
        ".venv",
        "venv",
        "third_party",
        ".next",
        "target",
        "coverage",
    }
)
SKIP_FILES = frozenset(
    {
        "package-lock.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "uv.lock",
        "poetry.lock",
        "Cargo.lock",
        "go.sum",
    }
)
DOC_EXT = frozenset({".md", ".rst", ".txt", ".adoc"})
TEXT_EXT = frozenset(
    {
        ".json",
        ".yaml",
        ".yml",
        ".toml",
        ".sql",
        ".sh",
        ".cfg",
        ".ini",
        ".html",
        ".css",
        ".java",
        ".kt",
        ".rb",
        ".rs",
        ".c",
        ".h",
        ".cpp",
        ".cs",
        ".php",
        ".swift",
        ".scala",
    }
)


@dataclass(frozen=True)
class LangSpec:
    language: Language
    functions: frozenset[str]
    classes: frozenset[str]
    methods: frozenset[str]
    calls: frozenset[str]
    wrappers: frozenset[str] = frozenset()  # decorated_definition, export_statement


def _ts(lang: object) -> Language:
    return Language(lang)


_JS_COMMON = dict(
    functions=frozenset({"function_declaration", "generator_function_declaration"}),
    classes=frozenset({"class_declaration", "abstract_class_declaration", "interface_declaration"}),
    methods=frozenset({"method_definition"}),
    calls=frozenset({"call_expression", "new_expression"}),
    wrappers=frozenset({"export_statement"}),
)

LANGS: dict[str, LangSpec] = {
    "python": LangSpec(
        language=_ts(tree_sitter_python.language()),
        functions=frozenset({"function_definition"}),
        classes=frozenset({"class_definition"}),
        methods=frozenset({"function_definition"}),
        calls=frozenset({"call"}),
        wrappers=frozenset({"decorated_definition"}),
    ),
    "javascript": LangSpec(language=_ts(tree_sitter_javascript.language()), **_JS_COMMON),
    "typescript": LangSpec(
        language=_ts(tree_sitter_typescript.language_typescript()),
        **_JS_COMMON,
    ),
    "tsx": LangSpec(language=_ts(tree_sitter_typescript.language_tsx()), **_JS_COMMON),
    "go": LangSpec(
        language=_ts(tree_sitter_go.language()),
        functions=frozenset({"function_declaration", "method_declaration"}),
        classes=frozenset(),
        methods=frozenset(),
        calls=frozenset({"call_expression"}),
    ),
}

EXT_LANG = {
    ".py": "python",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".mts": "typescript",
    ".tsx": "tsx",
    ".go": "go",
}


@dataclass(frozen=True)
class Chunk:
    path: str
    ordinal: int
    kind: str
    symbol: str | None
    signature: str | None
    language: str | None
    start_line: int  # 1-based, inclusive
    end_line: int
    content: str
    calls: tuple[tuple[str, int], ...] = ()  # (callee name, line)
    content_hash: str = field(default="")

    def embed_text(self) -> str:
        head = f"{self.path}" + (f" :: {self.symbol}" if self.symbol else "")
        return f"{head}\n{self.content}"


_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_CAMEL = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")


def split_identifier(name: str) -> list[str]:
    parts: list[str] = []
    for piece in name.split("_"):
        parts.extend(p.lower() for p in _CAMEL.findall(piece))
    return [p for p in parts if p]


def search_text(chunk: Chunk) -> str:
    """Content plus identifier sub-words and the path, for full-text search: a query for
    ``retry backoff`` matches ``retryWithBackoff`` and ``retry_with_backoff``."""
    idents = set(_IDENT.findall(chunk.content))
    if chunk.symbol:
        idents.update(_IDENT.findall(chunk.symbol))
    words = {w for i in idents for w in split_identifier(i) if len(w) > 1}
    path_words = re.split(r"[/._\-]+", chunk.path.lower())
    return " ".join(
        [chunk.content, " ".join(sorted(idents)), " ".join(sorted(words)), " ".join(path_words)]
    )


def language_for(path: str) -> str | None:
    return EXT_LANG.get(posixpath.splitext(path)[1].lower())


def should_index(path: str, size: int | None = None) -> bool:
    parts = path.split("/")
    if any(p in SKIP_DIRS for p in parts[:-1]) or parts[-1] in SKIP_FILES:
        return False
    if size is not None and size > MAX_FILE_BYTES:
        return False
    ext = posixpath.splitext(path)[1].lower()
    return ext in EXT_LANG or ext in DOC_EXT or ext in TEXT_EXT


def _hash(c: Chunk) -> Chunk:
    digest = hashlib.sha256(c.embed_text().encode()).hexdigest()
    return Chunk(**{**c.__dict__, "content_hash": digest})


def _windows(
    path: str,
    lines: list[str],
    kind: str,
    lang: str | None,
    size: int,
    overlap: int,
    start_ordinal: int = 0,
    symbol: str | None = None,
    offset: int = 0,
) -> list[Chunk]:
    out: list[Chunk] = []
    step = max(1, size - overlap)
    i = 0
    while i < len(lines):
        piece = lines[i : i + size]
        if any(line.strip() for line in piece):
            out.append(
                Chunk(
                    path=path,
                    ordinal=start_ordinal + len(out),
                    kind=kind,
                    symbol=symbol,
                    signature=None,
                    language=lang,
                    start_line=offset + i + 1,
                    end_line=offset + i + len(piece),
                    content="\n".join(piece),
                )
            )
        if i + size >= len(lines):
            break
        i += step
    return out


def _name(node: Node) -> str | None:
    n = node.child_by_field_name("name")
    if n is not None and n.text is not None:
        return n.text.decode(errors="replace")
    return None


def _calls(node: Node, spec: LangSpec) -> list[tuple[str, int]]:
    found: list[tuple[str, int]] = []
    stack = [node]
    while stack:
        n = stack.pop()
        if n.type in spec.calls:
            fn = n.child_by_field_name("function") or n.child_by_field_name("constructor")
            if fn is not None and fn.text is not None:
                text = fn.text.decode(errors="replace")
                name = re.split(r"[.\s]|::|->", text)[-1].strip("()")
                if _IDENT.fullmatch(name):
                    found.append((name, n.start_point[0] + 1))
        stack.extend(reversed(n.children))
    return found


class _Builder:
    def __init__(self, path: str, lang: str, spec: LangSpec, lines: list[str]) -> None:
        self.path, self.lang, self.spec, self.lines = path, lang, spec, lines
        self.chunks: list[Chunk] = []

    def emit(self, kind: str, symbol: str | None, start: int, end: int, node: Node | None) -> None:
        """Emit lines [start, end] (0-based inclusive), splitting long spans."""
        while start <= end and not self.lines[start].strip():
            start += 1
        while end >= start and not self.lines[end].strip():
            end -= 1
        body = self.lines[start : end + 1]
        if not any(ch.isalnum() for line in body for ch in line):
            return  # blank, or only braces/punctuation
        sig = body[0].strip()[:200] if symbol else None
        calls = tuple(_calls(node, self.spec)) if node is not None else ()
        if len(body) <= MAX_LINES:
            self.chunks.append(
                Chunk(
                    path=self.path,
                    ordinal=len(self.chunks),
                    kind=kind,
                    symbol=symbol,
                    signature=sig,
                    language=self.lang,
                    start_line=start + 1,
                    end_line=end + 1,
                    content="\n".join(body),
                    calls=calls,
                )
            )
            return
        parts = _windows(
            self.path,
            body,
            kind,
            self.lang,
            MAX_LINES,
            0,
            start_ordinal=len(self.chunks),
            symbol=symbol,
            offset=start,
        )
        for i, p in enumerate(parts):
            in_part = tuple(c for c in calls if p.start_line <= c[1] <= p.end_line)
            self.chunks.append(
                Chunk(**{**p.__dict__, "signature": sig if i == 0 else None, "calls": in_part})
            )

    def definition(self, node: Node) -> tuple[Node, Node] | None:
        """(outer node incl. decorators/export, definition node) or None."""
        inner = node
        while inner.type in self.spec.wrappers:
            nxt = inner.child_by_field_name("definition") or inner.child_by_field_name(
                "declaration"
            )
            if nxt is None:
                nxt = next(
                    (
                        c
                        for c in inner.named_children
                        if c.type in self.spec.functions | self.spec.classes
                    ),
                    None,
                )
            if nxt is None:
                return None
            inner = nxt
        if inner.type in self.spec.functions | self.spec.classes:
            return node, inner
        # JS/TS: const handler = () => {...} / function () {...}
        if inner.type in {"lexical_declaration", "variable_declaration"}:
            for d in inner.named_children:
                v = d.child_by_field_name("value")
                if v is not None and v.type in {
                    "arrow_function",
                    "function_expression",
                    "function",
                }:
                    return node, d
        return None

    def method_nodes(self, cls: Node) -> list[tuple[Node, Node]]:
        body = cls.child_by_field_name("body")
        if body is None:
            return []
        out = []
        for c in body.named_children:
            inner = c
            while inner.type in self.spec.wrappers:
                inner = inner.child_by_field_name("definition") or inner
                if inner is c:
                    break
            if inner.type in self.spec.methods or inner.type in self.spec.functions:
                out.append((c, inner))
        return out

    def build(self, root: Node) -> list[Chunk]:
        gap_start: int | None = None

        def flush_gap(until: int) -> None:
            nonlocal gap_start
            if gap_start is not None and until >= gap_start:
                self.emit("module", None, gap_start, until, None)
            gap_start = None

        for child in root.named_children:
            d = self.definition(child)
            start, end = child.start_point[0], child.end_point[0]
            if d is None:
                if gap_start is None:
                    gap_start = start
                continue
            flush_gap(start - 1)
            _, inner = d
            name = _name(inner) or "<anonymous>"
            if inner.type in self.spec.classes:
                methods = self.method_nodes(inner)
                header_end = (methods[0][0].start_point[0] - 1) if methods else end
                self.emit("class", name, start, header_end, None if methods else inner)
                for m_outer, m_inner in methods:
                    self.emit(
                        "method",
                        f"{name}.{_name(m_inner) or '<anonymous>'}",
                        m_outer.start_point[0],
                        m_outer.end_point[0],
                        m_inner,
                    )
                last = methods[-1][0].end_point[0] if methods else end
                if last < end:
                    self.emit("class", name, last + 1, end, None)
            else:
                self.emit("function", name, start, end, inner)
        if gap_start is not None:
            flush_gap(len(self.lines) - 1)
        return self.chunks


_PARSERS: dict[str, Parser] = {}


def _parser(lang: str) -> Parser:
    if lang not in _PARSERS:
        _PARSERS[lang] = Parser(LANGS[lang].language)
    return _PARSERS[lang]


def chunk_file(path: str, text: str) -> list[Chunk]:
    """Chunks for one file (empty for files we do not index)."""
    if "\x00" in text[:8000] or not should_index(path, len(text.encode())):
        return []
    lines = text.split("\n")
    ext = posixpath.splitext(path)[1].lower()
    lang = language_for(path)
    make: Callable[[], list[Chunk]]
    if lang is not None:

        def make() -> list[Chunk]:
            tree = _parser(lang).parse(text.encode())
            return _Builder(path, lang, LANGS[lang], lines).build(tree.root_node)
    elif ext in DOC_EXT:

        def make() -> list[Chunk]:
            return _windows(path, lines, "doc", None, DOC_WINDOW, DOC_OVERLAP)
    else:

        def make() -> list[Chunk]:
            return _windows(path, lines, "text", None, TEXT_WINDOW, 10)

    chunks = make()
    return [_hash(Chunk(**{**c.__dict__, "ordinal": i})) for i, c in enumerate(chunks)]
