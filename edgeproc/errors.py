"""edge-proc's canonical-error catalog, registered with ``edgeproc_core.errors``.

edge-proc surfaces one prominent operator-facing failure — a trust-boundary
integrity refusal from the CAS/bundle substrate — alongside the universal
transport and config failures. Each maps onto a code already in the shared
``starter_pack``, so edge-proc declares no codes of its own: it adopts the
portfolio-wide catalog and renders any carried code to the RFC 9457 Problem
Details shape a browser/service consumer wants (canonical code + i18n-ready
description + the throw-site's own message).

The integration is deliberately bounded and behavior-identical. Raising code is
unchanged; :class:`~edgeproc.bundles.cas.IntegrityError` simply *carries* its
canonical ``code`` (see that class), which this module resolves and serializes.
"""

from __future__ import annotations

from dataclasses import replace

from edgeproc_core.errors import (
    ErrorCode,
    Params,
    ProblemDetails,
    Registry,
    define_errors,
    starter_pack,
)

# This module is edge-proc's seam over the shared catalog: callers import the code
# constants AND the types they are annotated with from here, never reaching through to
# ``edgeproc_core.errors`` themselves. Re-exporting the two types explicitly is what
# makes that a supported import rather than an accident of implementation.
__all__ = [
    "BUNDLE_DOWNLOAD_FAILED",
    "BUNDLE_INTEGRITY_FAILED",
    "CONFIG_INVALID",
    "CONFIG_MISSING",
    "INTERNAL_UNKNOWN",
    "ErrorCode",
    "Params",
    "ProblemDetails",
    "code_of",
    "problem_details_for",
    "registry",
]

#: Canonical codes edge-proc surfaces, each attached at a real throw site.
#: ``bundle.integrity_failed`` rides on IntegrityError (CAS) and SignatureError (signing);
#: ``bundle.download_failed`` on ResponseTooLargeError (adapters); ``config.missing`` and
#: ``config.invalid`` are stamped onto the CLI's fail-closed config refusals;
#: ``internal.unknown`` is the fallback ``code_of`` returns for an uncoded error.
BUNDLE_INTEGRITY_FAILED: ErrorCode = "bundle.integrity_failed"
BUNDLE_DOWNLOAD_FAILED: ErrorCode = "bundle.download_failed"
CONFIG_MISSING: ErrorCode = "config.missing"
CONFIG_INVALID: ErrorCode = "config.invalid"
INTERNAL_UNKNOWN: ErrorCode = "internal.unknown"

#: edge-proc's registry — the shared universal catalog, adopted wholesale.
registry: Registry = define_errors(starter_pack)


def code_of(error: BaseException) -> ErrorCode:
    """The canonical code an error carries, or ``internal.unknown`` if it has none."""
    code = getattr(error, "code", None)
    if isinstance(code, str) and registry.has(code):
        return code
    return INTERNAL_UNKNOWN


def problem_details_for(error: BaseException, params: Params | None = None) -> ProblemDetails:
    """Render an error as RFC 9457 Problem Details (canonical code + description).

    ``title`` is the catalog's English for the carried code; ``detail`` is the
    exception's own message, preserved verbatim so no operator-facing text is lost.

    ``params`` fills the catalog entry's ``{placeholder}`` slots and rides along as
    RFC 9457 extension members. ``config.missing``/``config.invalid`` declare a
    ``field``, so passing ``{"field": "--key"}`` is what turns the generic
    "A required setting is missing: {field}." into a sentence naming the real input.
    """
    problem = registry.to_problem_details(code_of(error), params)
    return replace(problem, detail=str(error))
