"""Online evaluation: a sample of production reviews is scored by the LLM judge.

After a review succeeds, ``maybe_sample`` (rate ``online_eval_rate``) records an
``eval_samples`` row and enqueues ``jobs:eval`` in one transaction. The eval worker
scores the posted review with the judge (no expected issues online, so the judge rates
signal versus noise) and stores the score next to the prompt version, so a prompt or
model change shows up in production scores as well as in the offline gate.
"""

import json
import random
import uuid

import asyncpg

from argus.evals.judge import judge
from argus.llm.client import ModelSource
from argus.queue.consumer import PermanentError
from argus.queue.streams import Message
from argus.queue.topics import JOBS_EVAL


async def maybe_sample(
    pool: asyncpg.Pool,
    *,
    run_id: uuid.UUID,
    tenant_id: uuid.UUID,
    agent: str,
    prompt_version: str,
    rate: float,
    rng: random.Random | None = None,
) -> bool:
    if rate <= 0 or (rng or random).random() >= rate:
        return False
    async with pool.acquire() as conn, conn.transaction():
        inserted = await conn.fetchval(
            """
            INSERT INTO eval_samples (run_id, tenant_id, agent, prompt_version)
            VALUES ($1, $2, $3, $4) ON CONFLICT (run_id) DO NOTHING RETURNING true
            """,
            run_id,
            tenant_id,
            agent,
            prompt_version,
        )
        if inserted:
            await conn.execute(
                "INSERT INTO outbox (topic, payload) VALUES ($1, $2::jsonb)",
                JOBS_EVAL,
                json.dumps({"run_id": str(run_id)}),
            )
    return bool(inserted)


async def handle_eval_message(pool: asyncpg.Pool, llm: ModelSource, msg: Message) -> None:
    try:
        run_id = uuid.UUID(str(msg.json()["run_id"]))
    except (KeyError, ValueError) as exc:
        raise PermanentError(f"bad message: {exc}") from exc
    row = await pool.fetchrow(
        """
        SELECT s.status, s.tenant_id, r.result::text AS result FROM eval_samples s
        JOIN runs r ON r.id = s.run_id WHERE s.run_id = $1
        """,
        run_id,
    )
    if row is None or row["status"] != "pending":
        return
    review = json.loads(row["result"] or "{}").get("review", "")
    if not review:
        await pool.execute(
            "UPDATE eval_samples SET status = 'failed', reason = 'no review text'"
            " WHERE run_id = $1",
            run_id,
        )
        return
    score = await judge(llm.for_run(row["tenant_id"], run_id, "eval"), [], review)
    await pool.execute(
        """
        UPDATE eval_samples SET status = 'done', judge_score = $2, scored_at = now()
        WHERE run_id = $1 AND status = 'pending'
        """,
        run_id,
        score,
    )
