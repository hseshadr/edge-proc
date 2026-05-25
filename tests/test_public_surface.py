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


def test_version_is_a_string() -> None:
    assert isinstance(edgeproc.__version__, str)
