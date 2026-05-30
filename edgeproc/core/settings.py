"""Deploy-time configuration for EdgeProc, read from env / ``.env``.

One source of truth for tunables previously hardcoded (and in one case duplicated)
across modules: the default embedding model, default top-k, the bundle HTTP timeout,
and the Hugging Face auth token. Lives in ``core`` because both ``localvec`` (model,
token, k) and ``bundles`` (http timeout) consume it, so it must not sit behind an extra.

A library reads config lazily: construct ``EdgeProcSettings()`` where a default is
actually needed, never at import time. Env vars use the ``EDGEPROC_`` prefix
(``EDGEPROC_MODEL_NAME``, ``EDGEPROC_DEFAULT_K``, ``EDGEPROC_HTTP_TIMEOUT``,
``EDGEPROC_TRUST_ROOT_PUBKEY_PATH`` — the pinned sync trust-root key); the token uses
the ecosystem-standard ``HF_TOKEN`` so it drops in beside the rest of the HF stack.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_MODEL: Final[str] = "sentence-transformers/all-MiniLM-L6-v2"


class EdgeProcSettings(BaseSettings):
    """EdgeProc runtime config. Unknown fields are rejected (fail closed)."""

    model_config = SettingsConfigDict(
        env_prefix="EDGEPROC_",
        env_file=".env",
        extra="forbid",
        protected_namespaces=(),
    )

    model_name: str = DEFAULT_MODEL
    hf_token: str | None = Field(default=None, validation_alias="HF_TOKEN")
    default_k: int = 10
    http_timeout: float = 30.0
    # Per-task resource budgets; the source of truth for the Task model's defaults.
    task_budget_ms: int = 5000
    task_budget_memory_mb: int = 256
    # RRF rank-window constant — bigger k flattens the score curve (fewer top-rank wins).
    rrf_k_window: int = 60
    # Pinned TUF-style trust-root public key; a `sync` with none set is refused (fail-closed).
    trust_root_pubkey_path: Path | None = None
