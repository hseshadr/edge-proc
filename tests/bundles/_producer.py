"""Test-only origin producer: a thin wrapper over the SHIPPED ``build_bundle``.

The producer is no longer test-only code — ``edgeproc.bundles.publish.build_bundle``
ships it (with the ``edgeproc publish`` CLI). This helper now just adapts the tests'
``build_origin(origin: Path, ...)`` call shape to ``build_bundle(store=...)`` so the
wave-5/6 integration tests exercise the shipped producer, not a parallel copy. It
lays out the same flat ``origin`` dir matching the HTTP contract — ``latest``,
``manifest/<hash>``, ``chunk/<hash>`` — that a ``FilesystemAdapter`` serves verbatim.
"""

from __future__ import annotations

from pathlib import Path

from edgeproc.bundles.cas import FilesystemCacheStore
from edgeproc.bundles.chunking import GearCDC
from edgeproc.bundles.manifest import VersionPointer
from edgeproc.bundles.publish import build_bundle
from edgeproc.bundles.signing import Ed25519Signer


def build_origin(
    *,
    files: dict[str, bytes],
    origin: Path,
    chunker: GearCDC,
    signer: Ed25519Signer,
    version: str = "1.0.0",
    bundle_id: str = "b",
) -> VersionPointer:
    """Chunk + lay out ``files`` under ``origin`` and return the signed pointer."""
    return build_bundle(
        files=files,
        store=FilesystemCacheStore(origin),
        chunker=chunker,
        signer=signer,
        bundle_id=bundle_id,
        version=version,
    )
