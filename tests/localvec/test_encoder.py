"""Encoder is a structural Protocol; TextEncoder is the sentence-transformers impl.

The model is always faked here: a real ``SentenceTransformer(...)`` would download
weights from the Hub, which a unit test must never do. ``_FakeModel`` records the
``model_name`` positional and ``token`` kwarg it was constructed with so tests can
assert how ``TextEncoder`` resolves config, and offers ``get_embedding_dimension``
+ ``encode`` so the normalization tests keep exercising the real ``TextEncoder`` code.

Because the fake stands in for the download, **nothing in this file can observe whether
EdgeProc would have used the network** — that blind spot is why a false "works offline"
claim shipped green. The network property is tested in ``test_offline_model.py`` against
a cold cache; these tests own encoder *behavior* and grant download permission explicitly
so they exercise the hub-reference path rather than the fail-closed refusal.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from edgeproc.core.settings import DEFAULT_MODEL
from edgeproc.localvec.encoder import Encoder, TextEncoder

from ._fakes import FakeEncoder

_DIM = 3


class _FakeModel:
    """Stand-in for SentenceTransformer that records its constructor args."""

    last_model_name: str | None = None
    last_token: str | None = None
    last_local_files_only: bool | None = None
    last_saved_to: str | None = None

    def __init__(
        self, model_name: str, token: str | None = None, local_files_only: bool = False
    ) -> None:
        type(self).last_model_name = model_name
        type(self).last_token = token
        type(self).last_local_files_only = local_files_only

    def get_embedding_dimension(self) -> int:
        return _DIM

    def encode(
        self, texts: list[str], *, convert_to_numpy: bool, normalize_embeddings: bool
    ) -> np.ndarray:
        rows = np.ones((len(texts), _DIM), dtype=np.float32)
        return rows / np.linalg.norm(rows, axis=1, keepdims=True)

    def save(self, path: str) -> None:
        type(self).last_saved_to = path


@pytest.fixture(autouse=True)
def _fake_sentence_transformer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("edgeproc.localvec.encoder.SentenceTransformer", _FakeModel)
    # These tests exercise the hub-reference path, so they must opt in to it: without
    # permission TextEncoder now refuses before the fake is ever reached. Clearing
    # EDGEPROC_MODEL_PATH keeps a developer's own .env from redirecting them.
    monkeypatch.setenv("EDGEPROC_ALLOW_MODEL_DOWNLOAD", "1")
    monkeypatch.delenv("EDGEPROC_MODEL_PATH", raising=False)
    _FakeModel.last_model_name = None
    _FakeModel.last_token = None
    _FakeModel.last_local_files_only = None


def test_fake_encoder_satisfies_the_protocol() -> None:
    assert isinstance(FakeEncoder(), Encoder)


def test_text_encoder_satisfies_the_protocol() -> None:
    assert isinstance(TextEncoder(), Encoder)


def test_text_encoder_produces_normalized_float32_matrix() -> None:
    encoder = TextEncoder()
    vectors = encoder.encode_texts(["red running shoes", "blue hiking boots"])
    assert vectors.shape == (2, encoder.dim)
    assert vectors.dtype == np.float32
    assert np.allclose(np.linalg.norm(vectors, axis=1), 1.0, atol=1e-3)


def test_text_encoder_query_is_a_single_vector() -> None:
    encoder = TextEncoder()
    vector = encoder.encode_query("red running shoes")
    assert vector.shape == (encoder.dim,)
    assert vector.dtype == np.float32


def test_default_construction_uses_settings_model_and_env_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HF_TOKEN", "hf_secret")
    TextEncoder()
    assert _FakeModel.last_model_name == DEFAULT_MODEL
    assert _FakeModel.last_token == "hf_secret"  # noqa: S105 - test token, not a real secret


def test_explicit_args_override_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HF_TOKEN", "hf_secret")
    TextEncoder(model_name="custom/model", token="explicit_token")  # noqa: S106 - test token
    assert _FakeModel.last_model_name == "custom/model"
    assert _FakeModel.last_token == "explicit_token"  # noqa: S105 - test token, not a secret


def test_save_writes_the_model_where_a_bundle_can_pick_it_up(tmp_path: Path) -> None:
    """Provisioning: without this the fail-closed default would have no supported escape.

    A build machine fetches once and saves the weights beside the index so `publish` can
    chunk and sign them; the device then loads that directory and never calls the hub.
    """
    destination = tmp_path / "model"

    TextEncoder().save(destination)

    assert _FakeModel.last_saved_to == str(destination)


class _FakeModelWithNoDimension:
    """Stand-in for a model that cannot report its own embedding dimension."""

    def __init__(
        self, model_name: str, token: str | None = None, local_files_only: bool = False
    ) -> None:
        pass

    def get_embedding_dimension(self) -> int | None:
        return None


def test_dim_raises_when_model_exposes_no_embedding_dimension(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A model that cannot report its dimension must not silently hand one back: the
    # caller would build a FAISS index of the wrong size and have no signal why.
    monkeypatch.setattr("edgeproc.localvec.encoder.SentenceTransformer", _FakeModelWithNoDimension)
    encoder = TextEncoder()
    with pytest.raises(RuntimeError, match="no embedding dimension"):
        _ = encoder.dim
