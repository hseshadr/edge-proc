"""Reciprocal Rank Fusion for combining keyword and vector result lists.

A pure function over ``(id, score)`` tuples, generic to any two ranked lists.
"""

from __future__ import annotations

from edgeproc.core.settings import EdgeProcSettings


def _resolve_window(k: int | None) -> int:
    # The rank window defaults to the single source of truth in EdgeProcSettings.
    return EdgeProcSettings().rrf_k_window if k is None else k


def reciprocal_rank_fusion(
    keyword_results: list[tuple[str, float]],
    vector_results: list[tuple[str, float]],
    *,
    k: int | None = None,
) -> list[tuple[str, float]]:
    """rrf_score(doc) = sum(1 / (k + rank_i)) over each list containing doc.

    ``k`` (the rank window) defaults to ``EdgeProcSettings().rrf_k_window`` when not
    given — the single source of truth — but an explicit value always wins.
    """
    k = _resolve_window(k)
    rrf_scores: dict[str, float] = {}
    for rank, (doc_id, _score) in enumerate(keyword_results):
        rrf_scores[doc_id] = rrf_scores.get(doc_id, 0.0) + 1.0 / (k + rank + 1)
    for rank, (doc_id, _score) in enumerate(vector_results):
        rrf_scores[doc_id] = rrf_scores.get(doc_id, 0.0) + 1.0 / (k + rank + 1)
    return sorted(rrf_scores.items(), key=lambda x: x[1], reverse=True)
