"""Encoder is a structural Protocol; TextEncoder is the sentence-transformers impl."""

from __future__ import annotations

import numpy as np

from edgeproc.localvec.encoder import Encoder, TextEncoder

from ._fakes import FakeEncoder


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
