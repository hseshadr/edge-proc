"""EdgeProcSettings reads deploy-time config from env / .env under the EDGEPROC_ prefix."""

from __future__ import annotations

import pytest

from edgeproc.core.settings import DEFAULT_MODEL, EdgeProcSettings

_VARS = (
    "EDGEPROC_MODEL_NAME",
    "EDGEPROC_DEFAULT_K",
    "EDGEPROC_HTTP_TIMEOUT",
    "EDGEPROC_TASK_BUDGET_MS",
    "EDGEPROC_TASK_BUDGET_MEMORY_MB",
    "EDGEPROC_RRF_K_WINDOW",
    "HF_TOKEN",
)


def test_defaults_match_the_canonical_local_stack(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in _VARS:
        monkeypatch.delenv(var, raising=False)

    settings = EdgeProcSettings(_env_file=None)

    assert settings.model_name == DEFAULT_MODEL
    assert settings.hf_token is None
    assert settings.default_k == 10
    assert settings.http_timeout == 30.0
    assert settings.task_budget_ms == 5000
    assert settings.task_budget_memory_mb == 256
    assert settings.rrf_k_window == 60


def test_env_overrides_are_read_with_the_edgeproc_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EDGEPROC_MODEL_NAME", "BAAI/bge-small-en")
    monkeypatch.setenv("EDGEPROC_DEFAULT_K", "5")
    monkeypatch.setenv("EDGEPROC_HTTP_TIMEOUT", "12.5")
    monkeypatch.setenv("EDGEPROC_TASK_BUDGET_MS", "9000")
    monkeypatch.setenv("EDGEPROC_TASK_BUDGET_MEMORY_MB", "512")
    monkeypatch.setenv("EDGEPROC_RRF_K_WINDOW", "30")

    settings = EdgeProcSettings(_env_file=None)

    assert settings.model_name == "BAAI/bge-small-en"
    assert settings.default_k == 5
    assert settings.http_timeout == 12.5
    assert settings.task_budget_ms == 9000
    assert settings.task_budget_memory_mb == 512
    assert settings.rrf_k_window == 30


def test_hf_token_reads_the_unprefixed_ecosystem_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HF_TOKEN", "hf_secret_value")

    assert EdgeProcSettings(_env_file=None).hf_token == "hf_secret_value"  # noqa: S105


def test_ignores_host_app_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    # As a library, EdgeProc must coexist with a consumer's own .env: non-EDGEPROC_
    # host vars (e.g. a DATABASE_URL / OPENROUTER_API_KEY) are ignored, not
    # rejected — forbidding them would crash EdgeProcSettings() in any app with a
    # populated .env. The EDGEPROC_ prefix already scopes what binds here.
    monkeypatch.setenv("DATABASE_URL", "postgresql://localhost/app")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-host-app-key")  # noqa: S105

    settings = EdgeProcSettings(_env_file=None)

    assert settings.model_name == DEFAULT_MODEL
