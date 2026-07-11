"""Path-containment guard: a manifest path may never escape its output root.

A ``VersionPointer`` is signed, but the *paths inside* the manifest it names are
attacker-influenceable in a compromised- or malformed-origin scenario. Writing
``out / entry.path`` with no containment check turns a traversal path
(``../../etc/cron.d/x``) or an absolute path into a write OUTSIDE the target dir.
These tests pin the fail-closed guard that every materialize path routes through.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from edgeproc.bundles.containment import (
    UnsafePathError,
    ensure_safe_relpath,
    resolve_within,
)


def test_plain_relative_path_resolves_under_root(tmp_path: Path) -> None:
    target = resolve_within(tmp_path, "sub/dir/file.bin")

    assert target == (tmp_path / "sub" / "dir" / "file.bin").resolve()
    assert tmp_path.resolve() in target.parents


def test_parent_traversal_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(UnsafePathError):
        resolve_within(tmp_path, "../escape.txt")


def test_deep_parent_traversal_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(UnsafePathError):
        resolve_within(tmp_path, "a/b/../../../etc/passwd")


def test_absolute_path_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(UnsafePathError):
        resolve_within(tmp_path, "/etc/passwd")


def test_backslash_path_is_rejected(tmp_path: Path) -> None:
    # A backslash is a Windows traversal/drive vector; refuse it everywhere.
    with pytest.raises(UnsafePathError):
        resolve_within(tmp_path, "a\\..\\..\\evil")


def test_empty_path_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(UnsafePathError):
        resolve_within(tmp_path, "")


def test_symlink_escape_is_rejected(tmp_path: Path) -> None:
    # A path with no ``..`` still escapes if it routes through a symlink that
    # points outside the root — the string check passes, so the resolved-target
    # re-check is what must catch it (defense-in-depth for the symlink vector the
    # module docstring promises to refuse).
    outside = tmp_path.parent / "outside"
    outside.mkdir()
    (tmp_path / "link").symlink_to(outside, target_is_directory=True)

    with pytest.raises(UnsafePathError):
        resolve_within(tmp_path, "link/loot.txt")


def test_ensure_safe_relpath_returns_input_for_safe_paths() -> None:
    assert ensure_safe_relpath("norm.json") == "norm.json"
    assert ensure_safe_relpath("a/b/c.bin") == "a/b/c.bin"


def test_unsafe_path_error_is_a_value_error() -> None:
    # Subclassing ValueError lets a pydantic field validator raise it and have
    # pydantic fold it into a ValidationError, while direct callers can still
    # catch the precise UnsafePathError type.
    assert issubclass(UnsafePathError, ValueError)
