"""BM25 keyword search, decoupled from any domain model.

The core takes plain ``(texts, ids)``: any domain-specific projection — products to
title+tags+brand, documents to body+headings, posts to author+content, etc. — stays
in the consumer. This keeps the searcher reusable across catalogues.
"""

from __future__ import annotations

from rank_bm25 import BM25Okapi

from edgeproc.core.settings import EdgeProcSettings


def _tokenize(text: str) -> list[str]:
    return text.lower().split()


def _resolve_k(k: int | None) -> int:
    # Default top-k flows from EdgeProcSettings (one source of truth, no second literal).
    return EdgeProcSettings().default_k if k is None else k


class KeywordSearcher:
    """BM25 over a fixed corpus of token lists, addressed by parallel ids."""

    def __init__(self, bm25: BM25Okapi, ids: list[str]) -> None:
        self._bm25 = bm25
        self._ids = ids

    @classmethod
    def from_texts(cls, texts: list[str], ids: list[str]) -> KeywordSearcher:
        corpus = [_tokenize(text) for text in texts]
        bm25 = BM25Okapi(corpus) if corpus else BM25Okapi([[""]])
        return cls(bm25, list(ids))

    def search(self, query: str, *, k: int | None = None) -> list[tuple[str, float]]:
        top_k = _resolve_k(k)
        if not query.strip() or not self._ids:
            return []
        scores = self._bm25.get_scores(_tokenize(query))
        ranked = sorted(enumerate(scores), key=lambda item: item[1], reverse=True)
        return [(self._ids[idx], float(score)) for idx, score in ranked[:top_k] if score > 0]
