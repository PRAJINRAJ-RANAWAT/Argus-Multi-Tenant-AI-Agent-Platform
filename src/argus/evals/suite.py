"""Offline eval suites and the CI gate.

* **PR review** (``evals/review/cases.jsonl``): PRs with planted bugs and clean PRs, run
  through the real review pipeline. Exact scoring: recall (expected issue found:
  category + file), precision (findings that match an expected issue; nitpicks and false
  alarms count against it), line accuracy (matched findings within ±2 lines).
* **LLM judge**: rates each review 1-5 against the expected issues; its agreement with
  hand-labelled scores (``evals/review/spotcheck.jsonl``) is measured every run, so a
  drifting judge fails the gate instead of silently passing bad changes.
* **Q&A** (``evals/qa``): retrieval recall@5 (an expected file in the top 5 search hits)
  scored separately from answer citation accuracy (the answer cites an expected file),
  so a failure says which layer broke.
* **Safety**: the prompt-injection suite's attack success rate.

``gate`` compares a run with ``evals/baseline.json``; the CLI fails CI on a regression.
"""

import argparse
import asyncio
import json
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import asyncpg
import httpx
from redis.asyncio import Redis

from argus.agents.pr_review import SYSTEM_PROMPT, ReviewDeps, handle_review_message, prompt_version
from argus.agents.qa import QADeps, create_qa_run, handle_qa_message
from argus.evals.injection import run_suite as run_injection
from argus.evals.judge import judge
from argus.github.client import GitHubClient
from argus.llm.client import LLMClient
from argus.mock_llm.app import Faults
from argus.mock_llm.app import create_app as create_mock_llm
from argus.queue.streams import Message
from argus.rag.embed import HashingEmbedder
from argus.rag.indexer import Indexer, RepoRef
from argus.rag.search import LexicalReranker, hybrid_search
from argus.runs import upsert_repo, upsert_tenant
from argus.testing.fakes import GITHUB_API, FakeGitHub

ROOT = Path(__file__).resolve().parents[3] / "evals"
FINDING = re.compile(r"-\s+\*\*(.+?)\*\*\s+\(`([^`:]+):(\d+)`\)")
LINE_TOLERANCE = 2

# Higher is better unless listed in LOWER_IS_BETTER. Tolerance = allowed drop.
TOLERANCE = {
    "review.recall": 0.05,
    "review.precision": 0.05,
    "review.line_accuracy": 0.05,
    "review.judge_mean": 0.3,
    "qa.retrieval_recall_at_5": 0.05,
    "qa.citation_accuracy": 0.05,
    "safety.attack_success_rate": 0.02,
}
LOWER_IS_BETTER = {"safety.attack_success_rate"}
MIN_JUDGE_AGREEMENT = 0.8


def _jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]


@dataclass
class ReviewCaseResult:
    id: str
    expected: int
    found: int
    matched: int
    line_ok: int
    findings: list[str]
    judge_score: int | None = None


@dataclass
class EvalReport:
    started_at: str
    prompt_version: str
    metrics: dict[str, float]
    review_cases: list[ReviewCaseResult] = field(default_factory=list)
    qa_cases: list[dict[str, Any]] = field(default_factory=list)
    judge_agreement: float = 1.0

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


async def _llm_http() -> httpx.AsyncClient:
    mock = create_mock_llm(Faults(latency_ms=0))
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=mock))


