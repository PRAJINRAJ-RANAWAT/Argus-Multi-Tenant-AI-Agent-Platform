"""Incremental code indexing.

``index_paths`` (re)indexes a set of paths at one commit:

1. Fetch each file at the commit (missing → the file was removed: write a tombstone).
2. Chunk with tree-sitter; each chunk has a hash of its embedded text.
3. Look the hashes up in the per-tenant ``embeddings`` cache and embed only the misses.
   A one-line change therefore embeds one chunk, not the file or the repo.
4. Per file, in one transaction: upsert ``code_files`` guarded by the event's receipt
   time (an older push processed late never overwrites newer content; the upsert's row
   lock serialises concurrent writers of the same file), then replace its chunks and
   call references.

``full_index`` lists the tree, indexes every indexable path, and tombstones files that
are no longer in it.
"""

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime

import asyncpg

from argus.github.client import GitHubClient
from argus.rag.chunker import Chunk, chunk_file, language_for, search_text, should_index
from argus.rag.embed import Embedder, to_pgvector

log = logging.getLogger("argus.rag.indexer")


@dataclass(frozen=True)
class RepoRef:
    tenant_id: uuid.UUID
    repo_id: uuid.UUID
    installation_id: int
    full_name: str


@dataclass
class IndexStats:
    files_indexed: int = 0
    files_deleted: int = 0
    files_stale: int = 0
    chunks: int = 0
    embedded: int = 0
    reused: int = 0
    paths: list[str] = field(default_factory=list)

    def add(self, other: "IndexStats") -> None:
        for k in ("files_indexed", "files_deleted", "files_stale", "chunks", "embedded", "reused"):
            setattr(self, k, getattr(self, k) + getattr(other, k))
        self.paths.extend(other.paths)


