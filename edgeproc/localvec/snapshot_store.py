"""Crash-safe two-file snapshots committed by one generation manifest.

The FAISS binary and its JSON sidecar are one logical value.  Neither file can be
the commit marker because replacing them separately exposes a cross-generation pair.
This store writes generation-addressed data first, then atomically publishes one small
manifest naming both files.  Readers use only that manifest, so an interrupted save is
an orphaned generation, never an active hybrid.
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

_SNAPSHOT_DIR: Final[str] = "snapshots"
_MANIFEST_SUFFIX: Final[str] = ".snapshot.json"
_MANIFEST_NAME: Final[re.Pattern[str]] = re.compile(r"^[0-9]{20}\.snapshot\.json$")
_RETAIN_GENERATIONS: Final[int] = 2  # active snapshot plus one crash-recovery fallback
_MAX_SEQUENCE: Final[int] = 99_999_999_999_999_999_999
_HASH_BLOCK_BYTES: Final[int] = 512 * 1024


class NoSnapshotError(FileNotFoundError):
    """No generation manifest has ever committed in this directory."""


class SnapshotConflictError(RuntimeError):
    """A loaded writer is based on a generation that is no longer current."""


class SnapshotSequenceError(RuntimeError):
    """The fixed-width snapshot sequence space is exhausted."""


@dataclass(frozen=True)
class SnapshotRevision:
    """Identity used for compare-and-swap protection of loaded writers."""

    sequence: int
    generation: str


class SnapshotManifest(BaseModel):
    """The sole commit record for one complete FAISS + state generation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    format_version: Literal[1] = 1
    sequence: int = Field(ge=1)
    generation: str = Field(pattern=r"^[0-9a-f]{32}$")
    index_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    state_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class SnapshotPayload:
    """Verified paths and sidecar bytes selected by a committed manifest."""

    index_path: Path
    state_bytes: bytes
    revision: SnapshotRevision


class SnapshotStore:
    """Generation-addressed snapshot persistence rooted at one index directory."""

    def __init__(self, directory: Path) -> None:
        self._snapshots = directory / _SNAPSHOT_DIR
        _durable_mkdir(self._snapshots)

    @staticmethod
    def prepare_root(directory: Path) -> None:
        """Create an index root and persist that new entry in its parent directory."""
        _durable_mkdir(directory)

    def commit(
        self,
        write_index: Callable[[Path], None],
        state_bytes: bytes,
        expected_revision: SnapshotRevision | None = None,
    ) -> SnapshotRevision:
        """Stage both data files, then commit them with one atomic manifest rename."""
        self._assert_current(expected_revision)
        sequence = self._next_sequence()
        generation = uuid4().hex
        return self._write_generation(write_index, state_bytes, sequence, generation)

    def _write_generation(
        self,
        write_index: Callable[[Path], None],
        state_bytes: bytes,
        sequence: int,
        generation: str,
    ) -> SnapshotRevision:
        paths = self._generation_paths(generation)
        try:
            _stage_generation(paths, write_index, state_bytes)
            self._commit_manifest(paths, sequence)
            return SnapshotRevision(sequence, generation)
        finally:
            paths.index_temp.unlink(missing_ok=True)
            paths.state_temp.unlink(missing_ok=True)

    def latest(self) -> SnapshotPayload:
        """Load the newest valid commit, falling back only to an older complete one."""
        failures: list[OSError | ValueError] = []
        for manifest_path in sorted(self._manifest_paths(), key=_manifest_sequence, reverse=True):
            try:
                return self._payload(manifest_path)
            except (OSError, ValueError) as exc:
                failures.append(exc)
        raise ValueError("no complete vector-index snapshot could be recovered") from failures[0]

    def _payload(self, manifest_path: Path) -> SnapshotPayload:
        manifest = SnapshotManifest.model_validate_json(manifest_path.read_bytes())
        if manifest.sequence != _manifest_sequence(manifest_path):
            raise ValueError("snapshot manifest sequence does not match its filename")
        paths = self._generation_paths(manifest.generation)
        _verify_digest(paths.index_final, manifest.index_sha256, "FAISS index")
        _verify_digest(paths.state_final, manifest.state_sha256, "state sidecar")
        revision = SnapshotRevision(manifest.sequence, manifest.generation)
        return SnapshotPayload(paths.index_final, paths.state_final.read_bytes(), revision)

    def _assert_current(self, expected: SnapshotRevision | None) -> None:
        if expected is not None and self.latest().revision != expected:
            raise SnapshotConflictError("vector-index snapshot changed since this instance loaded")

    def _manifest_paths(self) -> list[Path]:
        paths = [path for path in self._snapshots.iterdir() if _MANIFEST_NAME.fullmatch(path.name)]
        if not paths:
            raise NoSnapshotError("no committed vector-index snapshot")
        return paths

    def _next_sequence(self) -> int:
        try:
            complete = self._complete_manifests(self._manifest_paths())
        except FileNotFoundError:
            return 1
        if not complete:
            return 1
        current = max(_manifest_sequence(path) for path in complete)
        if current >= _MAX_SEQUENCE:
            raise SnapshotSequenceError("vector-index snapshot sequence space is exhausted")
        return current + 1

    def _generation_paths(self, generation: str) -> _GenerationPaths:
        prefix = self._snapshots / generation
        return _GenerationPaths(
            index_temp=prefix.with_suffix(".faiss.tmp"),
            state_temp=prefix.with_suffix(".state.json.tmp"),
            index_final=prefix.with_suffix(".faiss"),
            state_final=prefix.with_suffix(".state.json"),
        )

    def _commit_manifest(self, paths: _GenerationPaths, sequence: int) -> None:
        manifest = _manifest_for(paths, sequence)
        temp = self._snapshots / f".{uuid4().hex}.manifest.tmp"
        try:
            _write_fsynced(temp, manifest.model_dump_json().encode("utf-8"))
            os.replace(temp, self._manifest_path(sequence))
            _fsync_directory(self._snapshots)
            self._collect_old_generations()
        finally:
            temp.unlink(missing_ok=True)

    def _manifest_path(self, sequence: int) -> Path:
        if not 1 <= sequence <= _MAX_SEQUENCE:
            raise SnapshotSequenceError("vector-index snapshot sequence space is exhausted")
        return self._snapshots / f"{sequence:020d}{_MANIFEST_SUFFIX}"

    def _collect_old_generations(self) -> None:
        manifests = sorted(self._manifest_paths(), key=_manifest_sequence, reverse=True)
        keep = self._complete_manifests(manifests)[:_RETAIN_GENERATIONS]
        generations = {self._read_manifest(path).generation for path in keep}
        for path in manifests:
            if path not in keep:
                path.unlink(missing_ok=True)
        self._remove_unreferenced_data(generations)
        _fsync_directory(self._snapshots)

    def _complete_manifests(self, manifests: list[Path]) -> list[Path]:
        complete: list[Path] = []
        for path in manifests:
            try:
                self._payload(path)
            except (OSError, ValueError):
                continue
            complete.append(path)
        return complete

    def _read_manifest(self, path: Path) -> SnapshotManifest:
        return SnapshotManifest.model_validate_json(path.read_bytes())

    def _remove_unreferenced_data(self, generations: set[str]) -> None:
        for path in self._snapshots.iterdir():
            if _is_generation_data(path) and _data_generation(path) not in generations:
                path.unlink(missing_ok=True)


