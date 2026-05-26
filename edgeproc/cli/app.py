"""EdgeProc command-line interface.

Commands report the version, show which optional runtime extras are installed,
sync + verify a bundle, and ``route`` a Task through a ``LocalVecRuntime`` loaded
from a persisted index directory (the runtime wiring the empty-registry default
can't do on its own).
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, NoReturn

import typer
from pydantic import ValidationError

from edgeproc._version import __version__
from edgeproc.core.facade import EdgeProc
from edgeproc.core.models import JsonValue, ResultEnvelope, Task
from edgeproc.core.registry import RuntimeRegistry

if TYPE_CHECKING:
    from edgeproc.core.protocols import Runtime
    from edgeproc.localvec.encoder import Encoder

app = typer.Typer(help="EdgeProc — AI-native local execution substrate.", no_args_is_help=True)


@app.command()
def version() -> None:
    """Print the EdgeProc version."""
    typer.echo(__version__)


@app.command("list-runtimes")
def list_runtimes() -> None:
    """Report which optional runtime extras are installed."""
    availability = {
        "localvec": importlib.util.find_spec("faiss") is not None,
        "bundles": importlib.util.find_spec("httpx") is not None,
    }
    typer.echo(json.dumps(availability))


@app.command("bundle-sync")
def bundle_sync(
    manifest_url: str,
    cache_dir: Path,
    file_base_url: str,
    http: bool = typer.Option(False, help="Fetch over HTTP/CDN instead of the filesystem."),
) -> None:
    """Download a bundle, checksum-verify every file, and cache it locally."""
    try:
        # Lazy: the bundles substrate is an optional extra, not a core dependency.
        from edgeproc.bundles.adapters import FilesystemAdapter, HttpAdapter  # noqa: PLC0415
        from edgeproc.bundles.sync import sync_bundle  # noqa: PLC0415
    except ImportError:  # pragma: no cover - exercised only without the [bundles] extra
        typer.echo("install edge-proc[bundles] to use bundle-sync", err=True)
        raise typer.Exit(code=1) from None

    adapter = HttpAdapter() if http else FilesystemAdapter()
    manifest = sync_bundle(
        manifest_url=manifest_url,
        cache_dir=cache_dir,
        adapter=adapter,
        file_base_url=file_base_url,
    )
    typer.echo(f"synced {manifest.bundle_id} v{manifest.version} ({len(manifest.files)} files)")


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
        from edgeproc.localvec.loader import load_local_runtime  # noqa: PLC0415
    except ImportError:  # pragma: no cover - only without the [localvec] extra
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


def _fail(message: str) -> NoReturn:
    typer.echo(message, err=True)
    raise typer.Exit(code=1)