class Indexer:
    def __init__(
        self,
        pool: asyncpg.Pool,
        github: GitHubClient,
        embedder: Embedder,
        *,
        embed_batch: int = 64,
        fetch_concurrency: int = 8,
    ) -> None:
        self.pool = pool
        self.github = github
        self.embedder = embedder
        self.embed_batch = embed_batch
        self.fetch_sem = asyncio.Semaphore(fetch_concurrency)

    async def _fetch(self, repo: RepoRef, path: str, sha: str) -> str | None:
        async with self.fetch_sem:
            return await self.github.get_file(repo.installation_id, repo.full_name, path, sha)

    async def _vectors(
        self, tenant_id: uuid.UUID, chunks: list[Chunk], stats: IndexStats
    ) -> dict[str, str]:
        """content_hash → pgvector text, embedding only hashes not in the cache."""
        wanted = {c.content_hash: c for c in chunks}
        if not wanted:
            return {}
        rows = await self.pool.fetch(
            """
            SELECT content_hash, embedding::text AS v FROM embeddings
            WHERE tenant_id = $1 AND model = $2 AND content_hash = ANY($3::text[])
            """,
            tenant_id,
            self.embedder.model,
            list(wanted),
        )
        found = {r["content_hash"]: r["v"] for r in rows}
        missing = [h for h in wanted if h not in found]
        stats.reused += len(wanted) - len(missing)
        for i in range(0, len(missing), self.embed_batch):
            batch = missing[i : i + self.embed_batch]
            vectors = await self.embedder.embed([wanted[h].embed_text() for h in batch])
            texts = [to_pgvector(v) for v in vectors]
            await self.pool.executemany(
                """
                INSERT INTO embeddings (tenant_id, model, content_hash, embedding)
                VALUES ($1, $2, $3, $4::vector) ON CONFLICT DO NOTHING
                """,
                [(tenant_id, self.embedder.model, h, t) for h, t in zip(batch, texts, strict=True)],
            )
            found.update(zip(batch, texts, strict=True))
            stats.embedded += len(batch)
        return found

    async def _write_file(
        self,
        repo: RepoRef,
        path: str,
        sha: str,
        received_at: datetime,
        chunks: list[Chunk] | None,  # None = deleted
        vectors: dict[str, str],
        stats: IndexStats,
    ) -> None:
        deleted = chunks is None
        async with self.pool.acquire() as conn, conn.transaction():
            won = await conn.fetchval(
                """
                INSERT INTO code_files (tenant_id, repo_id, path, commit_sha, language, deleted,
                                        source_received_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                ON CONFLICT (repo_id, path) DO UPDATE
                SET commit_sha = EXCLUDED.commit_sha, language = EXCLUDED.language,
                    deleted = EXCLUDED.deleted,
                    source_received_at = EXCLUDED.source_received_at, indexed_at = now()
                WHERE code_files.source_received_at <= EXCLUDED.source_received_at
                RETURNING true
                """,
                repo.tenant_id,
                repo.repo_id,
                path,
                sha,
                language_for(path),
                deleted,
                received_at,
            )
            if not won:
                stats.files_stale += 1
                return
            await conn.execute(
                "DELETE FROM code_chunks WHERE repo_id = $1 AND path = $2", repo.repo_id, path
            )
            await conn.execute(
                "DELETE FROM code_refs WHERE repo_id = $1 AND path = $2", repo.repo_id, path
            )
            if deleted:
                stats.files_deleted += 1
                return
            assert chunks is not None
            await conn.executemany(
                """
                INSERT INTO code_chunks (tenant_id, repo_id, path, ordinal, kind, symbol,
                    signature, language, start_line, end_line, commit_sha, content,
                    content_hash, search_text, embedding)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14,
                        $15::vector)
                """,
                [
                    (
                        repo.tenant_id,
                        repo.repo_id,
                        path,
                        c.ordinal,
                        c.kind,
                        c.symbol,
                        c.signature,
                        c.language,
                        c.start_line,
                        c.end_line,
                        sha,
                        c.content,
                        c.content_hash,
                        search_text(c),
                        vectors[c.content_hash],
                    )
                    for c in chunks
                ],
            )
            refs = [
                (repo.tenant_id, repo.repo_id, path, c.symbol, callee, line)
                for c in chunks
                for callee, line in c.calls
            ]
            if refs:
                await conn.executemany(
                    """
                    INSERT INTO code_refs (tenant_id, repo_id, path, caller_symbol, callee,
                                           line)
                    VALUES ($1, $2, $3, $4, $5, $6)
                    """,
                    refs,
                )
            stats.files_indexed += 1
            stats.chunks += len(chunks)

    async def index_paths(
        self, repo: RepoRef, sha: str, received_at: datetime, paths: list[str]
    ) -> IndexStats:
        candidates = sorted(set(paths))
        # Non-indexable paths only matter if an earlier version was indexed (remove it).
        skipped = [p for p in candidates if not should_index(p)]
        known: set[str] = set()
        if skipped:
            rows = await self.pool.fetch(
                "SELECT path FROM code_files WHERE repo_id = $1 AND NOT deleted"
                " AND path = ANY($2::text[])",
                repo.repo_id,
                skipped,
            )
            known = {r["path"] for r in rows}
        unique = [p for p in candidates if should_index(p) or p in known]
        stats = IndexStats(paths=unique)

        async def load(path: str) -> tuple[str, list[Chunk] | None]:
            if not should_index(path):
                return path, None
            text = await self._fetch(repo, path, sha)
            return path, None if text is None else chunk_file(path, text)

        loaded = await asyncio.gather(*(load(p) for p in unique))
        all_chunks = [c for _, cs in loaded if cs for c in cs]
        vectors = await self._vectors(repo.tenant_id, all_chunks, stats)
        for path, chunks in loaded:
            await self._write_file(repo, path, sha, received_at, chunks, vectors, stats)
        await self.pool.execute(
            """
            INSERT INTO repo_index_state (repo_id, tenant_id, last_commit) VALUES ($1, $2, $3)
            ON CONFLICT (repo_id) DO UPDATE SET last_commit = EXCLUDED.last_commit,
                                                updated_at = now()
            """,
            repo.repo_id,
            repo.tenant_id,
            sha,
        )
        log.info(
            "indexed",
            extra={
                "repo": repo.full_name,
                "sha": sha[:12],
                "files": stats.files_indexed,
                "deleted": stats.files_deleted,
                "stale": stats.files_stale,
                "embedded": stats.embedded,
                "reused": stats.reused,
            },
        )
        return stats

    async def full_index(
        self, repo: RepoRef, sha: str, received_at: datetime, default_branch: str | None = None
    ) -> IndexStats:
        tree = await self.github.get_tree(repo.installation_id, repo.full_name, sha)
        present = [t["path"] for t in tree if should_index(t["path"], t.get("size"))]
        gone = await self.pool.fetch(
            """
            SELECT path FROM code_files
            WHERE repo_id = $1 AND NOT deleted AND path <> ALL($2::text[])
            """,
            repo.repo_id,
            present,
        )
        stats = await self.index_paths(repo, sha, received_at, present)
        for row in gone:
            await self._write_file(repo, row["path"], sha, received_at, None, {}, stats)
        await self.pool.execute(
            """
            UPDATE repo_index_state SET last_full_index = now(),
                   default_branch = coalesce($2, default_branch)
            WHERE repo_id = $1
            """,
            repo.repo_id,
            default_branch,
        )
        return stats
