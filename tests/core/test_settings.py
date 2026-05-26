"""EdgeProcSettings reads deploy-time config from env / .env and fails closed on junk."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from edgeproc.core.settings import DEFAULT_MODEL, EdgeProcSettings

_VARS = ("EDGEPROC_MODEL_NAME", "EDGEPROC_DEFAULT_K", "EDGEPROC_HTTP_TIMEOUT", "HF_TOKEN")


def test_defaults_match_the_canonical_local_stack(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in _VARS:
        monkeypatch.delenv(var, raising=False)

    settings = EdgeProcSettings(_env_file=None)

    assert settings.model_name == DEFAULT_MODEL
    assert settings.hf_token is None
    assert settings.default_k == 10
    assert settings.http_timeout == 30.0


def test_env_overrides_are_read_with_the_edgeproc_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EDGEPROC_MODEL_NAME", "BAAI/bge-small-en")
    monkeypatch.setenv("EDGEPROC_DEFAULT_K", "5")
    monkeypatch.setenv("EDGEPROC_HTTP_TIMEOUT", "12.5")

    settings = EdgeProcSettings(_env_file=None)

    assert settings.model_name == "BAAI/bge-small-en"
    assert settings.default_k == 5
    assert settings.http_timeout == 12.5


def test_hf_token_reads_the_unprefixed_ecosystem_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HF_TOKEN", "hf_secret_value")

    assert EdgeProcSettings(_env_file=None).hf_token == "hf_secret_value"  # noqa: S105


def test_unknown_field_fails_closed() -> None:
    with pytest.raises(ValidationError):
        EdgeProcSettings(_env_file=None, totally_unknown="x")
