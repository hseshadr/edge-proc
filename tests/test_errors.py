"""edge-proc's adoption of the shared canonical-errors module.

edge-proc's most prominent operator-facing failure — a CAS/bundle integrity
refusal (:class:`IntegrityError`) at the trust boundary — now carries a canonical
code from ``shared_libs_python.errors`` and renders to RFC 9457 Problem Details,
WITHOUT changing the exception type or message any existing caller depends on.
"""

from __future__ import annotations

from pathlib import Path

from edgeproc import errors
from edgeproc.bundles.adapters import ResponseTooLargeError
from edgeproc.bundles.cas import IntegrityError, RollbackError
from edgeproc.bundles.signing import SignatureError

_INTEGRITY_EN = "A downloaded file failed its integrity check. Retry to re-fetch it."


def test_registry_registers_every_edgeproc_code() -> None:
    # Drift lock: every code edge-proc names is registered in its catalog.
    for code in (
        errors.BUNDLE_INTEGRITY_FAILED,
        errors.BUNDLE_DOWNLOAD_FAILED,
        errors.CONFIG_MISSING,
        errors.CONFIG_INVALID,
        errors.INTERNAL_UNKNOWN,
    ):
        assert errors.registry.has(code)


def test_integrity_error_carries_canonical_code() -> None:
    assert IntegrityError.code == errors.BUNDLE_INTEGRITY_FAILED
    assert IntegrityError("boom").code == "bundle.integrity_failed"


def test_registry_describes_integrity_failure_in_plain_english() -> None:
    assert errors.registry.describe(IntegrityError.code) == _INTEGRITY_EN


def test_problem_details_render_code_description_and_message() -> None:
    err = IntegrityError("manifest deadbeef failed content-address check")
    problem = errors.problem_details_for(err)
    assert problem.type == "bundle.integrity_failed"
    assert problem.title == _INTEGRITY_EN
    assert problem.detail == "manifest deadbeef failed content-address check"
    wire = problem.to_dict()  # RFC 9457 wire object carries the same triple
    assert wire["type"] == "bundle.integrity_failed"
    assert wire["detail"] == "manifest deadbeef failed content-address check"


def test_adoption_is_behavior_identical() -> None:
    # The message + type every existing caller depends on are unchanged, and the
    # canonical code rides along on the whole IntegrityError family.
    err = IntegrityError("chunk abc failed content-address check")
    assert str(err) == "chunk abc failed content-address check"
    assert type(err) is IntegrityError
    rollback = RollbackError("refusing rollback")
    assert isinstance(rollback, IntegrityError)  # subclass unchanged
    assert rollback.code == errors.BUNDLE_INTEGRITY_FAILED  # inherits the code
    assert str(rollback) == "refusing rollback"


def test_unknown_error_falls_back_to_internal_unknown() -> None:
    # An arbitrary exception carrying no canonical code maps to the universal fallback.
    problem = errors.problem_details_for(ValueError("something else"))
    assert problem.type == "internal.unknown"
    assert problem.detail == "something else"


def test_every_declared_code_is_attached_at_a_real_throw_site() -> None:
    """Anti-vacuity: a code named "for greppable reuse at throw-sites" must have one.

    Regression: three of the five codes were declared but never attached to anything
    raised, so the catalog advertised coverage the product did not have. Each code must
    be referenced from shipped code OUTSIDE its own declaration in ``errors.py``.
    """
    root = Path(__file__).resolve().parents[1] / "edgeproc"
    sources = {
        path: path.read_text(encoding="utf-8")
        for path in root.rglob("*.py")
        if path.name != "errors.py"
    }
    unattached = [
        name
        for name in (
            "BUNDLE_INTEGRITY_FAILED",
            "BUNDLE_DOWNLOAD_FAILED",
            "CONFIG_MISSING",
            "CONFIG_INVALID",
            "INTERNAL_UNKNOWN",
        )
        if not any(name in text for text in sources.values())
    ]
    assert unattached == [], f"declared but never used at a throw site: {unattached}"


def test_download_failure_carries_the_download_code() -> None:
    assert ResponseTooLargeError.code == errors.BUNDLE_DOWNLOAD_FAILED
    assert errors.code_of(ResponseTooLargeError("too big")) == "bundle.download_failed"


def test_signature_failure_carries_the_integrity_code() -> None:
    assert errors.code_of(SignatureError("bad signature")) == "bundle.integrity_failed"
