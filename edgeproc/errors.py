"""edge-proc's canonical-error catalog, registered with ``shared_libs_python.errors``.

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

from shared_libs_python.errors import (
    ErrorCode,
    ProblemDetails,
    Registry,
    define_errors,
    starter_pack,
)

#: Canonical codes edge-proc surfaces, named for greppable reuse at throw-sites.
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


def problem_details_for(error: BaseException) -> ProblemDetails:
    """Render an error as RFC 9457 Problem Details (canonical code + description).

    ``title`` is the catalog's English for the carried code; ``detail`` is the
    exception's own message, preserved verbatim so no operator-facing text is lost.
    """
    return replace(registry.to_problem_details(code_of(error)), detail=str(error))
