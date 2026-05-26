"""Wire a persisted index plus an encoder into a registrable ``LocalVecRuntime``.

This is the bundle/index → runtime adapter: ``FaissVectorIndex.load`` reloads an
index written by :meth:`FaissVectorIndex.save` (a synced bundle cache dir is exactly
such a directory), and ``load_local_runtime`` pairs it with an encoder. The encoder
is injected — the caller owns model selection and lifetime — so the runtime can be
assembled without this module deciding how text gets embedded.
"""

from __future__ import annotations

from pathlib import Path

from edgeproc.localvec.encoder import Encoder
from edgeproc.localvec.faiss_index import FaissVectorIndex
from edgeproc.localvec.runtime import LocalVecRuntime


def load_local_runtime(index_dir: Path, *, encoder: Encoder, index_name: str) -> LocalVecRuntime:
    """Load a saved index from ``index_dir`` and pair it with ``encoder``.

    Fails closed: a missing index raises ``FileNotFoundError``; an encoder whose
    output dimension won't match the stored vectors raises ``ValueError`` — before
    any query is ever embedded.
    """
    index = FaissVectorIndex.load(index_name, index_dir)
    _check_dimension(encoder, index)
    return LocalVecRuntime(encoder, index)


def _check_dimension(encoder: Encoder, index: FaissVectorIndex) -> None:
    if encoder.dim != index.config.dimension:
        raise ValueError(f"encoder dim {encoder.dim} != index dim {index.config.dimension}")
