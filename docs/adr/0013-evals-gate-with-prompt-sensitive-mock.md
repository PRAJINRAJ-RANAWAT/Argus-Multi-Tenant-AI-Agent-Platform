# 0013. Eval gate in CI with a deterministic, prompt-sensitive mock model

- Status: accepted
- Date: 2026-11-07

## Context

M8: a deliberately worse prompt must fail CI. Running a real model on every PR costs money, is slow and is non-deterministic, so thresholds would flap. A mock that ignores the prompt can never catch a prompt regression.

## Decision

- **The mock reviewer follows instructions.** `argus.mock_llm.reviewer` parses the system prompt for what it asks (bugs, security problems, missing tests, skip nitpicks and praise) and runs real heuristics over the added lines of the diff (SQL and command injection, hardcoded secrets, unsafe deserialisation, off-by-one, missing None checks, swallowed exceptions, mutable defaults, missing tests), citing `path:line`. Remove "security problems" from the prompt and the security findings disappear, as they would with an instruction-following model.
- **Suites** (`argus-eval`): PR review golden set (20 PRs, 16 planted issues, 5 clean) scored exactly (recall, precision, line accuracy); an LLM judge (1-5) whose agreement with 10 hand-labelled reviews is measured every run; Q&A retrieval recall@5 scored separately from answer citation accuracy; the injection suite's ASR.
- **Gate**: `evals/baseline.json` holds the accepted metrics; a run fails if any metric drops past its tolerance (0.05 for ratios, 0.3 for judge mean; ASR may rise at most 0.02) or the judge's agreement falls below 0.8. It runs twice in CI: as pytest tests and as the `evals` job, which publishes the report to the job summary and as an artifact. `--update-baseline` records an accepted change and appends to `evals/history.jsonl`.
- **Online**: a sample of successful reviews (`ARGUS_ONLINE_EVAL_RATE`, default 5%) is scored by the judge through the gateway and stored with its prompt version; users can rate runs up or down; `/api/evals/online` reports both.

## Consequences

- Deterministic gate: the same code gives the same numbers, so a 0.05 tolerance is meaningful.
- The mock measures the *system* (prompt plumbing, tool loop, retrieval, guardrails, scoring), not a real model's judgment. The same suites run against a real provider by pointing the worker at real keys (nightly, not per PR) to track model quality.
- Baseline at this commit: review recall 1.00, precision 1.00, line accuracy 1.00, judge 5.0 (agreement 1.0), Q&A retrieval@5 0.93 and citations 0.93 (the miss is "proration" vs `prorate`, which the lexical embedder does not stem), ASR 3.5%.

## When we would revisit

- With real keys available: add a nightly real-model run with its own baseline and wider tolerances, and use the judge sample size needed for a stable mean.
- When the golden sets grow past a few minutes of CI time: run a fast subset on PRs and the full set nightly.
