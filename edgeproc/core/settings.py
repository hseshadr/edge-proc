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
    """EdgeProc runtime config, scoped to the ``EDGEPROC_`` env prefix.

    ``extra="ignore"``: as a *library*, EdgeProc must coexist with a host app's
    own ``.env`` (e.g. a consumer's ``DATABASE_URL`` / ``OPENROUTER_API_KEY``).
    The ``EDGEPROC_`` prefix already scopes which vars bind here, so non-prefixed
    host keys are ignored rather than rejected — forbidding them would make any
    consumer with a populated ``.env`` crash on ``EdgeProcSettings()``.
    """

    model_config = SettingsConfigDict(
        env_prefix="EDGEPROC_",
        env_file=".env",
        extra="ignore",
        protected_namespaces=(),
    )

    model_name: str = DEFAULT_MODEL
    hf_token: str | None = Field(default=None, validation_alias="HF_TOKEN")
    default_k: int = 10
    http_timeout: float = 30.0
    # Fail-closed resource ceilings for the sync substrate (bomb / unbounded-read defense).
    # A single chunk's plaintext is <=256 KiB (chunker MAX_SIZE), so 64 MiB is huge headroom
    # that never rejects a legit chunk yet refuses a zstd bomb before it exhausts memory.
    max_decompressed_bytes: int = 64 * 1024 * 1024
    # A single HTTP fetch (pointer/manifest/chunk); 256 MiB bounds a hostile origin's body.
    max_fetch_bytes: int = 256 * 1024 * 1024
    # Per-task resource budgets; the source of truth for the Task model's defaults.
    task_budget_ms: int = 5000
    task_budget_memory_mb: int = 256
    # RRF rank-window constant — bigger k flattens the score curve (fewer top-rank wins).
    rrf_k_window: int = 60
    # Pinned TUF-style trust-root public key; a `sync` with none set is refused (fail-closed).
    trust_root_pubkey_path: Path | None = None
