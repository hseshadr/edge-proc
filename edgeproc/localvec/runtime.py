"""LocalVecRuntime — a Runtime backed by a vector index, an encoder, and BM25.

Serves three TaskKinds locally:

- ``EMBED``  → encode ``payload.texts`` into vectors.
- ``SEARCH`` → vector similarity over the index for ``payload.query``.
- ``RANK``   → hybrid keyword + vector, fused with Reciprocal Rank Fusion.

It only accepts ``PrivacyMode.LOCAL_ONLY``: a cloud privacy mode is rejected, never
silently downgraded. Bad input becomes a failure envelope — nothing raises across
the runtime boundary.
"""

from __future__ import annotations

from time import perf_counter
from typing import cast

from shared_libs_python.vector_mgmt.core.types import VectorIndex

from edgeproc._version import __version__
from edgeproc.core.models import (
    CapabilityVerdict,
    JsonValue,
    PrivacyMode,
    Provenance,
    ResultEnvelope,
    Task,
    TaskKind,
)
from edgeproc.localvec.encoder import Encoder
from edgeproc.localvec.fusion import reciprocal_rank_fusion
from edgeproc.localvec.searcher import KeywordSearcher

_SUPPORTED = frozenset({TaskKind.EMBED, TaskKind.SEARCH, TaskKind.RANK})
_DEFAULT_K = 10


class LocalVecRuntime:
    """Local embed/search/rank over a shared-libs ``VectorIndex``."""

    name = "localvec"

    def __init__(
        self,
        encoder: Encoder,
        index: VectorIndex,
        keyword: KeywordSearcher | None = None,
    ) -> None:
        self._encoder = encoder
        self._index = index
        self._keyword = keyword

    def can_handle(self, task: Task) -> CapabilityVerdict:
        if task.privacy_mode != PrivacyMode.LOCAL_ONLY:
            return CapabilityVerdict.REJECT_CAPABILITY
        if task.kind in _SUPPORTED:
            return CapabilityVerdict.ACCEPT
        return CapabilityVerdict.REJECT_KIND

    async def execute(self, task: Task) -> ResultEnvelope:
        start = perf_counter()
        try:
            payload = await self._dispatch(task)
        except (ValueError, KeyError, TypeError) as exc:
            return self._envelope(task, success=False, payload={}, start=start, error=str(exc))
        return self._envelope(task, success=True, payload=payload, start=start)

    async def _dispatch(self, task: Task) -> dict[str, JsonValue]:
        if task.kind == TaskKind.EMBED:
            return self._embed(task)
        if task.kind == TaskKind.SEARCH:
            return await self._search(task)
        return await self._rank(task)

    def _embed(self, task: Task) -> dict[str, JsonValue]:
        vectors = self._encoder.encode_texts(_require_texts(task))
        rows: list[JsonValue] = [row.tolist() for row in vectors]
        return {"embeddings": rows}

    async def _search(self, task: Task) -> dict[str, JsonValue]:
        query = self._encoder.encode_query(_require_query(task)).tolist()
        hits = await self._index.search(query, _require_k(task))
        return {"results": _as_rows(hits)}

    async def _rank(self, task: Task) -> dict[str, JsonValue]:
        if self._keyword is None:
            raise ValueError("RANK requires a keyword searcher")
        text = _require_query(task)
        k = _require_k(task)
        keyword_hits = self._keyword.search(text, k=k)
        vector_hits = await self._index.search(self._encoder.encode_query(text).tolist(), k)
        return {"results": _as_rows(reciprocal_rank_fusion(keyword_hits, vector_hits)[:k])}

    def _envelope(
        self,
        task: Task,
        *,
        success: bool,
        payload: dict[str, JsonValue],
        start: float,
        error: str | None = None,
    ) -> ResultEnvelope:
        return ResultEnvelope(
            request_id=task.request_id,
            task_kind=task.kind,
            success=success,
            payload=payload,
            runtime_used=self.name,
            privacy_mode=task.privacy_mode,
            confidence=1.0 if success else 0.0,
            latency_ms=(perf_counter() - start) * 1000.0,
            provenance=Provenance(signature_status="unsigned", runtime_version=__version__),
            error=error,
        )


def _as_rows(hits: list[tuple[str, float]]) -> list[JsonValue]:
    return [cast(JsonValue, [doc, score]) for doc, score in hits]


def _require_texts(task: Task) -> list[str]:
    texts = task.payload.get("texts")
    if not isinstance(texts, list) or not all(isinstance(item, str) for item in texts):
        raise ValueError("payload.texts must be a list of strings")
    return cast("list[str]", texts)


def _require_query(task: Task) -> str:
    query = task.payload.get("query")
    if not isinstance(query, str):
        raise ValueError("payload.query must be a string")
    return query


def _require_k(task: Task) -> int:
    k = task.payload.get("k", _DEFAULT_K)
    if not isinstance(k, int):
        raise ValueError("payload.k must be an int")
    return k
