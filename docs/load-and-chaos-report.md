# Load and chaos report

Every scenario from the SPEC's testing table, run against the mock LLM and the in-memory fake GitHub. Two kinds of evidence:

- **Live stack**: `make demo` (all services in Docker Compose on one laptop: Apple Silicon, Docker Desktop, single worker process with 20 concurrent runs, Postgres 16 + pgvector, Redis 7 with AOF). Run on 2026-11-07.
- **Automated**: pytest scenarios against real Postgres and Redis (testcontainers). They run in CI on every change, so these results are re-proven continuously.

Written incident-style: what we expected, what broke the first time, what we changed. The first-run failures are the useful part.

## Summary

| Scenario | How | Pass condition | Result |
|---|---|---|---|
| Webhook burst | k6, 300 signed webhooks/s for 60 s, live | Ingest p99 < 300 ms; queue drains; no lost events | **Pass.** 17,982 requests at 299.7/s, p50 1.3 ms, p95 28.8 ms, **p99 114.8 ms**, 0 failed. All 986 PR events reviewed, 0 lost |
| Duplicate storm | 10 PRs × 3 deliveries, shuffled, 3 workers | One review per (repo, PR, head SHA) | **Pass.** 10 reviews, exactly 1 comment each (`test_duplicate_storm_one_review_per_head_sha`) |
| Worker killed mid-run | Cancel the worker while the comment call is in flight | Resumes elsewhere; no duplicate comment | **Pass.** Reaper recovers; resume replays 3 steps without new LLM calls; 1 comment (M2) |
| Zombie worker | Expire a lease under a slow worker, let it finish | Fencing rejects its writes | **Pass.** `LeaseLostError` on start and finish of steps (M2) |
| Force-push churn | 5 pushes to one PR, 3 workers | Older runs superseded; one final review | **Pass after fix** (incident 1). Only the last SHA posts |
| Noisy neighbour | One tenant opens 100 PRs, two others 5 each, live | Other tenants' latency within SLO | **Pass after three fixes** (incidents 2–4). Quiet p95 **1.9 s** (FIFO 5.9 s), flood p95 5.7 s (FIFO 5.0 s) |
| Provider outage | Mock returns 500 for the primary model | Breaker opens, fallback used, recovery | **Pass.** 3 failures open the breaker, traffic moves to the fallback, a probe closes it after recovery (M5) |
| Rate-limit storm | Mock returns 429 with `Retry-After: 3` | Backoff honoured; budgets intact; no stampede | **Pass.** Waits ≥ 3 s, 3 attempts max, reservation released (M5) |
| Redis restart | Restart Redis during a 40-PR burst, live | Pending messages recovered; no stuck runs | **Pass after fix** (incident 5). 40/40 reviewed, 1 comment each |
| Redis data loss | `FLUSHALL` after publish | No lost events | **Pass after fix** (incident 6). Event reaper re-enqueues |
| Postgres failover | (stretch) | | Not run: single-node Postgres on one VPS (ADR 0002) |

## Webhook burst

```
k6 run deploy/load/k6/webhook_burst.js   (constant arrival rate 300/s, 60 s)
http_reqs ........ 17982   299.67/s
http_req_duration  avg=7.24ms p(50)=1.32ms p(95)=28.8ms p(99)=114.83ms max=477.97ms
http_req_failed .. 0.00%
```

98% of the webhooks were `issues` events (recorded, not acted on), 2% `pull_request` opened. Every PR event was reviewed: 986 events, 986 runs `succeeded`, 0 left `received`. The review backlog drained in about 90 s at mock-LLM speed. Ingest stays fast because it does only verify, dedupe and one transaction (event + outbox row); everything slow happens behind the queue.

## Noisy neighbour

One tenant opens 100 PRs; right after, two other tenants open 5 each. Latency is webhook received → review posted, mock LLM at 300 ms per call, one worker with 20 concurrent runs.

| | Quiet tenants p50 / p95 | Flood p50 / p95 |
|---|---|---|
| FIFO (one stream) | 5.9 s / 5.9 s | 2.9 s / 5.0 s |
| Fair, first run | 1.6 s / 2.4 s | 155.4 s / 231.5 s |
| Fair, after fixing the busy loop and making it work-conserving | 14.5 s / 15.3 s | 41.4 s / 67.9 s |
| **Fair, final (idle tenants pruned)** | **1.7 s / 1.9 s** | **4.1 s / 5.7 s** |

