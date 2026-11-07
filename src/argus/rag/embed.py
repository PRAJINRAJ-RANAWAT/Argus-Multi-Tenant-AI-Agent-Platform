"""Embedders. All produce 256-dimensional, L2-normalised vectors (``EMBED_DIM``).

* ``HashingEmbedder``: deterministic feature hashing of words and identifier sub-words.
  No model, no cost; lexical rather than semantic. The mock LLM serves it at
  ``/v1/embeddings`` so dev, tests and load tests never pay for embeddings.
* ``HttpEmbedder``: any OpenAI-compatible ``/embeddings`` endpoint, asking for 256
  dimensions (``text-embedding-3-*`` support the ``dimensions`` parameter), so a real
  provider fits the same ``vector(256)`` column.
"""

import hashlib
import math
import re
from typing import Protocol

import httpx

from argus.rag.chunker import split_identifier

EMBED_DIM = 256
_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|\d+")


class Embedder(Protocol):
    model: str

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


def hashing_vector(text: str, dim: int = EMBED_DIM) -> list[float]:
    vec = [0.0] * dim
    for tok in _TOKEN.findall(text):
        feats = {tok.lower(), *split_identifier(tok)}
        for f in feats:
            h = hashlib.blake2b(f.encode(), digest_size=8).digest()
            idx = int.from_bytes(h[:4], "little") % dim
            sign = 1.0 if h[4] & 1 else -1.0
            vec[idx] += sign
    norm = math.sqrt(sum(v * v for v in vec))
    if norm == 0:
        vec[0] = 1.0
        return vec
    return [v / norm for v in vec]


class HashingEmbedder:
    def __init__(self, model: str = "hash-256") -> None:
        self.model = model
        self.calls = 0
        self.texts_embedded = 0

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        self.texts_embedded += len(texts)
        return [hashing_vector(t) for t in texts]


class EmbeddingError(Exception):
    pass


class HttpEmbedder:
    def __init__(self, http: httpx.AsyncClient, *, base_url: str, api_key: str, model: str) -> None:
        self.http = http
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        try:
            resp = await self.http.post(
                f"{self.base_url}/embeddings",
                json={"model": self.model, "input": texts, "dimensions": EMBED_DIM},
                headers=headers,
            )
        except httpx.TransportError as exc:
            raise EmbeddingError(f"transport: {exc}") from exc
        if resp.status_code >= 400:
            raise EmbeddingError(f"embeddings {resp.status_code}: {resp.text[:300]}")
        data = sorted(resp.json()["data"], key=lambda d: d["index"])
        vectors = [list(map(float, d["embedding"])) for d in data]
        if len(vectors) != len(texts) or any(len(v) != EMBED_DIM for v in vectors):
            raise EmbeddingError("embedding response has the wrong shape")
        return vectors


def to_pgvector(vec: list[float]) -> str:
    return "[" + ",".join(f"{v:.6f}" for v in vec) + "]"
