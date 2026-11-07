"""tree-sitter chunking: structure, call edges, stable hashes, skip rules."""

from argus.rag.chunker import MAX_LINES, chunk_file, search_text, should_index, split_identifier
from argus.rag.embed import hashing_vector

PY = '''import os
LIMIT = 3


@cache
class Store:
    """Key-value store."""

    ttl = 60

    def get(self, key):
        return fetch(key) or self.backend.lookup(key)

    async def put(self, key, value):
        await save(key, value)


def retry_with_backoff(fn, attempts=3):
    for _ in range(attempts):
        fn()
'''


def test_python_chunks_follow_structure() -> None:
    chunks = chunk_file("app/store.py", PY)
    got = [(c.kind, c.symbol, c.start_line, c.end_line) for c in chunks]
    assert got == [
        ("module", None, 1, 2),
        ("class", "Store", 5, 9),  # decorator included, header before first method
        ("method", "Store.get", 11, 12),
        ("method", "Store.put", 14, 15),
        ("function", "retry_with_backoff", 18, 20),
    ]
    assert [c.ordinal for c in chunks] == list(range(5))
    get = chunks[2]
    assert get.signature == "def get(self, key):"
    assert set(get.calls) == {("fetch", 12), ("lookup", 12)}


def test_typescript_and_go() -> None:
    ts = (
        'import { x } from "y";\n'
        "export class Api {\n  async get(id: string) {\n    return fetchIt(id);\n  }\n}\n"
        "export const handler = async (req) => new Api().get(req.id);\n"
        "export function helper() { return handler({}); }\n"
    )
    got = [(c.kind, c.symbol) for c in chunk_file("src/api.ts", ts)]
    assert got == [
        ("module", None),
        ("class", "Api"),
        ("method", "Api.get"),
        ("function", "handler"),
        ("function", "helper"),
    ]
    go = "package main\n\nfunc (s *S) Run() error { return doIt() }\n\nfunc main() { s.Run() }\n"
    chunks = chunk_file("cmd/main.go", go)
    assert [(c.kind, c.symbol) for c in chunks] == [
        ("module", None),
        ("function", "Run"),
        ("function", "main"),
    ]
    assert chunks[2].calls == (("Run", 5),)


def test_moving_code_keeps_hashes_editing_changes_one() -> None:
    before = {c.symbol: c.content_hash for c in chunk_file("app/store.py", PY)}
    moved = PY.replace("import os\n", "import os\nimport sys\n\n\n")
    after_move = {c.symbol: c.content_hash for c in chunk_file("app/store.py", moved)}
    assert {k: v for k, v in after_move.items() if k} == {k: v for k, v in before.items() if k}

    edited = PY.replace("await save(key, value)", "await save(key, value, ttl=self.ttl)")
    after_edit = {c.symbol: c.content_hash for c in chunk_file("app/store.py", edited)}
    changed = {k for k in before if before[k] != after_edit[k]}
    assert changed == {"Store.put"}


def test_long_functions_split_and_docs_window() -> None:
    body = "\n".join(f"    x{i} = {i}" for i in range(MAX_LINES * 2))
    chunks = chunk_file("big.py", f"def big():\n{body}\n")
    assert len(chunks) == 3
    assert all(c.symbol == "big" and c.end_line - c.start_line < MAX_LINES for c in chunks)
    assert chunks[0].signature == "def big():" and chunks[1].signature is None

    doc = "\n".join(f"line {i}" for i in range(100))
    windows = chunk_file("docs/guide.md", doc)
    assert [(c.start_line, c.end_line) for c in windows] == [(1, 40), (31, 70), (61, 100)]
    assert all(c.kind == "doc" for c in windows)


def test_skip_rules() -> None:
    assert not should_index("node_modules/lib/index.js")
    assert not should_index("web/package-lock.json")
    assert not should_index("logo.png")
    assert not should_index("app/huge.py", size=10_000_000)
    assert should_index("app/main.py") and should_index("README.md")
    assert chunk_file("bin.py", "abc\x00def") == []


def test_identifier_splitting_and_search_text() -> None:
    assert split_identifier("retryWithBackoff") == ["retry", "with", "backoff"]
    assert split_identifier("HTTPServer_v2") == ["http", "server", "v", "2"]
    [chunk] = [c for c in chunk_file("app/x.py", PY) if c.symbol == "retry_with_backoff"]
    text = search_text(chunk)
    assert "backoff" in text.split() and "app" in text.split()


def test_hashing_vectors_are_normalised_and_lexical() -> None:
    a = hashing_vector("def retry_with_backoff(fn)")
    b = hashing_vector("retryWithBackoff helper")
    c = hashing_vector("render the html template")
    assert abs(sum(v * v for v in a) - 1) < 1e-9

    def cos(x: list[float], y: list[float]) -> float:
        return sum(i * j for i, j in zip(x, y, strict=True))

    assert cos(a, b) > cos(a, c)
