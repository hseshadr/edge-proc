"""A consumer imports the whole core surface from the top-level package."""

from __future__ import annotations

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
