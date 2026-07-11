"""Path-containment guard for materializing signed-manifest files (fail-closed).

A signed :class:`~edgeproc.bundles.manifest.VersionPointer` authenticates a
manifest by content hash, but the *file paths inside* that manifest still have to
be treated as untrusted when written to disk: a malformed or compromised origin
could carry a traversal path (``../../etc/cron.d/x``) or an absolute path, and a
naive ``out / entry.path`` would then write OUTSIDE the intended output directory.

This module is the single chokepoint. Every manifest path is validated here before
it is joined to any output root, and the fully-resolved target is re-checked to lie
within that root — so both string tricks and symlink/normalization escapes are
refused. A validly-produced manifest never trips this: our producer records paths as
``p.relative_to(src).as_posix()``, which are always plain relative POSIX paths.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath


class UnsafePathError(ValueError):
    """A manifest file path escapes its output root (traversal / absolute / drive).

    Subclasses :class:`ValueError` so a pydantic field validator can raise it and
    have it folded into a ``ValidationError``, while direct callers can still catch
    the precise type.
    """


def ensure_safe_relpath(path: str) -> str:
    """Return ``path`` iff it is a plain relative POSIX path, else raise.

    Rejects the empty string, any backslash (a Windows traversal/drive vector),
    absolute paths (``/x``), and any ``..`` parent-traversal segment.
    """
    if not path or "\\" in path:
        raise UnsafePathError(f"unsafe manifest path: {path!r}")
    pure = PurePosixPath(path)
    if pure.is_absolute() or ".." in pure.parts:
        raise UnsafePathError(f"unsafe manifest path: {path!r}")
    return path


def resolve_within(root: Path, relpath: str) -> Path:
    """Join ``relpath`` under ``root``, guaranteeing the result stays inside ``root``.

    Validates the raw path, then confirms the fully-resolved target is ``root``
    itself or a descendant — catching normalization/symlink escapes a string check
    alone would miss. Raises :class:`UnsafePathError` otherwise.
    """
    ensure_safe_relpath(relpath)
    root_resolved = root.resolve()
    target = (root_resolved / relpath).resolve()
    if target != root_resolved and root_resolved not in target.parents:
        raise UnsafePathError(f"path {relpath!r} escapes {root}")
    return target
