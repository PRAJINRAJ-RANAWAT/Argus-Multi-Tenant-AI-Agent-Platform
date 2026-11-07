# 0008. pgvector in the main Postgres for code search, not a dedicated vector store

- Status: accepted
- Date: 2026-11-07

## Context

M3 needs vector search over code chunks, combined with keyword search for exact identifiers, filtered by tenant and repo, and kept in step with every push. The candidates are pgvector in the existing Postgres, or a dedicated store (Qdrant, Weaviate, OpenSearch k-NN).

Design targets (SPEC capacity section): tens of tenants, hundreds of repos, low millions of chunks at 256 dimensions. One chunk is about 1 KB of vector plus its text.

## Decision

- Chunks, vectors (`vector(256)`, HNSW cosine) and full-text (`tsvector` GIN) live in one `code_chunks` table in the main Postgres.
- Hybrid search runs two SQL queries, both with the tenant and repo filter **inside** the query, and fuses them with reciprocal rank fusion (k = 60). pgvector 0.8's iterative HNSW scan (`hnsw.iterative_scan = relaxed_order`) keeps filtered vector queries from returning too few rows.
- 256 dimensions for every embedder: the mock's hashing embedder and real `text-embedding-3-*` models with `dimensions=256`, so a provider swap does not change the schema.
- Embeddings are cached per tenant by the hash of the embedded text, so only changed chunks are embedded. The cache is per tenant so no vector is shared across tenants.
- Indexing writes are per file and guarded by event receipt time, with tombstones for deleted files.

## Consequences

- One database: chunk writes, tombstones and the embedding cache commit atomically with the file row; backups and row-level security (M4) cover vectors for free.
- Full-text uses the `simple` configuration plus identifier sub-words (`retryWithBackoff` → `retry with backoff`) stored in `search_text`, since English stemming is wrong for code.
- Index builds and large re-indexes share the database's CPU and I/O with the transactional workload.
- The re-ranker is lexical for now; an LLM or cross-encoder re-ranker plugs in behind the `Reranker` interface once the gateway exists (M5).

## When we would revisit

- More than ~10M chunks, or HNSW memory beyond what the primary can keep cached → quantise (`halfvec`, binary quantisation with re-scoring), then move vectors to Qdrant with tenant payload filters.
- Index builds measurably hurt p95 of transactional queries → move indexing to a replica or a dedicated store.
- Need for multi-vector or sparse-dense hybrid models that pgvector does not support.
