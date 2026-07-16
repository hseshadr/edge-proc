"""Adversarial concurrency tests for the filesystem bundle transaction boundary."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from threading import Event, Thread

from edgeproc.bundles.adapters import FilesystemAdapter
from edgeproc.bundles.cas import FilesystemCacheStore, IntegrityError
from edgeproc.bundles.chunking import GearCDC
from edgeproc.bundles.manifest import VersionPointer
from edgeproc.bundles.signing import Ed25519Signer, Ed25519Verifier, generate_keypair
from edgeproc.bundles.sync import sync_index
from tests.bundles import _producer


class _BlockingAdapter:
    """Pause a sync after it owns the transaction but before it mutates the CAS."""

    def __init__(self) -> None:
        self.entered = Event()
        self.release = Event()
        self._delegate = FilesystemAdapter()

    def fetch_bytes(self, url: str) -> bytes:
        if url.endswith("/latest"):
            self.entered.set()
            assert self.release.wait(timeout=2)
        return self._delegate.fetch_bytes(url)


def _run(target: Callable[[], object], done: Event, errors: list[BaseException]) -> None:
    try:
        target()
    except BaseException as exc:  # pragma: no cover - asserted by the parent thread
        errors.append(exc)
    finally:
        done.set()


def _pointer(sequence: int) -> VersionPointer:
    return VersionPointer(
        manifest_hash=f"{sequence:064x}",
        version=f"1.0.{sequence}",
        sequence=sequence,
        signature="s",
    )


def test_promote_waits_for_an_existing_store_mutation(tmp_path: Path) -> None:
    first = FilesystemCacheStore(tmp_path)
    second = FilesystemCacheStore(tmp_path)
    first.promote(_pointer(0))
    done, errors = Event(), []
    worker = Thread(target=_run, args=(lambda: second.promote(_pointer(1)), done, errors))

    with first.mutation():
        worker.start()
        raced = done.wait(timeout=0.1)
    worker.join(timeout=2)

    assert raced is False
    assert errors == []
    assert first.read_active() == _pointer(1)


def test_sync_holds_mutation_lock_against_gc(tmp_path: Path) -> None:
    private, public = generate_keypair()
    origin = tmp_path / "origin"
    _producer.build_origin(
        files={"index.bin": b"new index"},
        origin=origin,
        chunker=GearCDC(),
        signer=Ed25519Signer(private),
    )
    verifier = Ed25519Verifier.from_public_bytes(public.public_bytes_raw())
    store, adapter = FilesystemCacheStore(tmp_path / "cache"), _BlockingAdapter()
    sync_done, gc_done, errors = Event(), Event(), []

    def run_sync() -> object:
        return sync_index(base_url=str(origin), store=store, adapter=adapter, verifier=verifier)

    sync_worker = Thread(target=_run, args=(run_sync, sync_done, errors))
    gc_worker = Thread(target=_run, args=(store.gc, gc_done, errors))

    sync_worker.start()
    assert adapter.entered.wait(timeout=1)
    gc_worker.start()
    raced = gc_done.wait(timeout=0.1)
    adapter.release.set()
    sync_worker.join(timeout=2)
    gc_worker.join(timeout=2)

    assert raced is False
    assert sync_done.is_set()
    assert gc_done.is_set()
    assert errors == []


def test_mutation_lock_timeout_is_bounded_and_typed(tmp_path: Path) -> None:
    first = FilesystemCacheStore(tmp_path)
    second = FilesystemCacheStore(tmp_path, mutation_lock_timeout=0.01)
    done, errors = Event(), []

    with first.mutation():
        worker = Thread(target=_run, args=(lambda: second.promote(_pointer(1)), done, errors))
        worker.start()
        assert done.wait(timeout=1)
    worker.join(timeout=1)

    assert len(errors) == 1
    assert isinstance(errors[0], IntegrityError)
    assert "mutation lock" in str(errors[0])
