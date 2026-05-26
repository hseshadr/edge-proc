"""load_local_runtime wires a saved index + an encoder into a LocalVecRuntime.

This is the bundle/index → runtime adapter the CLI `route` command needs: a
persisted FaissVectorIndex on disk plus an encoder becomes a registrable runtime.
Uses FakeEncoder (dim 6) so no model download is needed.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from shared_libs_python.vector_mgmt.core.types import IndexConfig, VectorEmbedding

from edgeproc.core.models import PrivacyMode, Task, TaskKind
from edgeproc.localvec.faiss_index import FaissVectorIndex
from edgeproc.localvec.loader import load_local_runtime

from ._fakes import FakeEncoder


async def _save_catalog(directory: Path) -> None:
    encoder = FakeEncoder()
    index = FaissVectorIndex("catalog", IndexConfig(dimension=encoder.dim))
    ids = ["p1", "p2", "p3", "p4"]
    texts = ["red shoes", "blue boots", "green dress", "red shoes"]
    vectors = encoder.encode_texts(texts)
    await index.insert(
        [
            VectorEmbedding(entity_id=entity_id, embedding=vector.tolist())
            for entity_id, vector in zip(ids, vectors, strict=True)
        ]
    )
    index.save(directory)


class _Dim3Encoder:
    """Reports a dimension that won't match the saved index — for the mismatch test."""

    @property
    def dim(self) -> int:
        return 3

    def encode_texts(self, texts: list[str]) -> object:  # pragma: no cover - never called
        raise NotImplementedError

    def encode_query(self, query: str) -> object:  # pragma: no cover - never called
        raise NotImplementedError


async def test_load_local_runtime_returns_a_searchable_runtime(tmp_path: Path) -> None:
    index_dir = tmp_path / "idx"
    await _save_catalog(index_dir)

    runtime = load_local_runtime(index_dir, encoder=FakeEncoder(), index_name="catalog")

    assert runtime.name == "localvec"
    task = Task(
        kind=TaskKind.SEARCH,
        payload={"query": "red shoes", "k": 2},
        privacy_mode=PrivacyMode.LOCAL_ONLY,
    )
    envelope = await runtime.execute(task)
    assert envelope.success
    assert envelope.payload["results"]


async def test_load_local_runtime_rejects_encoder_dimension_mismatch(tmp_path: Path) -> None:
    index_dir = tmp_path / "idx"
    await _save_catalog(index_dir)

    with pytest.raises(ValueError, match="dim"):
        load_local_runtime(index_dir, encoder=_Dim3Encoder(), index_name="catalog")


def test_load_local_runtime_missing_directory_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_local_runtime(tmp_path / "absent", encoder=FakeEncoder(), index_name="catalog")
