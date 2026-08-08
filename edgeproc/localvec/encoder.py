"""Text embedding. ``Encoder`` is the seam; ``TextEncoder`` is the real impl.

The projection from a domain object to a text view (title + tags + brand for
products, body + headings for documents, etc.) is the consumer's job —
``TextEncoder`` encodes plain ``list[str]``, so any consumer can supply its own.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, cast, runtime_checkable

import numpy as np
from numpy.typing import NDArray
from sentence_transformers import SentenceTransformer

from edgeproc.core.settings import EdgeProcSettings
from edgeproc.localvec.model_source import resolve_model_source


@runtime_checkable
class Encoder(Protocol):
    """Turns text into L2-normalized float32 embeddings.

    Encoding is L2-normalized float32 so inner product equals cosine similarity — the
    FAISS ``IndexFlatIP`` the runtime builds depends on that normalization.
    """

    @property
    def dim(self) -> int:
        """Embedding dimensionality; an index built for this encoder must match it."""
        ...

    def encode_texts(self, texts: list[str]) -> NDArray[np.float32]:
        """Encode a batch of documents into one normalized row vector each.

        RAISES (does not return an empty array) if the model exposes no embedding
        dimension; the caller never silently indexes a malformed encoder.
        """
        ...

    def encode_query(self, query: str) -> NDArray[np.float32]:
        """Encode a single query string into one normalized vector (same space as docs)."""
        ...


class _HasEmbeddingDimension(Protocol):
    """Structural view of the SentenceTransformer embedding-dimension accessor.

    sentence-transformers 5.x ships ``get_embedding_dimension`` at runtime but
    omits it from its type stubs, so this Protocol restores the real signature.
    """

    def get_embedding_dimension(self) -> int | None: ...


class TextEncoder:
    """sentence-transformers encoder producing normalized float32 vectors.

    Fail-closed about the network: construction resolves the model through
    :func:`~edgeproc.localvec.model_source.resolve_model_source`, which refuses before
    any loader exists unless a local ``model_path`` is configured or a deploy has
    explicitly permitted a download. See that module for why the refusal must come first.
    """

    def __init__(
        self,
        model_name: str | None = None,
        token: str | None = None,
        model_path: Path | None = None,
    ) -> None:
        settings = EdgeProcSettings()
        source = resolve_model_source(settings, model_name, model_path)
        self._model = SentenceTransformer(
            source.reference,
            token=token or settings.hf_token,
            local_files_only=source.local_files_only,
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

    def save(self, path: Path) -> None:
        """Write the loaded model to ``path`` so it can ship inside a signed bundle.

        This is the provisioning half of the offline story, and the reason a fail-closed
        default is workable: fetch once on a build machine, save the weights beside the
        index, publish both, and the device is handed a model it never has to go get.
        Point ``EDGEPROC_MODEL_PATH`` at what ``sync --materialize-to`` wrote.
        """
        self._model.save(str(path))
