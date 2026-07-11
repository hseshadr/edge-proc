"""EdgeProc command-line interface.

Commands report the version, show which optional runtime extras are installed,
sync + verify a bundle, and ``route`` a Task through a ``LocalVecRuntime`` loaded
from a persisted index directory (the runtime wiring the empty-registry default
can't do on its own).
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, NoReturn

import typer
from pydantic import ValidationError

from edgeproc._version import __version__
from edgeproc.core.facade import EdgeProc
from edgeproc.core.models import JsonValue, ResultEnvelope, Task
from edgeproc.core.registry import RuntimeRegistry

if TYPE_CHECKING:
    from edgeproc.bundles.adapters import FetchAdapter
    from edgeproc.bundles.cas import CacheStore
    from edgeproc.bundles.manifest import IndexManifest, VersionPointer
    from edgeproc.bundles.signing import Ed25519Signer, Signer, Verifier
    from edgeproc.bundles.sync import SyncResult
    from edgeproc.core.protocols import Runtime
    from edgeproc.localvec.encoder import Encoder

app = typer.Typer(help="EdgeProc — AI-native local execution substrate.", no_args_is_help=True)


@app.command()
def version() -> None:
    """Print the EdgeProc version."""
    typer.echo(__version__)


@app.command("list-runtimes")
def list_runtimes() -> None:
    """Report which optional runtime extras are installed.

    Imports each extra's actual entry-point module so a partial install (e.g.
    cryptography without httpx, numpy without faiss) reports the extra as
    unavailable rather than misleadingly green.
    """
    availability = {
        "localvec": _can_import("edgeproc.localvec.runtime"),
        "bundles": _can_import("edgeproc.bundles.sync"),
    }
    typer.echo(json.dumps(availability))


def _can_import(module: str) -> bool:
    """True iff ``module`` and its transitive dependencies all import successfully."""
    try:
        importlib.import_module(module)
    except ImportError:
        return False
    return True


@app.command()
def sync(
    base_url: Annotated[str, typer.Option(help="Origin base URL (/latest, /manifest, /chunk).")],
    cache_dir: Annotated[Path, typer.Option(help="Local content-addressed store directory.")],
    http: Annotated[bool, typer.Option(help="Fetch over HTTP/CDN instead of the filesystem.")] = (
        False
    ),
    key: Annotated[
        Path | None,
        typer.Option(help="Pinned ed25519 trust-root pubkey (else the env trust-root path)."),
    ] = None,
    materialize_to: Annotated[
        Path | None,
        typer.Option(help="Also reassemble every file from the synced cache into this dir."),
    ] = None,
    pretty: Annotated[bool, typer.Option(help="Print a human summary instead of JSON.")] = False,
) -> None:
    """Pull a signed pointer, diff + fetch only missing chunks, verify, atomically swap.

    Refuses to sync without a pinned trust root (``--key`` or the env var): an
    unverifiable sync is rejected fail-closed. ``--materialize-to`` reassembles the
    synced files (fail-closed on a reassembly mismatch) so a follow-on ``route`` can
    read them by path. Exit 0 on success, 1 otherwise.
    """
    try:
        # Imported lazily so the core install stays light: these belong to the optional
        # `[bundles]` extra, not the core dependency set (hence the per-line PLC0415).
        from edgeproc.bundles.adapters import FilesystemAdapter, HttpAdapter  # noqa: PLC0415
        from edgeproc.bundles.cas import FilesystemCacheStore  # noqa: PLC0415
        from edgeproc.bundles.signing import Ed25519Verifier  # noqa: PLC0415
    except ImportError:  # pragma: no cover - exercised only without the [bundles] extra
        _fail("install edge-proc[bundles] to use sync")
    verifier = Ed25519Verifier.from_public_bytes(_resolve_trust_key(key).read_bytes())
    store = FilesystemCacheStore(cache_dir)
    adapter = HttpAdapter() if http else FilesystemAdapter()
    result = _run_sync(base_url, store, adapter, verifier, close=http)
    if materialize_to is not None:
        _materialize_active(store, materialize_to)
    typer.echo(_render_sync(result, pretty=pretty))


@app.command()
def publish(
    src: Annotated[Path, typer.Option(help="Directory of files to publish (recursive).")],
    origin_dir: Annotated[Path, typer.Option(help="Origin dir to lay out the CDN contract into.")],
    key: Annotated[Path, typer.Option(help="Ed25519 raw private key to sign the pointer with.")],
    bundle_id: Annotated[str, typer.Option(help="Bundle identifier recorded in the manifest.")],
    version: Annotated[str, typer.Option(help="Bundle version recorded in the pointer.")],
    pretty: Annotated[bool, typer.Option(help="Print a human summary instead of JSON.")] = False,
) -> None:
    """Chunk + sign every file under ``--src`` into a content-addressed origin dir.

    The counterpart to ``sync``: produces the ``/latest`` + ``/manifest`` + ``/chunk``
    an ``edgeproc sync`` consumes. A missing/invalid key or src fails closed (exit 1,
    no traceback); exit 0 on success.
    """
    try:
        # Lazy: the bundles substrate is an optional extra, not a core dependency.
        from edgeproc.bundles.cas import FilesystemCacheStore  # noqa: PLC0415
        from edgeproc.bundles.chunking import GearCDC  # noqa: PLC0415
        from edgeproc.bundles.publish import build_bundle  # noqa: PLC0415
        from edgeproc.bundles.signing import Ed25519Signer  # noqa: PLC0415
    except ImportError:  # pragma: no cover - exercised only without the [bundles] extra
        _fail("install edge-proc[bundles] to use publish")
    signer = _load_signer(key, Ed25519Signer)
    pointer = build_bundle(
        files=_read_src(src),
        store=FilesystemCacheStore(origin_dir),
        chunker=GearCDC(),
        signer=signer,
        bundle_id=bundle_id,
        version=version,
    )
    typer.echo(_render_pointer(pointer, pretty=pretty))


@app.command()
def keygen(
    out: Annotated[Path, typer.Option(help="Dir to write private.key + public.key (raw ed25519).")],
) -> None:
    """Write a raw ed25519 keypair (``private.key`` + ``public.key``) into ``--out``."""
    from edgeproc.bundles.signing import generate_keypair  # noqa: PLC0415

    out.mkdir(parents=True, exist_ok=True)
    private, public = generate_keypair()
    _write_secret(out / "private.key", private.private_bytes_raw())
    (out / "public.key").write_bytes(public.public_bytes_raw())
    typer.echo(f"wrote {out / 'private.key'} and {out / 'public.key'}")


def _write_secret(path: Path, data: bytes) -> None:
    """Write a secret file readable/writable by its owner ONLY (mode ``0600``).

    A signing key must never be world-readable: ``os.open`` creates it 0600 up front and
    ``os.chmod`` re-asserts 0600 even if the file pre-existed or an umask loosened it.
    """
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
    os.chmod(path, 0o600)


@app.command()
def route(
    index_dir: Annotated[Path, typer.Option(help="Directory holding a saved FaissVectorIndex.")],
    task: Annotated[Path, typer.Option(help="JSON file holding the Task to route.")],
    model: Annotated[
        str | None,
        typer.Option(help="Encoder model (defaults to EDGEPROC_MODEL_NAME or the built-in)."),
    ] = None,
    pretty: Annotated[bool, typer.Option(help="Print a human summary instead of JSON.")] = False,
) -> None:
    """Route a Task (JSON) through a LocalVecRuntime loaded from a saved index dir.

    The exit code mirrors the result: 0 when the runtime succeeded, 1 otherwise
    (including ``no_runtime_accepted``), so scripts can branch on it.
    """
    task_obj = _load_task(task)
    runtime = _route_runtime(index_dir, model, index_dir.name)
    envelope = _run_task(task_obj, runtime)
    typer.echo(_render(envelope, pretty=pretty))
    raise typer.Exit(code=0 if envelope.success else 1)


def build_encoder(model: str | None) -> Encoder:
    """Construct the real text encoder. A seam: tests replace this with a fake."""
    from edgeproc.localvec.encoder import TextEncoder  # noqa: PLC0415  # pragma: no cover

    return TextEncoder(model_name=model)  # pragma: no cover - downloads a model; live demo


def _load_task(path: Path) -> Task:
    try:
        return Task.model_validate_json(path.read_text())
    except (OSError, ValidationError) as exc:
        _fail(f"could not load task from {path}: {exc}")


def _route_runtime(index_dir: Path, model: str | None, index_name: str) -> Runtime:
    try:
        # Lazy: route belongs to the optional `[localvec]` extra, not the core deps.
        from edgeproc.localvec.loader import load_local_runtime  # noqa: PLC0415
    except ImportError:  # pragma: no cover - guards a partial install: route is unusable
        # without the [localvec] extra, so we fail closed with an install hint, not a crash.
        _fail("install edge-proc[localvec] to use route")
    encoder = build_encoder(model)
    try:
        return load_local_runtime(index_dir, encoder=encoder, index_name=index_name)
    except (FileNotFoundError, ValueError) as exc:
        _fail(f"could not load index from {index_dir}: {exc}")


def _run_task(task: Task, runtime: Runtime) -> ResultEnvelope:
    registry = RuntimeRegistry()
    registry.register(runtime)
    return asyncio.run(EdgeProc(registry=registry).run(task))


def _render(envelope: ResultEnvelope, *, pretty: bool) -> str:
    if pretty:
        return _summary(envelope)
    return envelope.model_dump_json(indent=2)


def _summary(envelope: ResultEnvelope) -> str:
    lines = [
        f"success={envelope.success} runtime={envelope.runtime_used} "
        f"latency={envelope.latency_ms:.1f}ms"
    ]
    if envelope.error:
        lines.append(f"error={envelope.error}")
    lines.extend(_result_lines(envelope.payload))
    return "\n".join(lines)


def _result_lines(payload: dict[str, JsonValue]) -> list[str]:
    results = payload.get("results")
    if not isinstance(results, list):
        return []
    return [_fmt_row(row) for row in results]


def _fmt_row(row: JsonValue) -> str:
    if isinstance(row, list):
        return "  " + "  ".join(_fmt_part(part) for part in row)
    return f"  {row}"  # pragma: no cover - result rows are always [id, score] lists


def _fmt_part(part: JsonValue) -> str:
    return f"{part:.3f}" if isinstance(part, float) else str(part)


def _resolve_trust_key(key: Path | None) -> Path:
    """The pinned verify key: ``--key`` else the setting. Neither set → fail-closed."""
    from edgeproc.core.settings import EdgeProcSettings  # noqa: PLC0415

    resolved = key if key is not None else EdgeProcSettings().trust_root_pubkey_path
    if resolved is None:
        _fail("no trust root: pass --key or set EDGEPROC_TRUST_ROOT_PUBKEY_PATH (refusing to sync)")
    return resolved


def _run_sync(
    base_url: str,
    store: CacheStore,
    adapter: FetchAdapter,
    verifier: Verifier,
    *,
    close: bool,
) -> SyncResult:
    """Run ``sync_index``; map signature/integrity/fetch failures to exit 1, no traceback."""
    import httpx  # noqa: PLC0415

    from edgeproc.bundles.cas import IntegrityError  # noqa: PLC0415
    from edgeproc.bundles.signing import SignatureError  # noqa: PLC0415
    from edgeproc.bundles.sync import sync_index  # noqa: PLC0415

    try:
        return sync_index(base_url=base_url, store=store, adapter=adapter, verifier=verifier)
    except (SignatureError, IntegrityError, httpx.HTTPError, OSError) as exc:
        _fail(f"sync failed: {exc}")
    finally:
        if close:
            # The FetchAdapter Protocol has no close(); only HttpAdapter does (it owns a
            # pooled client). close is True only on the HTTP path, so this is safe.
            adapter.close()  # type: ignore[attr-defined]


def _materialize_active(store: CacheStore, out: Path) -> None:
    """Reassemble every file in the active manifest into ``out/`` (fail-closed)."""
    from edgeproc.bundles.manifest import IndexManifest  # noqa: PLC0415

    pointer = store.read_active()
    if pointer is None:  # pragma: no cover
        # Guards a future bug: sync_index always promotes an active pointer before we
        # materialize, so a None here means that invariant broke — fail closed, never None-deref.
        _fail("no active pointer to materialize")
    manifest = IndexManifest.model_validate_json(store.get_manifest(pointer.manifest_hash))
    _materialize_files(store, manifest, out)


def _materialize_files(store: CacheStore, manifest: IndexManifest, out: Path) -> None:
    """Write each manifest file under ``out``, refusing any path that escapes it.

    ``resolve_within`` runs BEFORE any write, so a traversal/absolute path can never
    land bytes outside ``out`` — defense-in-depth behind the model-level path check.
    """
    from edgeproc.bundles.containment import resolve_within  # noqa: PLC0415
    from edgeproc.bundles.sync import materialize_file  # noqa: PLC0415

    for entry in manifest.files:
        target = resolve_within(out, entry.path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(materialize_file(store, manifest, entry.path))


def _render_sync(result: SyncResult, *, pretty: bool) -> str:
    if pretty:
        return (
            f"synced v{result.version} manifest={result.manifest_hash[:12]} "
            f"chunks_fetched={result.chunks_fetched} chunks_reused={result.chunks_reused} "
            f"bytes_fetched={result.bytes_fetched}"
        )
    return result.model_dump_json(indent=2)


def _load_signer(key: Path, signer_cls: type[Ed25519Signer]) -> Signer:
    """Load a raw ed25519 private key into a ``Signer``; fail closed if it can't.

    Two distinct, actionable failures: a missing/unreadable key FILE (OSError) vs a
    present-but-malformed key (ValueError from ``from_private_bytes`` — wrong length /
    bad bytes). Splitting them tells the operator whether to fix the path or the key.
    """
    try:
        raw = key.read_bytes()
    except OSError as exc:
        _fail(f"could not read signing key {key}: {exc}")
    try:
        return signer_cls.from_private_bytes(raw)
    except ValueError as exc:
        _fail(f"malformed signing key {key}: {exc}")


def _read_src(src: Path) -> dict[str, bytes]:
    """Read every file under ``src`` into ``{relative-posix-path: bytes}`` (fail closed)."""
    if not src.is_dir():
        _fail(f"src is not a directory: {src}")
    files = {
        p.relative_to(src).as_posix(): p.read_bytes() for p in sorted(src.rglob("*")) if p.is_file()
    }
    if not files:
        _fail(f"no files to publish under {src}")
    return files


def _render_pointer(pointer: VersionPointer, *, pretty: bool) -> str:
    if pretty:
        return f"published v{pointer.version} manifest={pointer.manifest_hash[:12]}"
    return pointer.model_dump_json(indent=2)


def _fail(message: str) -> NoReturn:
    typer.echo(message, err=True)
    raise typer.Exit(code=1)
