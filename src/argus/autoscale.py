"""Autoscaling signal from queue lag (Little's law).

Needed workers = arrival rate * time per job / jobs per worker. Arrival rate is
estimated from the backlog: drain what is waiting within ``target_drain_s`` plus what is
in flight. Scale up immediately, scale down slowly (hysteresis), within bounds. On one
VPS ``argus-autoscale --apply`` runs ``docker compose up --scale worker=N``; on
Kubernetes the same numbers map to a KEDA ScaledObject on the queue-lag metric.
"""

import argparse
import asyncio
import math
import subprocess
from dataclasses import dataclass

from redis.asyncio import Redis

from argus.queue.fair import FairQueue
from argus.queue.streams import StreamQueue


@dataclass(frozen=True)
class Policy:
    min_workers: int = 1
    max_workers: int = 20
    per_worker: int = 20  # concurrent runs one worker process holds (I/O bound)
    avg_job_s: float = 90.0
    target_drain_s: float = 120.0
    scale_down_ratio: float = 0.5  # only scale down below half the current need


def desired_workers(backlog: int, in_flight: int, current: int, p: Policy) -> int:
    rate = backlog / p.target_drain_s  # jobs/s we must start to drain in time
    concurrent = rate * p.avg_job_s + in_flight  # Little's law: L = λ·W
    need = max(p.min_workers, math.ceil(concurrent / p.per_worker))
    need = min(p.max_workers, need)
    if need >= current:
        return need
    # Scale down only when clearly over-provisioned, to avoid flapping.
    return need if need <= current * p.scale_down_ratio else current


GROUPS = {
    "jobs:review": "review-workers",
    "jobs:qa": "qa-workers",
    "jobs:index": "index-workers",
    "jobs:eval": "eval-workers",
}


async def measure(redis: Redis, fair: set[str]) -> tuple[int, int]:
    """(backlog waiting or delayed, messages in flight) across all job queues."""
    backlog = in_flight = 0
    for topic, group in GROUPS.items():
        q: StreamQueue | FairQueue = (
            FairQueue(redis, topic, group, "autoscale")
            if topic in fair
            else StreamQueue(redis, topic, group, "autoscale")
        )
        try:
            d = await q.depth()
        except Exception:  # noqa: S112 - the stream does not exist yet
            continue
        backlog += d["lag"] + d["delayed"]
        in_flight += d["pending"]
    return backlog, in_flight


def main() -> None:
    p = argparse.ArgumentParser(description="Recommend (or apply) a worker count from lag.")
    p.add_argument("--redis-url", required=True)
    p.add_argument("--current", type=int, default=1)
    p.add_argument("--apply", action="store_true", help="docker compose up --scale worker=N")
    args = p.parse_args()

    async def run() -> tuple[int, int]:
        redis = Redis.from_url(args.redis_url, decode_responses=True)
        try:
            return await measure(redis, {"jobs:review", "jobs:qa"})
        finally:
            await redis.aclose()

    backlog, in_flight = asyncio.run(run())
    n = desired_workers(backlog, in_flight, args.current, Policy())
    print(f"backlog={backlog} in_flight={in_flight} current={args.current} desired={n}")
    if args.apply and n != args.current:
        subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["docker", "compose", "up", "-d", "--no-recreate", "--scale", f"worker={n}"],  # noqa: S607
            check=True,
        )
