"""Hybrid code search: Postgres full-text + pgvector, fused with reciprocal rank fusion,
then re-ranked.

Tenant and repo filters are inside both SQL queries (filter first, then search), never
applied to results afterwards. The HNSW scan uses pgvector's iterative scan so a
selective filter still returns enough candidates.
"""

import re
import uuid
from dataclasses import dataclass, field
from typing import Protocol

import asyncpg

from argus.rag.chunker import split_identifier
from argus.rag.embed import Embedder, to_pgvector

RRF_K = 60
_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "do",
        "does",
        "for",
        "from",
        "how",
        "i",
        "in",
        "is",
        "it",
        "of",
        "on",
        "or",
        "the",
        "this",
        "to",
        "we",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "with",
        "you",
        "our",
        "your",
        "can",
        "should",
        "would",
        "there",
        "their",
        "that",
    }
)


@dataclass
class Hit:
    chunk_id: int
    repo: str
    path: str
    symbol: str | None
    kind: str
    start_line: int
    end_line: int
    commit_sha: str
    content: str
    score: float = 0.0
    sources: set[str] = field(default_factory=set)

    @property
    def citation(self) -> str:
        return f"{self.path}:{self.start_line}-{self.end_line}@{self.commit_sha[:12]}"

    @property
    def url(self) -> str:
        return (
            f"https://github.com/{self.repo}/blob/{self.commit_sha}/{self.path}"
            f"#L{self.start_line}-L{self.end_line}"
        )

    def to_dict(self, max_chars: int = 1500) -> dict[str, object]:
        return {
            "citation": self.citation,
            "url": self.url,
            "path": self.path,
            "symbol": self.symbol,
            "kind": self.kind,
            "lines": [self.start_line, self.end_line],
            "score": round(self.score, 5),
            "content": self.content[:max_chars],
        }


class Reranker(Protocol):
    async def rerank(self, query: str, hits: list[Hit]) -> list[Hit]: ...


def query_terms(query: str) -> list[str]:
    terms: set[str] = set()
    for w in _WORD.findall(query):
        low = w.lower()
        if low not in STOPWORDS:
            terms.add(low)
        terms.update(p for p in split_identifier(w) if p not in STOPWORDS and len(p) > 1)
    return sorted(terms)


class LexicalReranker:
    """Cheap re-ranker: boosts hits whose symbol or path contains query identifiers, and
    exact identifier matches in the code. An LLM or cross-encoder re-ranker plugs in
    behind the same interface once the LLM gateway exists (M5)."""

    async def rerank(self, query: str, hits: list[Hit]) -> list[Hit]:
        idents = {w for w in _WORD.findall(query) if len(w) > 2}
        terms = set(query_terms(query))
        for h in hits:
            bonus = 0.0
            sym = (h.symbol or "").lower()
            words = set(split_identifier(h.symbol or "")) | set(re.split(r"[/._\-]+", h.path))
            if any(i.lower() == sym.split(".")[-1] for i in idents):
                bonus += 0.05
            bonus += 0.01 * len(terms & words)
            if any(i in h.content for i in idents if not i.islower() or "_" in i):
                bonus += 0.01
            h.score += bonus
        return sorted(hits, key=lambda h: (-h.score, h.path, h.start_line))


async def hybrid_search(
    pool: asyncpg.Pool,
    embedder: Embedder,
    *,
    tenant_id: uuid.UUID,
    repo_ids: list[uuid.UUID],
    query: str,
    k: int = 8,
    candidates: int = 50,
    reranker: Reranker | None = None,
) -> list[Hit]:
    if not query.strip() or not repo_ids:
        return []
    hits: dict[int, Hit] = {}

    def add(rows: list[asyncpg.Record], source: str) -> None:
        for rank, r in enumerate(rows):
            h = hits.get(r["id"])
            if h is None:
                h = hits[r["id"]] = Hit(
                    chunk_id=r["id"],
                    repo=r["repo"],
                    path=r["path"],
                    symbol=r["symbol"],
                    kind=r["kind"],
                    start_line=r["start_line"],
                    end_line=r["end_line"],
                    commit_sha=r["commit_sha"],
                    content=r["content"],
                )
            h.score += 1.0 / (RRF_K + rank + 1)
            h.sources.add(source)

    terms = query_terms(query)
    if terms:
        tsquery = " | ".join(t.replace("'", "") for t in terms)
        rows = await pool.fetch(
            """
            SELECT c.id, r.full_name AS repo, c.path, c.symbol, c.kind, c.start_line, c.end_line,
                   c.commit_sha, c.content, ts_rank_cd(c.tsv, q) AS rank
            FROM code_chunks c JOIN repos r ON r.id = c.repo_id,
                 to_tsquery('simple', $3) q
            WHERE c.tenant_id = $1 AND c.repo_id = ANY($2::uuid[]) AND c.tsv @@ q
            ORDER BY rank DESC, c.id
            LIMIT $4
            """,
            tenant_id,
            repo_ids,
            tsquery,
            candidates,
        )
        add(rows, "fts")

    [vec] = await embedder.embed([query])
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SET LOCAL hnsw.ef_search = 100")
        await conn.execute("SET LOCAL hnsw.iterative_scan = relaxed_order")
        rows = await conn.fetch(
            """
            SELECT c.id, r.full_name AS repo, c.path, c.symbol, c.kind, c.start_line, c.end_line,
                   c.commit_sha, c.content
            FROM code_chunks c JOIN repos r ON r.id = c.repo_id
            WHERE c.tenant_id = $1 AND c.repo_id = ANY($2::uuid[])
            ORDER BY c.embedding <=> $3::vector
            LIMIT $4
            """,
            tenant_id,
            repo_ids,
            to_pgvector(vec),
            candidates,
        )
    add(rows, "vector")

    ranked = sorted(hits.values(), key=lambda h: (-h.score, h.path, h.start_line))
    if reranker is not None:
        ranked = await reranker.rerank(query, ranked)
    return ranked[:k]


@dataclass(frozen=True)
class CallSite:
    path: str
    caller_symbol: str | None
    line: int
    commit_sha: str


async def find_callers(
    pool: asyncpg.Pool,
    *,
    tenant_id: uuid.UUID,
    repo_id: uuid.UUID,
    symbol: str,
    limit: int = 50,
) -> list[CallSite]:
    """Call sites of ``symbol`` (``Class.method`` or a bare name) from the call graph."""
    name = symbol.split(".")[-1].strip()
    if not _WORD.fullmatch(name):
        return []
    rows = await pool.fetch(
        """
        SELECT r.path, r.caller_symbol, r.line, f.commit_sha
        FROM code_refs r JOIN code_files f ON f.repo_id = r.repo_id AND f.path = r.path
        WHERE r.tenant_id = $1 AND r.repo_id = $2 AND r.callee = $3
        ORDER BY r.path, r.line
        LIMIT $4
        """,
        tenant_id,
        repo_id,
        name,
        limit,
    )
    return [CallSite(r["path"], r["caller_symbol"], r["line"], r["commit_sha"]) for r in rows]