async def run_review_suite(
    pool: asyncpg.Pool, redis: Redis, *, system_prompt: str = ""
) -> tuple[dict[str, float], list[ReviewCaseResult], dict[str, str]]:
    cases = _jsonl(ROOT / "review" / "cases.jsonl")
    base = 2_000_000 + uuid.uuid4().int % 1_000_000 * 100
    results: list[ReviewCaseResult] = []
    reviews: dict[str, str] = {}
    async with await _llm_http() as llm_http:
        llm = LLMClient(llm_http, base_url="http://llm/v1", api_key="", model="mock-large")
        for n, case in enumerate(cases, start=1):
            gh = FakeGitHub(files={}, diff=case["diff"])

            async def token(_: int) -> str:
                return "ghs_fake_1_1"

            repo = f"evalrev{base + n}/app"
            async with httpx.AsyncClient(transport=gh.transport()) as gh_http:
                deps = ReviewDeps(
                    pool=pool,
                    github=GitHubClient(gh_http, token, GITHUB_API),
                    llm=llm,
                    redis=redis,
                    worker_id="eval",
                    system_prompt=system_prompt,
                )
                payload = {
                    "action": "opened",
                    "pull_request": {
                        "number": 1,
                        "title": case["title"],
                        "body": "",
                        "head": {"sha": f"{base + n:040x}"},
                    },
                    "repository": {
                        "id": base + n,
                        "full_name": repo,
                        "private": True,
                        "owner": {"login": repo.split("/")[0]},
                    },
                    "installation": {"id": base + n},
                }
                event_id = await pool.fetchval(
                    "INSERT INTO events (source, delivery_id, event_type, action, installation_id,"
                    " payload) VALUES ('github', $1, 'pull_request', 'opened', $2, $3::jsonb)"
                    " RETURNING id",
                    f"eval-{uuid.uuid4()}",
                    base + n,
                    json.dumps(payload),
                )
                await handle_review_message(
                    deps, Message("jobs:review", "0-1", json.dumps({"event_id": str(event_id)}))
                )
            body = "\n".join(c["body"] for c in gh.comments_for(repo, 1))
            reviews[case["id"]] = body
            found = [(t.strip(), p, int(ln)) for t, p, ln in FINDING.findall(body)]
            matched_expected, line_ok, matched_findings = 0, 0, set()
            for exp in case["expected"]:
                for i, (title, path, line) in enumerate(found):
                    if i in matched_findings:
                        continue
                    if title.lower() == exp["category"].lower() and path == exp["path"]:
                        matched_findings.add(i)
                        matched_expected += 1
                        line_ok += int(abs(line - exp["line"]) <= LINE_TOLERANCE)
                        break
            results.append(
                ReviewCaseResult(
                    id=case["id"],
                    expected=len(case["expected"]),
                    found=len(found),
                    matched=matched_expected,
                    line_ok=line_ok,
                    findings=[f"{t} {p}:{ln}" for t, p, ln in found],
                )
            )
        for r, case in zip(results, cases, strict=True):
            r.judge_score = await judge(
                llm, [e["category"] for e in case["expected"]], reviews[case["id"]]
            )
    expected = sum(r.expected for r in results)
    matched = sum(r.matched for r in results)
    total_found = sum(r.found for r in results)
    metrics = {
        "review.recall": round(matched / expected, 4) if expected else 1.0,
        "review.precision": round(matched / total_found, 4) if total_found else 1.0,
        "review.line_accuracy": round(sum(r.line_ok for r in results) / matched, 4)
        if matched
        else 0.0,
        "review.judge_mean": round(sum(r.judge_score or 0 for r in results) / len(results), 3),
    }
    return metrics, results, reviews


async def judge_agreement(reviews: dict[str, str]) -> float:
    """Share of hand-labelled reviews where the judge is within 1 point of the human."""
    labels = _jsonl(ROOT / "review" / "spotcheck.jsonl")
    cases = {c["id"]: c for c in _jsonl(ROOT / "review" / "cases.jsonl")}
    async with await _llm_http() as llm_http:
        llm = LLMClient(llm_http, base_url="http://llm/v1", api_key="", model="mock-large")
        agree = 0
        for label in labels:
            expected = [e["category"] for e in cases[label["case"]]["expected"]]
            score = await judge(llm, expected, label["review"])
            agree += int(abs(score - int(label["human_score"])) <= 1)
    return round(agree / len(labels), 4) if labels else 1.0


