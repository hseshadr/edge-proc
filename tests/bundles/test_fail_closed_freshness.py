"""Freshness must be PROVED at promote — an unprovable pointer is refused, not waved through.

Three fail-OPEN defects this module pins closed. Each is driven as the real attack through
the real code path (``build_bundle`` -> ``sync_index`` -> ``promote``) and asserts the
attack is REFUSED — never that a predicate returned ``False`` in isolation:

- **Anti-rollback bypassed by a non-PEP 440 version.** ``Version("2019-01-01")`` raises
  ``InvalidVersion``; the guard swallowed that and answered "not a downgrade", so any
  publisher using date-style versions had NO anti-rollback at all. Measured: a 2019
  pointer replaced a 2026 one on a device that had already installed the 2026 bundle.
- **Anti-replay bypassed by an absent sequence.** A device running ``sequence=7`` accepted
  a validly signed older pointer carrying no sequence at all: the counter comparison
  answered "fresh" whenever *either* side was ``None``, so deleting the freshness evidence
  was enough to defeat the guard that evidence exists to power.
- **Anti-replay bypassed by an EQUAL version.** ``Version(a) < Version(b)`` is ``False``
  when the two are equal, and the guard read that single ``False`` as affirmative PROOF of
  freshness. So a publisher that ships two bundles under one label — and every publisher
  that carries no monotonic counter at all — had NO anti-replay: any earlier, genuinely
  signed pointer at the same version replaced the installed one and moved content backwards.

The rule all three now obey: a promote must PROVE it is at least as fresh as the active
pointer — by a strictly-greater monotonic ``sequence``, or by a strictly-greater PEP 440
``version``. No proof is a refusal. "I cannot tell whether this is a rollback" REJECTS.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from edgeproc.bundles.adapters import FilesystemAdapter
from edgeproc.bundles.cas import FilesystemCacheStore, RollbackError
from edgeproc.bundles.chunking import GearCDC
from edgeproc.bundles.manifest import VersionPointer
from edgeproc.bundles.publish import build_bundle
from edgeproc.bundles.signing import Ed25519Signer, Ed25519Verifier, generate_keypair
from edgeproc.bundles.sync import SyncResult, sync_index

# Distinct payloads, so a rollback is observable as CONTENT moving backwards, not just a label.
_OLD_FILES: dict[str, bytes] = {"catalog.json": b'{"generation":"old"}'}
_NEW_FILES: dict[str, bytes] = {"catalog.json": b'{"generation":"new"}'}


class _Publisher:
    """One signing key, many origins — the real producer, used to mint real bundles."""

    def __init__(self) -> None:
        private, public = generate_keypair()
        self.signer = Ed25519Signer(private)
        self.verifier = Ed25519Verifier.from_public_bytes(public.public_bytes_raw())

    def publish(
        self,
        origin: Path,
        *,
        version: str,
        files: dict[str, bytes],
        sequence: int | None = None,
    ) -> VersionPointer:
        return build_bundle(
            files=files,
            store=FilesystemCacheStore(origin),
            chunker=GearCDC(),
            signer=self.signer,
            bundle_id="catalog",
            version=version,
            sequence=sequence,
        )

    def sync(self, origin: Path, cache: Path) -> SyncResult:
        return sync_index(
            base_url=str(origin),
            store=FilesystemCacheStore(cache),
            adapter=FilesystemAdapter(),
            verifier=self.verifier,
        )


def _active(cache: Path) -> VersionPointer:
    pointer = FilesystemCacheStore(cache).read_active()
    assert pointer is not None, "the cache should have an active pointer"
    return pointer


# --- attack 1: rollback across versions PEP 440 cannot parse ---------------------------


def test_sync_refuses_a_replayed_pointer_whose_version_is_not_pep440(tmp_path: Path) -> None:
    """A 2019 date-versioned pointer must not replace an installed 2026 one.

    Nothing here is forged: both pointers are genuinely signed by the publisher's key. The
    attacker only replays the OLD `/latest`. With date versions the comparison raises
    ``InvalidVersion``, and the guard used to treat "cannot compare" as "allow".
    """
    # Given: a publisher that versions by date, and a device already on the 2026 bundle
    publisher = _Publisher()
    old_origin, new_origin, cache = (tmp_path / n for n in ("origin-2019", "origin-2026", "cache"))
    publisher.publish(old_origin, version="2019-01-01", files=_OLD_FILES)
    current = publisher.publish(new_origin, version="2026-07-01", files=_NEW_FILES)
    publisher.sync(new_origin, cache)

    # When: the attacker serves the genuinely signed 2019 pointer to that device
    with pytest.raises(RollbackError):
        publisher.sync(old_origin, cache)

    # Then: the device is still on 2026 — the downgrade never took effect
    assert _active(cache) == current
    assert _active(cache).version == "2026-07-01"


def test_sync_refuses_an_unparseable_version_over_a_parseable_active(tmp_path: Path) -> None:
    """Mixed forms are refused too: "cannot compare" is a refusal in either direction."""
    # Given
    publisher = _Publisher()
    weird_origin, good_origin, cache = (tmp_path / n for n in ("weird", "good", "cache"))
    publisher.publish(weird_origin, version="nightly-build", files=_OLD_FILES)
    current = publisher.publish(good_origin, version="2.0.0", files=_NEW_FILES)
    publisher.sync(good_origin, cache)

    # When / Then
    with pytest.raises(RollbackError):
        publisher.sync(weird_origin, cache)
    assert _active(cache) == current


# --- attack 2: replay of a pointer carrying no monotonic sequence ----------------------


def test_sync_refuses_a_replayed_pointer_whose_sequence_is_absent(tmp_path: Path) -> None:
    """A device on ``sequence=7`` must refuse a signed pointer that carries no sequence.

    The versions are EQUAL, so the PEP 440 guard has nothing to say and the monotonic
    counter is the only thing standing between the device and a content rollback. A legacy
    pointer (minted before sequences existed) is validly signed and carries ``None`` — the
    guard used to read that as "fresh".
    """
    # Given: a device running the sequenced bundle, and an older unsequenced one on file
    publisher = _Publisher()
    legacy_origin, current_origin, cache = (tmp_path / n for n in ("legacy", "current", "cache"))
    publisher.publish(legacy_origin, version="1.0.0", files=_OLD_FILES)  # no sequence
    current = publisher.publish(current_origin, version="1.0.0", files=_NEW_FILES, sequence=7)
    publisher.sync(current_origin, cache)
    assert _active(cache).sequence == 7

    # When: the attacker replays the unsequenced pointer at the same version
    with pytest.raises(RollbackError):
        publisher.sync(legacy_origin, cache)

    # Then: the active manifest is unchanged — the content did not roll back
    assert _active(cache) == current
    assert _active(cache).manifest_hash == current.manifest_hash


def test_promote_refuses_an_absent_sequence_against_a_sequenced_active(tmp_path: Path) -> None:
    """The same refusal at the store boundary, independent of the sync engine."""
    # Given
    store = FilesystemCacheStore(tmp_path)
    active = VersionPointer(manifest_hash="ab" * 32, version="1.0.0", sequence=7, signature="s")
    stripped = VersionPointer(manifest_hash="cd" * 32, version="1.0.0", signature="s")
    store.promote(active)

    # When / Then
    with pytest.raises(RollbackError):
        store.promote(stripped)
    assert store.read_active() == active


# --- attack 3: replay of a validly-signed pointer at an EQUAL version ------------------


def test_sync_refuses_a_replayed_pointer_at_an_equal_version(tmp_path: Path) -> None:
    """A device on v1.2.0 must refuse an EARLIER, genuinely signed pointer also at v1.2.0.

    The realistic shape: a publisher ships v1.2.0, finds a defect, and re-publishes the
    fix under the SAME label (no counter — ``sequence`` is optional and most publishers
    carry none). Both pointers are validly signed; the attacker forges nothing and only
    replays the first ``/latest`` from a stale mirror or a poisoned CDN edge.

    ``Version("1.2.0") < Version("1.2.0")`` is ``False``, and the guard read that single
    ``False`` as proof of freshness — so the replay promoted and the device's content
    silently moved BACKWARDS to the defective bundle under an unchanged version string.
    An equal version proves nothing about which bundle is newer. It must REJECT.
    """
    # Given: a device already running the re-published (fixed) v1.2.0 bundle
    publisher = _Publisher()
    stale_origin, fixed_origin, cache = (tmp_path / n for n in ("stale", "fixed", "cache"))
    publisher.publish(stale_origin, version="1.2.0", files=_OLD_FILES)
    current = publisher.publish(fixed_origin, version="1.2.0", files=_NEW_FILES)
    publisher.sync(fixed_origin, cache)
    assert _active(cache).manifest_hash == current.manifest_hash

    # When: the attacker replays the earlier, genuinely signed v1.2.0 pointer
    with pytest.raises(RollbackError):
        publisher.sync(stale_origin, cache)

    # Then: the content did not move backwards
    assert _active(cache) == current
    assert _active(cache).manifest_hash == current.manifest_hash


def test_promote_refuses_an_equal_version_it_cannot_prove_fresher(tmp_path: Path) -> None:
    """The same refusal at the store boundary, independent of the sync engine."""
    # Given
    store = FilesystemCacheStore(tmp_path)
    active = VersionPointer(manifest_hash="ab" * 32, version="1.2.0", signature="s")
    replay = VersionPointer(manifest_hash="cd" * 32, version="1.2.0", signature="s")
    store.promote(active)

    # When / Then
    with pytest.raises(RollbackError):
        store.promote(replay)
    assert store.read_active() == active


# --- the escape hatch the fail-closed rule must leave open -----------------------------


def test_re_promoting_the_identical_pointer_is_idempotent(tmp_path: Path) -> None:
    """Equal versions refuse a DIFFERENT pointer, never the byte-identical one.

    An interrupted sync that re-runs promotes the same pointer again. That is a no-op
    write, not equivocation, so it needs no freshness proof and must keep working.
    """
    # Given
    store = FilesystemCacheStore(tmp_path)
    pointer = VersionPointer(manifest_hash="ab" * 32, version="1.2.0", signature="s")

    # When
    store.promote(pointer)
    store.promote(pointer)

    # Then
    assert store.read_active() == pointer


def test_a_monotonic_sequence_still_ships_an_equal_version_republish(tmp_path: Path) -> None:
    """Fail-closed must not brick a publisher that re-ships under one label — ``sequence`` is.

    The no-regression half of the fix: an equal-version re-publish keeps shipping by
    binding a strictly-greater counter, and replaying the lower counter is still refused.
    """
    # Given: two bundles at the SAME version that DO carry monotonic counters
    publisher = _Publisher()
    first_origin, second_origin, cache = (tmp_path / n for n in ("first", "second", "cache"))
    publisher.publish(first_origin, version="1.2.0", files=_OLD_FILES, sequence=1)
    second = publisher.publish(second_origin, version="1.2.0", files=_NEW_FILES, sequence=2)

    # When: the device syncs both in order
    publisher.sync(first_origin, cache)
    publisher.sync(second_origin, cache)

    # Then: the equal-version forward move landed, and replaying the lower counter is refused
    assert _active(cache) == second
    with pytest.raises(RollbackError):
        publisher.sync(first_origin, cache)
    assert _active(cache) == second


def test_a_monotonic_sequence_still_ships_date_versioned_bundles(tmp_path: Path) -> None:
    """Fail-closed must not brick a publisher PEP 440 cannot parse — ``sequence`` is the proof.

    The no-regression half of the fix: a date-versioned publisher keeps shipping by binding
    a strictly-greater counter, and replaying the lower counter is still refused.
    """
    # Given: two date-versioned bundles that DO carry monotonic counters
    publisher = _Publisher()
    first_origin, second_origin, cache = (tmp_path / n for n in ("first", "second", "cache"))
    publisher.publish(first_origin, version="2019-01-01", files=_OLD_FILES, sequence=1)
    second = publisher.publish(second_origin, version="2026-07-01", files=_NEW_FILES, sequence=2)

    # When: the device syncs both in order
    publisher.sync(first_origin, cache)
    publisher.sync(second_origin, cache)

    # Then: the forward move landed, and replaying the lower counter is still refused
    assert _active(cache) == second
    with pytest.raises(RollbackError):
        publisher.sync(first_origin, cache)
    assert _active(cache) == second
