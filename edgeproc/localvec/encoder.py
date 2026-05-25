"""Text embedding. ``Encoder`` is the seam; ``TextEncoder`` is the real impl.

edge-reco's ``ProductEncoder`` baked the reco text-projection (title + tags +
brand) into ``encode``. Here the projection is the consumer's job: ``TextEncoder``
encodes plain ``list[str]``, so any consumer can supply its own text view.
"""

from __future__ import annotations

from typing import Protocol, cast, runtime_checkable

import numpy as np
from numpy.typing import NDArray
from sentence_transformers import SentenceTransformer

_DEFAULT_MODEL = "sentence-transformers/all-MiniLM-L6-v2"


@runtime_checkable
class Encoder(Protocol):
    """Turns text into L2-normalized float32 embeddings."""

    @property
    def dim(self) -> int: ...

    def encode_texts(self, texts: list[str]) -> NDArray[np.float32]: ...

    def encode_query(self, query: str) -> NDArray[np.float32]: ...


class TextEncoder:
    """sentence-transformers encoder producing normalized float32 vectors."""

    def __init__(self, model_name: str = _DEFAULT_MODEL) -> None:
        self._model = SentenceTransformer(model_name)

    @property
    def dim(self) -> int:
        return int(self._model.get_embedding_dimension())

    def encode_texts(self, texts: list[str]) -> NDArray[np.float32]:
        embeddings = self._model.encode(texts, convert_to_numpy=True, normalize_embeddings=True)
        return cast(NDArray[np.float32], embeddings.astype(np.float32))

    def encode_query(self, query: str) -> NDArray[np.float32]:
        embeddings = self._model.encode([query], convert_to_numpy=True, normalize_embeddings=True)
        return cast(NDArray[np.float32], embeddings[0].astype(np.float32))