The quiet tenants are served 3× faster than FIFO while the flood is only 14% slower. The automated version (`test_noisy_neighbour_does_not_starve_other_tenants`, 200 vs 2×5 jobs) shows quiet p95 0.76 s → 0.06 s and requires the flood to finish within 2× its FIFO time. The real gap grows with the size of the flood: under FIFO, the quiet tenants' wait is proportional to the backlog in front of them; under fair scheduling it is about one run.

## Incidents

### 1. Force-push churn: an older push posted a review

**Expected:** five quick pushes to one PR end with one review, for the last SHA.
**What happened:** the run for the first SHA posted a review. Superseding compared *runs*, but a run row is only created when a worker picks up its message; the oldest push was processed first, when no newer run existed yet.
**Change:** supersede also checks for newer accepted `pull_request` events for the same PR (an expression index keeps it cheap). Now the older runs end `superseded` before posting.

### 2. Fair queue: tenant lanes created after the worker started stalled everything

**What happened (live only):** only 4 of 110 PRs were processed. Housekeeping (`XAUTOCLAIM`) on a lane the relay had just created raised `NOGROUP`, and the exception aborted every read loop. The in-process tests always created groups up front, so they never saw it. The event reaper re-enqueued the stuck events after 10 minutes, as designed.
**Change:** consumer groups are created lazily per lane before any operation; an error on one lane is logged and no longer stalls the others. Regression test added.

### 3. Fair queue: the flood took 231 s instead of 5 s

**What happened:** quiet tenants got faster, but the flood slowed 46×. Two causes. When every tenant was at its cap, `read()` returned immediately, so the consumer spun on Redis and starved the running reviews of the event loop. And the cap applied even when no one else was waiting, leaving 16 of 20 slots idle.
**Change:** when everyone is at capacity, the read waits for a slot to free. The scheduler is work-conserving: in a round where no other tenant has work, a capped tenant may borrow idle slots up to 4× its cap; a newly arriving tenant still gets its own slot on the next read.

### 4. Fair queue: ~60 idle tenant lanes made every read expensive

**What happened:** after the load test, the active-tenant set held about 60 lanes. Every read and every housekeeping pass touched all of them (several Redis calls each), so even the quiet tenants took 15 s.
**Change:** an atomic Lua check drops a tenant from the active set when its lane has no backlog, nothing in flight and nothing delayed. It cannot race the relay, which re-adds the tenant after every publish. The set went from ~60 to 1.

### 5. Redis restart: unacked messages waited a minute, with crash noise

**What happened:** all 40 reviews completed with one comment each, but 9 waited about 60 s: workers that finished a run while Redis was down failed to ack, the exception escaped the task ("Task exception was never retrieved"), and the messages sat pending until the visibility timeout reclaimed them.
**Change:** a failed ack or retry is caught and logged; the message stays pending for reclaim, which is safe because handlers are idempotent. Redis runs with AOF (`appendfsync everysec`), so at most about a second of acknowledgements can be lost.

### 6. Redis data loss: an accepted event could be lost

**What happened (test):** with the message flushed from Redis after the relay published it, the event stayed `received` forever: the outbox row was already marked published and no run existed for the reaper to find.
**Change:** an event reaper re-enqueues routed events still `received` after 10 minutes with no run and no pending outbox row. With AOF on this is rare, but it closes the "0 lost events" SLO for the case where Redis loses data.

### Also found during M9

- **Slot lease collisions:** two reads in the same millisecond produced the same slot id, so the second lease overwrote the first and the tenant cap was undercounted. Slot ids are now random.

## Autoscaling

`argus-autoscale` turns queue lag into a worker count with Little's law (needed concurrency = starts per second × run duration + in flight), scales up immediately and down only when clearly over-provisioned. On the VPS `--apply` runs `docker compose up --scale worker=N`; on Kubernetes the same signal maps to a KEDA ScaledObject on the queue-lag metric. Interactive work is protected first: indexing and online evals pause while reviews or Q&A wait longer than 60 s.

## Reproduce

```bash
make demo                                    # stack + observability + fake GitHub, seeded
docker run --rm -i --network host -e INGEST=http://localhost:8001 -e DURATION=60s \
  -v "$PWD/deploy/load/k6:/s" grafana/k6:0.54.0 run /s/webhook_burst.js
uv run pytest -q tests/test_scale.py -s     # fairness, chaos scenarios, autoscaling
```
