"""EdgeProc command-line interface.

v0 commands cover what's genuinely useful without a configured corpus: report the
version, show which optional runtime extras are installed, and sync + verify a
bundle. ``route`` is roadmap — it needs a bundle wired into a runtime.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import typer

from edgeproc._version import __version__

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
