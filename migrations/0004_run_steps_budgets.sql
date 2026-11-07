-- Durable agent runtime: checkpointed steps and per-run budgets.

CREATE TABLE run_steps (
    run_id         uuid        NOT NULL REFERENCES runs (id) ON DELETE CASCADE,
    seq            integer     NOT NULL CHECK (seq >= 0),
    kind           text        NOT NULL
                   CHECK (kind IN ('llm_call', 'tool_call', 'retrieval', 'approval')),
    name           text        NOT NULL,                     -- model or tool name
    input_hash     text        NOT NULL,                     -- sha256 of canonical input
    input          jsonb,
    status         text        NOT NULL DEFAULT 'started'
                   CHECK (status IN ('started', 'done', 'failed')),
    output         jsonb,
    error          text,
    lease_version  bigint      NOT NULL,                     -- fencing token of the writer
    attempts       integer     NOT NULL DEFAULT 1,
    input_tokens   integer     NOT NULL DEFAULT 0,
    output_tokens  integer     NOT NULL DEFAULT 0,
    started_at     timestamptz NOT NULL DEFAULT now(),
    finished_at    timestamptz,
    PRIMARY KEY (run_id, seq)
);

ALTER TABLE runs
    ADD COLUMN max_steps   integer NOT NULL DEFAULT 25 CHECK (max_steps > 0),
    ADD COLUMN max_tokens  bigint  NOT NULL DEFAULT 200000 CHECK (max_tokens > 0),
    ADD COLUMN deadline_at timestamptz;
