"""LocalVecRuntime serves EMBED/SEARCH/RANK and fails closed off the local path."""

from __future__ import annotations

from shared_libs_python.vector_mgmt.core.types import IndexConfig, VectorEmbedding

from edgeproc.core.models import CapabilityVerdict, PrivacyMode, Task, TaskKind
from edgeproc.core.protocols import Runtime
from edgeproc.localvec.faiss_index import FaissVectorIndex
from edgeproc.localvec.runtime import LocalVecRuntime
from edgeproc.localvec.searcher import KeywordSearcher

from ._fakes import FakeEncoder

_TEXTS = ["red shoes", "blue boots", "green dress"]
_IDS = ["p1", "p2", "p3"]


async def _populated_runtime() -> LocalVecRuntime:
    encoder = FakeEncoder()
    index = FaissVectorIndex("products", IndexConfig(dimension=encoder.dim))
    vectors = encoder.encode_texts(_TEXTS)
    await index.insert(
        [
            VectorEmbedding(entity_id=i, embedding=v.tolist())
            for i, v in zip(_IDS, vectors, strict=True)
        ]
    )
    keyword = KeywordSearcher.from_texts(_TEXTS, _IDS)
    return LocalVecRuntime(encoder=encoder, index=index, keyword=keyword)


def _task(kind: TaskKind, privacy: PrivacyMode = PrivacyMode.LOCAL_ONLY, **payload: object) -> Task:
    return Task(kind=kind, payload=dict(payload), privacy_mode=privacy)


def test_is_a_runtime() -> None:
    runtime = LocalVecRuntime(encoder=FakeEncoder(), index=FaissVectorIndex("x"))
    assert isinstance(runtime, Runtime)
    assert runtime.name == "localvec"


def test_accepts_local_search_embed_rank() -> None:
    runtime = LocalVecRuntime(encoder=FakeEncoder(), index=FaissVectorIndex("x"))
    for kind in (TaskKind.EMBED, TaskKind.SEARCH, TaskKind.RANK):
        assert runtime.can_handle(_task(kind)) == CapabilityVerdict.ACCEPT


def test_rejects_unsupported_kind() -> None:
    runtime = LocalVecRuntime(encoder=FakeEncoder(), index=FaissVectorIndex("x"))
    assert runtime.can_handle(_task(TaskKind.GENERATE)) == CapabilityVerdict.REJECT_KIND


def test_rejects_non_local_privacy_no_silent_cloud() -> None:
    runtime = LocalVecRuntime(encoder=FakeEncoder(), index=FaissVectorIndex("x"))
    verdict = runtime.can_handle(_task(TaskKind.SEARCH, privacy=PrivacyMode.CLOUD_PREMIUM))
    assert verdict == CapabilityVerdict.REJECT_CAPABILITY


async def test_embed_returns_one_vector_per_text() -> None:
    runtime = await _populated_runtime()
    result = await runtime.execute(_task(TaskKind.EMBED, texts=["red shoes", "blue boots"]))
    assert result.success is True
    assert len(result.payload["embeddings"]) == 2


async def test_search_ranks_the_matching_document_first() -> None:
    runtime = await _populated_runtime()
    result = await runtime.execute(_task(TaskKind.SEARCH, query="red shoes", k=3))
    assert result.success is True
    assert result.payload["results"][0][0] == "p1"


async def test_rank_fuses_keyword_and_vector() -> None:
    runtime = await _populated_runtime()
    result = await runtime.execute(_task(TaskKind.RANK, query="red shoes", k=3))
    assert result.success is True
    assert result.payload["results"][0][0] == "p1"


async def test_bad_payload_fails_closed_as_envelope() -> None:
    runtime = await _populated_runtime()
    result = await runtime.execute(_task(TaskKind.SEARCH))  # missing "query"
    assert result.success is False
    assert result.error is not None
    assert result.runtime_used == "localvec"


async def test_rank_without_keyword_searcher_fails_closed() -> None:
    runtime = LocalVecRuntime(encoder=FakeEncoder(), index=FaissVectorIndex("x"))
    result = await runtime.execute(_task(TaskKind.RANK, query="red"))
    assert result.success is False
    assert "keyword searcher" in (result.error or "")


async def test_embed_with_non_string_texts_fails_closed() -> None:
    runtime = await _populated_runtime()
    result = await runtime.execute(_task(TaskKind.EMBED, texts=[1, 2, 3]))
    assert result.success is False
    assert "texts" in (result.error or "")


async def test_non_int_k_fails_closed() -> None:
    runtime = await _populated_runtime()
    result = await runtime.execute(_task(TaskKind.SEARCH, query="red shoes", k="lots"))
    assert result.success is False
    assert "k" in (result.error or "")
