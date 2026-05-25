"""BM25 keyword search, lifted from edge-reco and decoupled from any domain model.

edge-reco's ``KeywordSearcher.build(products)`` is split here: the generic core is
``from_texts(texts, ids)``; the reco-specific projection (title + tags + brand →
text) stays in the consumer.
"""

from __future__ import annotations

from rank_bm25 import BM25Okapi


def _tokenize(text: str) -> list[str]:
    return text.lower().split()


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

    def search(self, query: str, *, k: int = 10) -> list[tuple[str, float]]:
        if not query.strip() or not self._ids:
            return []
        scores = self._bm25.get_scores(_tokenize(query))
        ranked = sorted(enumerate(scores), key=lambda item: item[1], reverse=True)
        return [(self._ids[idx], float(score)) for idx, score in ranked[:k] if score > 0]
