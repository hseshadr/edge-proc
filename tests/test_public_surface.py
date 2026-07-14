"""A consumer imports the whole core surface from the top-level package."""

from __future__ import annotations

import importlib.metadata

import edgeproc


def test_top_level_exports_the_core_surface() -> None:
    expected = {
        "EdgeProc",
        "Task",
        "ResultEnvelope",
        "TaskKind",
        "PrivacyMode",
        "CapabilityVerdict",
        "Provenance",
        "Runtime",
        "Router",
        "TelemetrySink",
        "__version__",
    }
    assert expected <= set(edgeproc.__all__)
    for name in expected:
        assert hasattr(edgeproc, name)


def test_top_level_reexports_concrete_defaults() -> None:
    # A consumer should be able to wire EdgeProc from the top-level import alone —
    # no scavenger hunt through edgeproc.core.* for the default router/sink/registry.
    for name in ("DefaultRouter", "NullSink", "BufferedSink", "RuntimeRegistry"):
        assert name in edgeproc.__all__
        assert hasattr(edgeproc, name)


def test_version_is_a_string() -> None:
    assert isinstance(edgeproc.__version__, str)


def test_version_matches_installed_package_metadata() -> None:
    # Regression: __version__ once sat at 0.1.1 while the package shipped 0.1.3.
    # The version must be single-sourced so the two can never drift again.
    assert edgeproc.__version__ == importlib.metadata.version("edge-proc")


async def test_result_envelope_stamps_the_installed_version() -> None:
    # Provenance is the audit trail — every envelope must carry the real
    # released version, not a stale literal.
    envelope = await edgeproc.EdgeProc.local_default().run(
        edgeproc.Task(
            kind=edgeproc.TaskKind.DETERMINISTIC,
            payload={},
            privacy_mode=edgeproc.PrivacyMode.LOCAL_ONLY,
        )
    )
    assert envelope.provenance.runtime_version == importlib.metadata.version("edge-proc")
