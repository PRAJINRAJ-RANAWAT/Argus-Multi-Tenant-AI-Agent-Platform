# 0011. Guardrails in layers, measured by an injection eval with a hijacked model

- Status: accepted
- Date: 2026-11-07

## Context

Argus reads text written by strangers on every run (PR descriptions, diffs, code, questions), so indirect prompt injection is the main threat. No classifier catches every injection, so the system must stay safe when the model *is* fooled, and the claim "has guardrails" needs a number behind it.

## Decision

Defences in layers, each cheap and independent:

1. **Separate instructions from data.** Untrusted text is wrapped in `<untrusted_data>`; attempts to close or reopen the wrapper are neutralised.
2. **Injection classifier** (weighted rules on intent: override, role change, exfiltration, tool invocation, wrapper escape, hidden text, multilingual overrides). A flagged run gets a reduced tool set for the rest of the run, a visible warning in the review, and an **echo filter** that drops output lines copied verbatim from untrusted input (code blocks kept).
3. **Capability limits do the real work.** Per-agent tool allowlists (the reviewer has no approve or issue tool), tools bound to the run's repo with repo-relative paths only, and optional human approval of write actions (`waiting_approval`; the run releases its worker and resumes by replay).
4. **Output scanning** before anything is posted or returned: secrets (token formats, private keys, `*_SECRET=…` assignments) and PII (emails, phones, Luhn-valid cards, SSNs) are redacted. The same redaction runs in the JSON log formatter.
5. **Egress allowlist** in the HTTP transport used by agents and tools.

**Measurement.** `argus-eval-injection` runs 29 attacks (plus a benign control) through the real pipelines with the mock LLM in `obey_injections` mode, a model that does whatever the injected text says. It reports the attack success rate (ASR) and re-runs with layers turned off. CI fails if ASR exceeds 5%, if any secret/PII/tool/scope/egress attack succeeds, or on a false positive.

## Consequences

- First run: 34.5% ASR. The eval found three real gaps (flagged runs still echoed injected text; classifier misses on common phrasings; absolute paths silently treated as repo-relative) and two harness bugs. After fixes: **3.5% ASR (1/29)**, 0 false positives; without the classifier 31.0%, without output scanning 17.2% (`evals/reports/injection.md`).
- The remaining success is a polite, keyword-free request to end the summary with a phrase. It changes wording only; no write tool, secret or other repo is reachable.
- Rules are cheap and explainable but brittle against paraphrase; the echo filter only catches verbatim copies.

## When we would revisit

- Add an LLM classifier (gateway small tier) when the rule set's recall on a larger, paraphrased attack set drops below target.
- Move from verbatim echo detection to semantic similarity if attacks start paraphrasing payloads.
