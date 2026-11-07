-- Code RAG index: files, structural chunks with vectors and full-text, call references,
-- and a per-tenant embedding cache keyed by content hash.

-- Embedding cache: a chunk whose text did not change is never embedded again.
CREATE TABLE embeddings (
    tenant_id     uuid        NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
    model         text        NOT NULL,
    content_hash  text        NOT NULL,
    embedding     vector(256) NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, model, content_hash)
);

-- One row per indexed path. ``source_received_at`` is the receipt time of the event
-- whose content is stored: an older event processed late never overwrites newer content.
-- Deleted files keep a tombstone row (``deleted``) for the same reason.
CREATE TABLE code_files (
    tenant_id           uuid        NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
    repo_id             uuid        NOT NULL REFERENCES repos (id) ON DELETE CASCADE,
    path                text        NOT NULL,
    commit_sha          text        NOT NULL,
    language            text,
    deleted             boolean     NOT NULL DEFAULT false,
    source_received_at  timestamptz NOT NULL,
    indexed_at          timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (repo_id, path)
);

CREATE TABLE code_chunks (
    id            bigserial PRIMARY KEY,
    tenant_id     uuid        NOT NULL,
    repo_id       uuid        NOT NULL,
    path          text        NOT NULL,
    ordinal       integer     NOT NULL,
    kind          text        NOT NULL,          -- function, method, class, module, doc, text
    symbol        text,
    signature     text,
    language      text,
    start_line    integer     NOT NULL,
    end_line      integer     NOT NULL,
    commit_sha    text        NOT NULL,
    content       text        NOT NULL,
    content_hash  text        NOT NULL,
    search_text   text        NOT NULL,          -- content + split identifiers + path
    tsv           tsvector GENERATED ALWAYS AS (to_tsvector('simple', search_text)) STORED,
    embedding     vector(256) NOT NULL,
    FOREIGN KEY (repo_id, path) REFERENCES code_files (repo_id, path) ON DELETE CASCADE,
    CONSTRAINT code_chunks_file_ordinal_uniq UNIQUE (repo_id, path, ordinal)
);

CREATE INDEX code_chunks_tenant_repo_idx ON code_chunks (tenant_id, repo_id);
CREATE INDEX code_chunks_tsv_idx ON code_chunks USING gin (tsv);
CREATE INDEX code_chunks_embedding_idx ON code_chunks USING hnsw (embedding vector_cosine_ops);
CREATE INDEX code_chunks_symbol_idx ON code_chunks (repo_id, symbol);

-- Call graph edges for "find callers": caller chunk → callee name.
CREATE TABLE code_refs (
    tenant_id      uuid    NOT NULL,
    repo_id        uuid    NOT NULL,
    path           text    NOT NULL,
    caller_symbol  text,
    callee         text    NOT NULL,             -- bare name of the called function
    line           integer NOT NULL,
    FOREIGN KEY (repo_id, path) REFERENCES code_files (repo_id, path) ON DELETE CASCADE
);

CREATE INDEX code_refs_callee_idx ON code_refs (repo_id, callee);

CREATE TABLE repo_index_state (
    repo_id          uuid        PRIMARY KEY REFERENCES repos (id) ON DELETE CASCADE,
    tenant_id        uuid        NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
    default_branch   text,
    last_commit      text,
    last_full_index  timestamptz,
    updated_at       timestamptz NOT NULL DEFAULT now()
);