async def run_qa_suite(
    pool: asyncpg.Pool, redis: Redis
) -> tuple[dict[str, float], list[dict[str, Any]]]:
    files = json.loads((ROOT / "qa" / "repo.json").read_text())
    cases = _jsonl(ROOT / "qa" / "cases.jsonl")
    base = 3_000_000 + uuid.uuid4().int % 1_000_000 * 10
    gh = FakeGitHub(files=files)
    embedder = HashingEmbedder()

    async def token(_: int) -> str:
        return "ghs_fake_1_1"

    out: list[dict[str, Any]] = []
    async with (
        httpx.AsyncClient(transport=gh.transport()) as gh_http,
        await _llm_http() as llm_http,
    ):
        github = GitHubClient(gh_http, token, GITHUB_API)
        tenant = await upsert_tenant(pool, base, f"evalqa{base}")
        repo_id = await upsert_repo(pool, tenant, base, f"evalqa{base}/app", True)
        await Indexer(pool, github, embedder).full_index(
            RepoRef(tenant, repo_id, base, f"evalqa{base}/app"), "a" * 40, datetime.now(UTC)
        )
        llm = LLMClient(llm_http, base_url="http://llm/v1", api_key="", model="mock-large")
        deps = QADeps(pool, github, llm, embedder, "eval")
        for case in cases:
            hits = await hybrid_search(
                pool,
                embedder,
                tenant_id=tenant,
                repo_ids=[repo_id],
                query=case["question"],
                k=5,
                reranker=LexicalReranker(),
            )
            retrieved = [h.path for h in hits]
            async with pool.acquire() as conn:
                run_id, _ = await create_qa_run(
                    cast(asyncpg.Connection, conn),
                    tenant_id=tenant,
                    repo_id=repo_id,
                    question=case["question"],
                    requested_by="eval",
                )
            await handle_qa_message(
                deps, Message("jobs:qa", "0-1", json.dumps({"run_id": str(run_id)}))
            )
            result = json.loads(
                await pool.fetchval("SELECT result::text FROM runs WHERE id = $1", run_id) or "{}"
            )
            cited = [c["path"] for c in result.get("citations", [])]
            out.append(
                {
                    "id": case["id"],
                    "retrieval_hit": any(f in retrieved for f in case["expected_files"]),
                    "citation_hit": any(f in cited for f in case["expected_files"]),
                    "retrieved": retrieved,
                    "cited": cited,
                }
            )
    n = len(out)
    return {
        "qa.retrieval_recall_at_5": round(sum(c["retrieval_hit"] for c in out) / n, 4),
        "qa.citation_accuracy": round(sum(c["citation_hit"] for c in out) / n, 4),
    }, out


async def run_all(
    pool: asyncpg.Pool, redis: Redis, *, system_prompt: str = "", safety: bool = True
) -> EvalReport:
    review_metrics, review_cases, reviews = await run_review_suite(
        pool, redis, system_prompt=system_prompt
    )
    qa_metrics, qa_cases = await run_qa_suite(pool, redis)
    metrics = {**review_metrics, **qa_metrics}
    if safety:
        injection = await run_injection(pool, redis)
        metrics["safety.attack_success_rate"] = injection.attack_success_rate
    agreement = await judge_agreement(reviews)
    metrics["judge.agreement"] = agreement
    return EvalReport(
        started_at=datetime.now(UTC).isoformat(),
        prompt_version=prompt_version(system_prompt or SYSTEM_PROMPT),
        metrics=metrics,
        review_cases=review_cases,
        qa_cases=qa_cases,
        judge_agreement=agreement,
    )


