"""Deploy-time configuration for EdgeProc, read from env / ``.env``.

One source of truth for tunables previously hardcoded (and in one case duplicated)
across modules: the default embedding model, default top-k, the bundle HTTP timeout,
and the Hugging Face auth token. Lives in ``core`` because both ``localvec`` (model,
token, k) and ``bundles`` (http timeout) consume it, so it must not sit behind an extra.

A library reads config lazily: construct ``EdgeProcSettings()`` where a default is
actually needed, never at import time. Env vars use the ``EDGEPROC_`` prefix
(``EDGEPROC_MODEL_NAME``, ``EDGEPROC_DEFAULT_K``, ``EDGEPROC_HTTP_TIMEOUT``,
``EDGEPROC_TRUST_ROOT_PUBKEY_PATH`` — the pinned sync trust root, a key or a keyring); the
token uses
the ecosystem-standard ``HF_TOKEN`` so it drops in beside the rest of the HF stack.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated, Final

from pydantic import BeforeValidator, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_MODEL: Final[str] = "sentence-transformers/all-MiniLM-L6-v2"

_DURATION: Final = re.compile(r"([0-9]+)([smhdw]?)")
_UNIT_SECONDS: Final[dict[str, int]] = {
    "": 1,
    "s": 1,
    "m": 60,
    "h": 3600,
    "d": 86_400,
    "w": 604_800,
}
#: A signed ``expires_at = now + duration`` must stay below 2**53 (JSON-safe on every
#: runtime). Capping the duration at 2**52 seconds keeps the sum safe for ~142M years.
_MAX_DURATION_SECONDS: Final = 2**52


def parse_duration(text: str) -> int:
    """Parse a positive duration: bare seconds (``3600``) or one unit (``90s 30m 12h 7d 2w``).

    ASCII digits only, one lowercase unit, no fractions or signs: a pointer lifetime has
    exactly one spelling, and anything else is refused rather than guessed at.
    """
    match = _DURATION.fullmatch(text.strip())
    if match is None:
        raise ValueError(f"invalid duration {text!r}: use seconds or <n>s|m|h|d|w")
    seconds = int(match.group(1)) * _UNIT_SECONDS[match.group(2)]
    if not 0 < seconds <= _MAX_DURATION_SECONDS:
        raise ValueError(f"invalid duration {text!r}: must be positive and at most 2**52 seconds")
    return seconds


def _duration_or_none(value: object) -> object:
    """Settings adapter: a duration string becomes seconds; ints pass to strict validation."""
    return parse_duration(value) if isinstance(value, str) else value


type DurationSeconds = Annotated[
    int, BeforeValidator(_duration_or_none), Field(strict=True, gt=0, le=_MAX_DURATION_SECONDS)
]


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
    # The offline path for the embedding model. `sync` ships the index; without a local
    # model the encoder would resolve `model_name` by calling the hub, so "needs no
    # network after one sync" would be false. Point this at what `--materialize-to` wrote.
    model_path: Path | None = None
    # Pins a model that did NOT arrive through `sync` (which already verifies its payload
    # against the trust root). Unset = no check; set = a mismatch is refused, not warned.
    model_digest: str | None = None
    # Fail-closed: the encoder never reaches the network unless a deploy explicitly says
    # it may. Provisioning on a build machine is the legitimate use; a device query is not.
    allow_model_download: bool = False
    default_k: int = 10
    http_timeout: float = 30.0
    # Fail-closed resource ceilings for the sync substrate (bomb / unbounded-read defense).
    # A single chunk's plaintext is <=256 KiB (chunker MAX_SIZE), so 64 MiB is huge headroom
    # that never rejects a legit chunk yet refuses a zstd bomb before it exhausts memory.
    max_decompressed_bytes: int = 64 * 1024 * 1024
    # A single HTTP fetch (pointer/manifest/chunk); 256 MiB bounds a hostile origin's body.
    max_fetch_bytes: int = 256 * 1024 * 1024
    # AGGREGATE per-sync ceilings (disk-exhaustion defense): a hostile/runaway manifest
    # can enumerate unbounded chunks/files, so one sync refuses to pull past these. Generous
    # enough never to reject a legit bundle; a single sync over 4 GiB or 100k files is refused.
    max_sync_total_bytes: int = 4 * 1024 * 1024 * 1024
    max_sync_files: int = 100_000
    # A caller asking to materialize one file receives bytes, so keep that explicit
    # allocation bounded even when a signed manifest contains many chunks.
    max_materialize_bytes: int = 256 * 1024 * 1024
    # Sum of declared task reservations admitted concurrently by one EdgeProc instance.
    # This is deterministic admission control, not a portable native-RSS hard limit.
    max_in_flight_memory_mb: int = 512
    # Cross-process filesystem mutation lock. A wedged peer fails retryably instead of
    # making sync/promote/GC wait forever.
    mutation_lock_timeout: float = 30.0
    # Cross-process vector snapshot lock. Save/load/migration/GC form one boundary;
    # a wedged writer fails after this wait instead of blocking an application forever.
    snapshot_lock_timeout: float = 30.0
    # Per-task resource budgets; the source of truth for the Task model's defaults.
    task_budget_ms: int = 5000
    task_budget_memory_mb: int = 256
    # RRF rank-window constant — bigger k flattens the score curve (fewer top-rank wins).
    rrf_k_window: int = 60
    # Pinned trust root: a raw 32-byte `public.key` (a keyring of one) or a JSON keyring
    # (`edgeproc.keyring/v1`). A `sync` with none set is refused (fail-closed).
    trust_root_pubkey_path: Path | None = None
    # Publisher stamping, OFF by default: an older consumer's pointer model forbids unknown
    # fields, so upgrade every consumer before turning either on. `publish_stamp_key_id`
    # signs the key's `key_id` into the pointer; `publish_expires_in` (seconds, or 90s /
    # 30m / 12h / 7d / 2w) signs `expires_at = now + duration`.
    publish_stamp_key_id: bool = False
    publish_expires_in: DurationSeconds | None = None
