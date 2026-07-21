"""edge-proc's adoption of the shared canonical-errors module.

edge-proc's most prominent operator-facing failure — a CAS/bundle integrity
refusal (:class:`IntegrityError`) at the trust boundary — now carries a canonical
code from ``edgeproc_core.errors`` and renders to RFC 9457 Problem Details,
WITHOUT changing the exception type or message any existing caller depends on.
"""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from edgeproc import errors
from edgeproc.bundles.adapters import ResponseTooLargeError
from edgeproc.bundles.cas import IntegrityError, RollbackError
from edgeproc.bundles.signing import SignatureError
from edgeproc.cli import app

_INTEGRITY_EN = "A downloaded file failed its integrity check. Retry to re-fetch it."
_runner = CliRunner()


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


def _cli_refusal_code(tmp_path: Path, key: Path) -> str:
    """Drive `sync` to a fail-closed refusal and return the code it actually rendered."""
    result = _runner.invoke(
        app,
        [
            "sync",
            "--base-url",
            str(tmp_path / "origin"),
            "--cache-dir",
            str(tmp_path / "cache"),
            "--key",
            str(key),
        ],
        env={"EDGEPROC_ERROR_FORMAT": "json"},
    )
    assert result.exit_code == 1
    code: str = json.loads(result.stderr.strip())["type"]
    return code


def test_every_declared_code_is_produced_by_a_real_failure(tmp_path: Path) -> None:
    """Anti-vacuity: every declared code must be OBSERVABLE from a real failure path.

    The earlier guard grepped edge-proc's sources for each constant's NAME, which a bare
    ``from edgeproc.errors import ...`` line already satisfied — it proved a spelling,
    not a behavior, and would have stayed green while a code was declared and never
    rendered. This drives the paths instead and collects the codes they emit: the two
    ``config.*`` codes come back off the CLI's real stderr payload, so a code nothing can
    produce (or one whose wiring regresses) fails here.
    """
    malformed = tmp_path / "public.key"
    malformed.write_bytes(b"short")  # present, but not a 32-byte raw ed25519 public key

    observed = {
        errors.code_of(IntegrityError("chunk failed its content-address check")),
        errors.code_of(SignatureError("manifest signature did not verify")),
        errors.code_of(ResponseTooLargeError("body exceeded the fetch ceiling")),
        errors.code_of(ValueError("a failure carrying no canonical code")),
        _cli_refusal_code(tmp_path, tmp_path / "absent.key"),
        _cli_refusal_code(tmp_path, malformed),
    }

    assert observed == {
        errors.BUNDLE_INTEGRITY_FAILED,
        errors.BUNDLE_DOWNLOAD_FAILED,
        errors.CONFIG_MISSING,
        errors.CONFIG_INVALID,
        errors.INTERNAL_UNKNOWN,
    }


def test_download_failure_carries_the_download_code() -> None:
    assert ResponseTooLargeError.code == errors.BUNDLE_DOWNLOAD_FAILED
    assert errors.code_of(ResponseTooLargeError("too big")) == "bundle.download_failed"


def test_signature_failure_carries_the_integrity_code() -> None:
    assert errors.code_of(SignatureError("bad signature")) == "bundle.integrity_failed"
