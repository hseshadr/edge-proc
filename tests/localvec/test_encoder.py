"""Encoder is a structural Protocol; TextEncoder is the sentence-transformers impl.

The model is always faked here: a real ``SentenceTransformer(...)`` would download
weights from the Hub, which a unit test must never do. ``_FakeModel`` records the
``model_name`` positional and ``token`` kwarg it was constructed with so tests can
assert how ``TextEncoder`` resolves config, and offers ``get_embedding_dimension``
+ ``encode`` so the normalization tests keep exercising the real ``TextEncoder`` code.
"""

from __future__ import annotations

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

    def __init__(self, model_name: str, token: str | None = None) -> None:
        type(self).last_model_name = model_name
        type(self).last_token = token

    def get_embedding_dimension(self) -> int:
        return _DIM

    def encode(
        self, texts: list[str], *, convert_to_numpy: bool, normalize_embeddings: bool
    ) -> np.ndarray:
        rows = np.ones((len(texts), _DIM), dtype=np.float32)
        return rows / np.linalg.norm(rows, axis=1, keepdims=True)


@pytest.fixture(autouse=True)
def _fake_sentence_transformer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("edgeproc.localvec.encoder.SentenceTransformer", _FakeModel)
    _FakeModel.last_model_name = None
    _FakeModel.last_token = None


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
