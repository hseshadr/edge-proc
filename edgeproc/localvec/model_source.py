"""Where the embedding model comes from — and the refusal when it is not already here.

EdgeProc's user-facing promise is that after one ``sync`` a device needs no network to
answer a query. That promise is about the *model* as much as the index. ``sync`` ships a
signed, content-addressed FAISS index; ``sentence-transformers``, handed a bare repo id,
resolves it by calling huggingface.co. On a machine with a warm Hub cache that fetch is
invisible — which is exactly why a false "works offline" claim could ship green.

So egress here is **opt-in, never a fallback**:

- A configured ``model_path`` is the offline path. Point it at the directory ``sync``
  materialized from the signed bundle and the model travels under the same pinned key as
  the rest of the payload.
- Without one, EdgeProc still refuses to fetch. It loads from local files if they are
  already present and otherwise raises :class:`ModelNotLocalError` naming the two fixes,
  rather than quietly opening a socket at the first query.
- ``model_digest`` pins a model that did *not* arrive through ``sync``, so a locally
  supplied directory is verified rather than merely trusted.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Final, Protocol

from edgeproc.core.settings import EdgeProcSettings
from edgeproc.errors import BUNDLE_INTEGRITY_FAILED, CONFIG_INVALID, CONFIG_MISSING

#: Read weights a megabyte at a time; a model shard is far too large to hold in memory.
_HASH_CHUNK: Final[int] = 1024 * 1024

#: Named in every refusal. A fail-closed guard an operator cannot act on is an outage.
REMEDY: Final[str] = (
    "Set EDGEPROC_MODEL_PATH to a local model directory (ship the model in the signed "
    "bundle and point at what `edgeproc sync --materialize-to` wrote), or set "
    "EDGEPROC_ALLOW_MODEL_DOWNLOAD=1 to permit a one-time fetch on a build machine."
)


class ModelSourceError(RuntimeError):
    """Base for every refusal to load an embedding model.

    Exists so one ``except`` at the CLI boundary renders all three as coded failures.
    Each subclass carries its own canonical code, which a consumer can turn into RFC 9457
    via :func:`edgeproc.errors.problem_details_for` — the base deliberately declares none.
    """


class ModelNotLocalError(ModelSourceError):
    """The model is not on disk and egress was not permitted (fail-closed)."""

    code: ClassVar[str] = CONFIG_MISSING


class ModelPathInvalidError(ModelSourceError):
    """``EDGEPROC_MODEL_PATH`` does not point at a readable directory."""

    code: ClassVar[str] = CONFIG_INVALID


class ModelDigestMismatchError(ModelSourceError):
    """The local model does not match its pinned digest — treated as a compromise."""

    code: ClassVar[str] = BUNDLE_INTEGRITY_FAILED


@dataclass(frozen=True, slots=True)
class ModelSource:
    """Resolved answer to "load what, and may it use the network?".

    ``reference`` is either a local directory path or a hub model id. ``local_files_only``
    is the un-bypassable half: when true the loader is pinned to files already on disk.
    """

    reference: str
    local_files_only: bool


class _Digest(Protocol):
    """The single ``hashlib`` method this module needs, without naming a private type."""

    def update(self, data: bytes, /) -> None: ...


def digest_model_dir(path: Path) -> str:
    """sha256 over a model directory: each file's relative path, then its bytes.

    Files are walked in sorted relative-path order so the digest is byte-identical across
    machines and filesystems. The path is hashed alongside the content, so swapping bytes
    between two files changes the digest instead of preserving it.
    """
    digest = hashlib.sha256()
    for file in sorted(item for item in path.rglob("*") if item.is_file()):
        digest.update(str(file.relative_to(path)).encode())
        _absorb(digest, file)
    return digest.hexdigest()


def _absorb(digest: _Digest, file: Path) -> None:
    """Feed one file into ``digest`` a chunk at a time."""
    with file.open("rb") as handle:
        while chunk := handle.read(_HASH_CHUNK):
            digest.update(chunk)


def resolve_model_source(
    settings: EdgeProcSettings,
    model_name: str | None = None,
    model_path: Path | None = None,
) -> ModelSource:
    """Decide where weights load from. Egress is opt-in; the default refuses it.

    The refusal is raised *here*, before any loader is constructed, and that ordering is
    the guard. Delegating "don't use the network" to a downstream ``local_files_only``
    flag would leave the decision inside a library whose transport we cannot observe:
    ``huggingface_hub`` downloads via ``hf_xet``, a Rust extension that never touches
    Python's ``socket`` module, so a socket-level block sees nothing and reports success.
    Refusing before the call is the only check that cannot be bypassed from underneath.

    A configured ``model_path`` always wins and is always local-only — an explicit local
    model is never a reason to reach the hub anyway.
    """
    local = model_path or settings.model_path
    if local is not None:
        return _verified_local_source(local, settings.model_digest)
    reference = model_name or settings.model_name
    if not settings.allow_model_download:
        raise ModelNotLocalError(
            f"no local embedding model is configured, so EdgeProc will not load "
            f"{reference!r}: fetching it would need the network. {REMEDY}"
        )
    return ModelSource(reference=reference, local_files_only=False)


def _verified_local_source(path: Path, expected_digest: str | None) -> ModelSource:
    if not path.is_dir():
        raise ModelPathInvalidError(f"EDGEPROC_MODEL_PATH is not a directory: {path}")
    _require_digest(path, expected_digest)
    return ModelSource(reference=str(path), local_files_only=True)


def _require_digest(path: Path, expected: str | None) -> None:
    """Refuse a model whose bytes do not match the pin. No pin configured = no check."""
    if expected is None:
        return
    actual = digest_model_dir(path)
    if actual != expected:
        raise ModelDigestMismatchError(
            f"model at {path} hashes to {actual}, but EDGEPROC_MODEL_DIGEST pins {expected}"
        )
