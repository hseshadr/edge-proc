"""Deterministic test doubles for the localvec runtime — no model download needed."""

from __future__ import annotations

import numpy as np
from numpy.typing import NDArray

_VOCAB = ("red", "blue", "green", "shoes", "boots", "dress")


def _vectorize(text: str) -> NDArray[np.float32]:
    counts = np.zeros(len(_VOCAB), dtype=np.float32)
    tokens = text.lower().split()
    for i, word in enumerate(_VOCAB):
        counts[i] = float(tokens.count(word))
    norm = float(np.linalg.norm(counts))
    return counts / norm if norm > 0 else counts


class FakeEncoder:
    """Bag-of-words encoder over a fixed vocabulary — fast, deterministic, offline."""

    @property
    def dim(self) -> int:
        return len(_VOCAB)

    def encode_texts(self, texts: list[str]) -> NDArray[np.float32]:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        return np.vstack([_vectorize(text) for text in texts])

    def encode_query(self, query: str) -> NDArray[np.float32]:
        return _vectorize(query)
