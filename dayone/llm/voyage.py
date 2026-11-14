"""Voyage embeddings (via the Atlas-hosted endpoint) behind the `Embedder` contract."""

from __future__ import annotations

import httpx

from dayone.config import Settings

TIMEOUT_ATTEMPTS = 2  # one retry, on timeout only


class EmbeddingError(RuntimeError):
    pass


class EmbeddingTimeout(EmbeddingError):
    pass


class VoyageEmbedder:
    def __init__(self, settings: Settings) -> None:
        self._model = settings.embedding_model
        self._dimensions = settings.embedding_dimensions
        self._client = httpx.Client(
            base_url=settings.embedding_base_url,
            headers={"Authorization": f"Bearer {settings.voyage_api_key}"},
            timeout=settings.embedding_timeout_s,
        )

    def embed(self, texts: list[str], input_type: str) -> list[list[float]]:
        payload = {
            "input": texts,
            "model": self._model,
            "input_type": input_type,
            "output_dimension": self._dimensions,
        }
        for attempt in range(1, TIMEOUT_ATTEMPTS + 1):
            try:
                response = self._client.post("/embeddings", json=payload)
                break
            except httpx.TimeoutException as error:
                if attempt == TIMEOUT_ATTEMPTS:
                    raise EmbeddingTimeout(f"{self._model} timed out {TIMEOUT_ATTEMPTS} times") from error
        if response.status_code != httpx.codes.OK:
            raise EmbeddingError(f"{self._model} returned HTTP {response.status_code}: {response.text[:500]}")
        rows = sorted(response.json()["data"], key=lambda row: row["index"])
        vectors = [row["embedding"] for row in rows]
        if len(vectors) != len(texts) or any(len(vector) != self._dimensions for vector in vectors):
            raise EmbeddingError(f"expected {len(texts)} vectors of {self._dimensions} dims from {self._model}")
        return vectors

    def close(self) -> None:
        self._client.close()
