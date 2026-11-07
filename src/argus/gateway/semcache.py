"""Q&A semantic cache.

A question is embedded and compared with earlier questions asked in the same scope:
tenant, repository, indexed commit and the requester's role. Above ``threshold``
cosine similarity, the earlier answer is reused. The scope keeps answers from crossing
permissions, and a new push (new commit) naturally invalidates them.
"""

import json
import uuid
from typing import Any

import asyncpg

from argus.rag.embed import Embedder, to_pgvector


class QACache:
    def __init__(self, pool: asyncpg.Pool, embedder: Embedder, threshold: float = 0.95) -> None:
        self.pool = pool
        self.embedder = embedder
        self.threshold = threshold

    async def _vec(self, question: str) -> str:
        [v] = await self.embedder.embed([question.strip().lower()])
        return to_pgvector(v)

    async def lookup(
        self, *, tenant_id: uuid.UUID, repo_id: uuid.UUID, commit: str, role: str, question: str
    ) -> dict[str, Any] | None:
        vec = await self._vec(question)
        row = await self.pool.fetchrow(
            """
            SELECT id, result::text AS result, 1 - (embedding <=> $5::vector) AS sim
            FROM qa_cache
            WHERE tenant_id = $1 AND repo_id = $2 AND commit_sha = $3 AND role_scope = $4
            ORDER BY embedding <=> $5::vector LIMIT 1
            """,
            tenant_id,
            repo_id,
            commit,
            role,
            vec,
        )
        if row is None or row["sim"] < self.threshold:
            return None
        await self.pool.execute("UPDATE qa_cache SET hits = hits + 1 WHERE id = $1", row["id"])
        result: dict[str, Any] = json.loads(row["result"])
        return {**result, "cache_hit": True, "cache_similarity": round(float(row["sim"]), 4)}

    async def store(
        self,
        *,
        tenant_id: uuid.UUID,
        repo_id: uuid.UUID,
        commit: str,
        role: str,
        question: str,
        result: dict[str, Any],
        run_id: uuid.UUID,
    ) -> None:
        await self.pool.execute(
            """
            INSERT INTO qa_cache (tenant_id, repo_id, commit_sha, role_scope, question,
                                  embedding, result, source_run)
            VALUES ($1, $2, $3, $4, $5, $6::vector, $7::jsonb, $8)
            """,
            tenant_id,
            repo_id,
            commit,
            role,
            question,
            await self._vec(question),
            json.dumps(result),
            run_id,
        )