@dataclass(frozen=True)
class _GenerationPaths:
    index_temp: Path
    state_temp: Path
    index_final: Path
    state_final: Path


def _manifest_for(paths: _GenerationPaths, sequence: int) -> SnapshotManifest:
    return SnapshotManifest(
        sequence=sequence,
        generation=paths.index_final.stem,
        index_sha256=_digest(paths.index_final),
        state_sha256=_digest(paths.state_final),
    )


def _stage_generation(
    paths: _GenerationPaths, write_index: Callable[[Path], None], state_bytes: bytes
) -> None:
    write_index(paths.index_temp)
    _fsync_file(paths.index_temp)
    _write_fsynced(paths.state_temp, state_bytes)
    os.replace(paths.index_temp, paths.index_final)
    os.replace(paths.state_temp, paths.state_final)


def _manifest_sequence(path: Path) -> int:
    return int(path.name.removesuffix(_MANIFEST_SUFFIX))


def _is_generation_data(path: Path) -> bool:
    return path.name.endswith((".faiss", ".state.json", ".faiss.tmp", ".state.json.tmp"))


def _data_generation(path: Path) -> str:
    return path.name.split(".", maxsplit=1)[0]


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(_HASH_BLOCK_BYTES):
            digest.update(block)
    return digest.hexdigest()


def _verify_digest(path: Path, expected: str, label: str) -> None:
    if _digest(path) != expected:
        raise ValueError(f"persisted {label} digest does not match snapshot manifest")


def _write_fsynced(path: Path, data: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def _fsync_file(path: Path) -> None:
    with path.open("rb") as handle:
        os.fsync(handle.fileno())


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _durable_mkdir(path: Path) -> None:
    """Create every missing component and durably link it from its parent."""
    missing: list[Path] = []
    cursor = path
    while not cursor.exists():
        missing.append(cursor)
        cursor = cursor.parent
    for directory in reversed(missing):
        directory.mkdir(exist_ok=True)
        _fsync_directory(directory.parent)