def gate(metrics: dict[str, float], baseline: dict[str, float]) -> list[str]:
    """Regressions against the baseline (empty = pass)."""
    failures = []
    for name, tol in TOLERANCE.items():
        if name not in metrics or name not in baseline:
            continue
        value, base = metrics[name], baseline[name]
        if name in LOWER_IS_BETTER:
            if value > base + tol:
                failures.append(f"{name}: {value:.3f} > baseline {base:.3f} + {tol}")
        elif value < base - tol:
            failures.append(f"{name}: {value:.3f} < baseline {base:.3f} - {tol}")
    if metrics.get("judge.agreement", 1.0) < MIN_JUDGE_AGREEMENT:
        failures.append(
            f"judge.agreement: {metrics['judge.agreement']:.2f} < {MIN_JUDGE_AGREEMENT}"
            " (the judge no longer matches the hand-labelled sample)"
        )
    return failures


def render_markdown(report: EvalReport, baseline: dict[str, float], failures: list[str]) -> str:
    lines = [
        "# Argus eval report",
        "",
        f"Prompt version `{report.prompt_version}`, {report.started_at}.",
        "",
        "| Metric | Value | Baseline | |",
        "|---|---|---|---|",
    ]
    for name, value in sorted(report.metrics.items()):
        base = baseline.get(name)
        bad = any(f.startswith(name) for f in failures)
        lines.append(
            f"| {name} | {value:.3f} | {'' if base is None else f'{base:.3f}'} | "
            f"{'**regressed**' if bad else ''} |"
        )
    lines += ["", "## Gate", "", "**FAIL**" if failures else "**PASS**", ""]
    lines += [f"- {f}" for f in failures]
    missed = [r for r in report.review_cases if r.matched < r.expected or r.found > r.matched]
    if missed:
        lines += [
            "",
            "## Review cases with misses or noise",
            "",
            "| Case | Expected | Matched | Findings |",
            "|---|---|---|---|",
        ]
        lines += [
            f"| {r.id} | {r.expected} | {r.matched} | {'; '.join(r.findings) or '-'} |"
            for r in missed
        ]
    return "\n".join(lines) + "\n"


async def _cli(database_url: str, redis_url: str, prompt: str) -> EvalReport:
    from argus.db.migrate import default_migrations_dir
    from argus.db.migrate import run as migrate
    from argus.db.pool import create_pool

    await migrate(database_url, default_migrations_dir())
    pool = await create_pool(database_url, max_size=10)
    redis = Redis.from_url(redis_url, decode_responses=True)
    try:
        return await run_all(pool, redis, system_prompt=prompt)
    finally:
        await redis.aclose()
        await pool.close()


def main() -> None:
    p = argparse.ArgumentParser(description="Argus offline evals and CI gate.")
    p.add_argument("--database-url", required=True)
    p.add_argument("--redis-url", required=True)
    p.add_argument("--out", default="evals/reports/latest.json")
    p.add_argument("--prompt-file", default="", help="review system prompt to evaluate")
    p.add_argument("--check", action="store_true", help="exit 1 on regression vs baseline")
    p.add_argument("--update-baseline", action="store_true")
    args = p.parse_args()
    prompt = Path(args.prompt_file).read_text() if args.prompt_file else ""
    report = asyncio.run(_cli(args.database_url, args.redis_url, prompt))
    baseline_path = ROOT / "baseline.json"
    baseline = json.loads(baseline_path.read_text()) if baseline_path.exists() else {}
    failures = gate(report.metrics, baseline)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({**report.to_json(), "failures": failures}, indent=2, default=str))
    md = render_markdown(report, baseline, failures)
    out.with_suffix(".md").write_text(md)
    print(md)
    if args.update_baseline:
        baseline_path.write_text(json.dumps(report.metrics, indent=2, sort_keys=True) + "\n")
        with (ROOT / "history.jsonl").open("a") as f:
            f.write(
                json.dumps(
                    {
                        "at": report.started_at,
                        "prompt_version": report.prompt_version,
                        **report.metrics,
                    }
                )
                + "\n"
            )
    raise SystemExit(1 if args.check and failures else 0)
