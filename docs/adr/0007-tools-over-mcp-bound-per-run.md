# 0007. Tools over MCP: per-run servers, allowlists, runtime-only writes

- Status: accepted
- Date: 2026-11-07

## Context

M2 moves the review agent from a fixed pipeline to an LLM tool loop. The spec requires tools served over MCP with the official SDK (`argus-code`, `argus-github`), per-agent allowlists, read/write tags, idempotent writes, and a reviewer that cannot open issues. The model's output is untrusted: it can hallucinate tools, pass hostile arguments (path traversal, another repo), or be steered by prompt injection in a diff.

## Decision

- **Servers are built per run and bound to it.** `RunContext` fixes tenant, repo, PR, head SHA and lease version. No tool takes a repo or PR argument, so a model cannot reach another repository whatever it passes.
- **In-process MCP for now.** The runtime connects with `mcp.Client(server)` (SDK v2, in-memory transport): the real protocol, schemas and error semantics, without the network. The same `MCPServer` objects can be served over Streamable HTTP with OAuth 2.1 when tools must run remotely (M4).
- **Two lists per agent** (`AgentToolPolicy`): `model_tools` are offered to the LLM; `runtime_tools` are called only by agent code. The reviewer's model sees only read tools; posting the review is a runtime call. Anything else is denied, recorded as a step (an audit trail), and returned to the model as an error.
- **Writes need a runtime-supplied `idempotency_key`.** It is stripped from the schema shown to the model and from model-supplied arguments, and each write goes through the side-effect ledger (plus a hidden marker for comments).
- **Errors:** a tool's deliberate `ToolError` (file not found, path outside the repo) goes back to the model; an unexpected crash (GitHub 5xx) surfaces from the SDK as a generic error, which the runtime turns into `ToolCrashError`, so the step fails and the queue retries it.
- Tool output re-enters the conversation wrapped in `<untrusted_data source="tool:…">`.

## Consequences

- A prompt-injected model can at worst call read tools on its own PR; tests show `open_issue`, `post_review_comment` and unknown tools are denied and audited.
- Every LLM turn and tool call is a checkpointed step, so the conversation is rebuilt from `run_steps` on resume. LLM-turn inputs are stored only as hashes to avoid quadratic storage.
- Detecting crashes by the SDK's generic error text is brittle; if the SDK changes it, crashes would reach the model as errors (degraded, not unsafe). A test covers it.

## When we would revisit

- Tools need separate scaling, isolation or other languages → serve them over Streamable HTTP with OAuth (M4).
- A model needs write tools directly (e.g. inline review comments) → add them to `model_tools` behind the approval flow (M6).
