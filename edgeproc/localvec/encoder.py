"""Text embedding. ``Encoder`` is the seam; ``TextEncoder`` is the real impl.

The projection from a domain object to a text view (title + tags + brand for
products, body + headings for documents, etc.) is the consumer's job —
``TextEncoder`` encodes plain ``list[str]``, so any consumer can supply its own.
"""

from __future__ import annotations

from typing import Protocol, cast, runtime_checkable

import numpy as np
from numpy.typing import NDArray
from sentence_transformers import SentenceTransformer

from edgeproc.core.settings import EdgeProcSettings


@runtime_checkable
class Encoder(Protocol):
    """Turns text into L2-normalized float32 embeddings."""

    @property
    def dim(self) -> int: ...

    def encode_texts(self, texts: list[str]) -> NDArray[np.float32]: ...

    def encode_query(self, query: str) -> NDArray[np.float32]: ...


class _HasEmbeddingDimension(Protocol):
    """Structural view of the SentenceTransformer embedding-dimension accessor.

    sentence-transformers 5.x ships ``get_embedding_dimension`` at runtime but
    omits it from its type stubs, so this Protocol restores the real signature.
    """

    def get_embedding_dimension(self) -> int | None: ...


class TextEncoder:
    """sentence-transformers encoder producing normalized float32 vectors."""

    def __init__(self, model_name: str | None = None, token: str | None = None) -> None:
        settings = EdgeProcSettings()
        self._model = SentenceTransformer(
            model_name or settings.model_name, token=token or settings.hf_token
        )

    @property
    def dim(self) -> int:
        # sentence-transformers 5.x renamed get_sentence_embedding_dimension ->
        # get_embedding_dimension, but the new name is not yet in its type stubs, so
        # mypy resolves the attribute through nn.Module.__getattr__ to Tensor | Module.
        # Cast the model to a narrow structural type that declares the real method.
        model = cast("_HasEmbeddingDimension", self._model)
        embedding_dim = model.get_embedding_dimension()
        if embedding_dim is None:
            raise RuntimeError("SentenceTransformer model exposes no embedding dimension")
        return embedding_dim

    def encode_texts(self, texts: list[str]) -> NDArray[np.float32]:
        embeddings = self._model.encode(texts, convert_to_numpy=True, normalize_embeddings=True)
        return cast(NDArray[np.float32], embeddings.astype(np.float32))

    def encode_query(self, query: str) -> NDArray[np.float32]:
        embeddings = self._model.encode([query], convert_to_numpy=True, normalize_embeddings=True)
        return cast(NDArray[np.float32], embeddings[0].astype(np.float32))
