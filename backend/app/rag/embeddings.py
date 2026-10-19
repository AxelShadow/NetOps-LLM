"""Провайдеры эмбеддингов для RAG.

Два режима:

* `OpenAICompatibleEmbeddings` — POST `{base}/embeddings` на тот же
  OpenAI-совместимый сервер, что и LLM (LM Studio / vLLM). Модель задаётся
  `NETOPS_EMBEDDING_MODEL` (bge-m3, Qwen3-Embedding-0.6B, ...), размерность
  должна совпасть с `NETOPS_EMBEDDING_DIM` — иначе коллекция Qdrant создастся
  с неверной размерностью и поиск будет падать.
* `MockEmbeddings` — детерминированный bag-of-words хэш: нужен для dev-стека
  и тестов, когда сервера эмбеддингов нет (`NETOPS_MOCK_MODE=true`). Семантика
  грубая (совпадение слов), зато без сети и воспроизводимо.

Секретов в текстах ошибок нет: ключ доступа передаётся заголовком и не
логируется (см. `.dsh/security/policy.md`).
"""
from __future__ import annotations

import hashlib
import logging
import re
from typing import Protocol

import httpx

from ..config import get_settings

log = logging.getLogger(__name__)

_WORD_RE = re.compile(r"[\w\-]+", re.UNICODE)


class EmbeddingError(RuntimeError):
    """Ошибка получения эмбеддингов (сеть, формат ответа, размерность)."""


class EmbeddingProvider(Protocol):
    dim: int

    def embed_texts(self, texts: list[str]) -> list[list[float]]: ...


class MockEmbeddings:
    """Детерминированный провайдер: хэш слова -> измерение, L2-нормализация."""

    def __init__(self, dim: int = 1024):
        self.dim = max(8, int(dim))

    def _vector(self, text: str) -> list[float]:
        vector = [0.0] * self.dim
        words = _WORD_RE.findall(text.lower())
        grams = words + [f"{a} {b}" for a, b in zip(words, words[1:])]
        for gram in grams:
            digest = hashlib.sha256(gram.encode("utf-8")).digest()
            position = int.from_bytes(digest[:4], "big") % self.dim
            vector[position] += 1.0
        norm = sum(v * v for v in vector) ** 0.5
        if norm:
            vector = [v / norm for v in vector]
        return vector

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]


class OpenAICompatibleEmbeddings:
    """POST {base_url}/embeddings на OpenAI-совместимый сервер."""

    def __init__(self, base_url: str, model: str, auth_key: str = "",
                 timeout: float = 60, dim: int = 1024):
        if not base_url:
            raise EmbeddingError(
                "Не задан NETOPS_EMBEDDING_BASE_URL и пуст NETOPS_LLM_BASE_URL: "
                "некуда отправлять запросы эмбеддингов")
        if not model:
            raise EmbeddingError(
                "Не задана модель эмбеддингов (NETOPS_EMBEDDING_MODEL)")
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.auth_key = auth_key
        self.timeout = timeout
        self.dim = int(dim)

    def _headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.auth_key:
            headers["Authorization"] = "Bearer " + self.auth_key
        return headers

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        try:
            with httpx.Client(timeout=self.timeout) as client:
                response = client.post(
                    f"{self.base_url}/embeddings",
                    headers=self._headers(),
                    json={"model": self.model, "input": texts},
                )
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPStatusError as exc:
            raise EmbeddingError(
                f"Сервер эмбеддингов вернул HTTP {exc.response.status_code}") from exc
        except httpx.HTTPError as exc:
            raise EmbeddingError(
                f"Сервер эмбеддингов недоступен: {type(exc).__name__}") from exc
        except ValueError as exc:
            raise EmbeddingError("Сервер эмбеддингов вернул не JSON") from exc

        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list) or len(data) != len(texts):
            raise EmbeddingError(
                "Сервер эмбеддингов вернул неверное число векторов: "
                f"ожидалось {len(texts)}")
        ordered = sorted(data, key=lambda item: item.get("index", 0))
        vectors: list[list[float]] = []
        for item in ordered:
            vector = item.get("embedding")
            if not isinstance(vector, list) or not vector:
                raise EmbeddingError("Пустой вектор в ответе сервера эмбеддингов")
            if len(vector) != self.dim:
                raise EmbeddingError(
                    "Размерность эмбеддинга не совпадает с "
                    f"NETOPS_EMBEDDING_DIM: получено {len(vector)}, "
                    f"ожидалось {self.dim}")
            vectors.append([float(x) for x in vector])
        return vectors


def build_embedding_provider() -> EmbeddingProvider:
    """Выбирает провайдера по настройкам (мок-режим имеет приоритет)."""
    settings = get_settings()
    if settings.mock_mode:
        return MockEmbeddings(dim=settings.embedding_dim)
    base_url = settings.embedding_base_url or settings.llm_base_url
    model = settings.embedding_model or settings.llm_default_model
    auth_key = settings.embedding_api_key or settings.llm_api_key
    return OpenAICompatibleEmbeddings(
        base_url=base_url, model=model, auth_key=auth_key,
        timeout=settings.embedding_timeout, dim=settings.embedding_dim)
