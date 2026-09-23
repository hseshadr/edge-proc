"""CLI: ``keygen`` prints the key_id, ``keyring`` subcommands, stamping on ``publish``,
and ``sync`` against a JSON keyring trust root."""

from __future__ import annotations

import importlib
import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner, Result

from edgeproc.bundles.keyring import KEYRING_SCHEMA, parse_keyring
from edgeproc.bundles.manifest import VersionPointer
from edgeproc.bundles.signing import key_id_for
from edgeproc.cli import app

runner = CliRunner()
_cli_app_module = importlib.import_module("edgeproc.cli.app")
_NOW = 1_767_225_000


def _keygen(out: Path) -> str:
    result = runner.invoke(app, ["keygen", "--out", str(out)])
    assert result.exit_code == 0, result.output
    return key_id_for((out / "public.key").read_bytes())


def _invoke(*args: str) -> Result:
    return runner.invoke(app, list(args))


# --- keygen -----------------------------------------------------------------------------


def test_keygen_prints_the_key_id_after_the_unchanged_first_line(tmp_path: Path) -> None:
    result = _invoke("keygen", "--out", str(tmp_path))
    assert result.exit_code == 0
    lines = result.stdout.splitlines()
    assert lines[0] == f"wrote {tmp_path / 'private.key'} and {tmp_path / 'public.key'}"
    assert lines[1] == f"key_id {key_id_for((tmp_path / 'public.key').read_bytes())}"


# --- keyring init / add / revoke / show -------------------------------------------------


def test_keyring_init_writes_a_canonical_keyring(tmp_path: Path) -> None:
    id_a = _keygen(tmp_path / "a")
    id_b = _keygen(tmp_path / "b")
    ring_path = tmp_path / "keyring.json"
    result = _invoke(
        "keyring",
        "init",
        str(tmp_path / "b" / "public.key"),
        str(tmp_path / "a" / "public.key"),
        "--out",
        str(ring_path),
    )
    assert result.exit_code == 0, result.output
    doc = json.loads(ring_path.read_text())
    assert doc["schema"] == KEYRING_SCHEMA
    assert [k["key_id"] for k in doc["keys"]] == sorted([id_a, id_b])
    assert doc["revoked"] == []
    assert oct(ring_path.stat().st_mode & 0o777) == "0o644"


def test_keyring_init_refuses_to_overwrite(tmp_path: Path) -> None:
    _keygen(tmp_path / "a")
    ring_path = tmp_path / "keyring.json"
    ring_path.write_text("keep me")
    result = _invoke("keyring", "init", str(tmp_path / "a" / "public.key"), "--out", str(ring_path))
    assert result.exit_code == 1
    assert ring_path.read_text() == "keep me"
    assert "[config.invalid]" in result.stderr


def test_keyring_init_refuses_a_malformed_public_key(tmp_path: Path) -> None:
    bad = tmp_path / "bad.key"
    bad.write_bytes(b"short")
    result = _invoke("keyring", "init", str(bad), "--out", str(tmp_path / "k.json"))
    assert result.exit_code == 1
    assert "[config.invalid]" in result.stderr
    assert not (tmp_path / "k.json").exists()


def test_keyring_init_refuses_a_missing_public_key(tmp_path: Path) -> None:
    result = _invoke(
        "keyring", "init", str(tmp_path / "absent.key"), "--out", str(tmp_path / "k.json")
    )
    assert result.exit_code == 1
    assert "[config.missing]" in result.stderr


def test_keyring_init_refuses_duplicate_keys(tmp_path: Path) -> None:
    _keygen(tmp_path / "a")
    pub = str(tmp_path / "a" / "public.key")
    result = _invoke("keyring", "init", pub, pub, "--out", str(tmp_path / "k.json"))
    assert result.exit_code == 1
    assert "[config.invalid]" in result.stderr


def _init(tmp_path: Path, *names: str) -> Path:
    ring_path = tmp_path / "keyring.json"
    pubs = [str(tmp_path / n / "public.key") for n in names]
    assert _invoke("keyring", "init", *pubs, "--out", str(ring_path)).exit_code == 0
    return ring_path


def test_keyring_add_and_revoke_edit_the_file_in_place(tmp_path: Path) -> None:
    id_a = _keygen(tmp_path / "a")
    id_b = _keygen(tmp_path / "b")
    ring_path = _init(tmp_path, "a")

    added = _invoke("keyring", "add", str(ring_path), str(tmp_path / "b" / "public.key"))
    assert added.exit_code == 0, added.output
    assert id_b in added.stdout
    revoked = _invoke("keyring", "revoke", str(ring_path), id_a)
    assert revoked.exit_code == 0, revoked.output
    assert id_a in revoked.stdout

    ring = parse_keyring(ring_path.read_bytes())
    assert {e.key_id for e in ring.keys} == {id_a, id_b}
    assert ring.revoked == [id_a]
    # Idempotent: revoking again is a success that changes nothing.
    before = ring_path.read_bytes()
    assert _invoke("keyring", "revoke", str(ring_path), id_a).exit_code == 0
    assert ring_path.read_bytes() == before


def test_keyring_revoke_refuses_to_revoke_the_last_active_key(tmp_path: Path) -> None:
    id_a = _keygen(tmp_path / "a")
    ring_path = _init(tmp_path, "a")
    before = ring_path.read_bytes()
    result = _invoke("keyring", "revoke", str(ring_path), id_a)
    assert result.exit_code == 1
    assert "[config.invalid]" in result.stderr
    assert ring_path.read_bytes() == before


def test_keyring_revoke_refuses_a_malformed_key_id(tmp_path: Path) -> None:
    _keygen(tmp_path / "a")
    ring_path = _init(tmp_path, "a")
    result = _invoke("keyring", "revoke", str(ring_path), "nope")
    assert result.exit_code == 1
    assert "[config.invalid]" in result.stderr


def test_keyring_add_refuses_a_duplicate(tmp_path: Path) -> None:
    _keygen(tmp_path / "a")
    ring_path = _init(tmp_path, "a")
    result = _invoke("keyring", "add", str(ring_path), str(tmp_path / "a" / "public.key"))
    assert result.exit_code == 1
    assert "already" in result.stderr


def test_keyring_add_refuses_a_legacy_raw_key_as_the_keyring(tmp_path: Path) -> None:
    # Editing a raw public.key "in place" would overwrite the pin; it must be converted
    # explicitly with `keyring init`.
    _keygen(tmp_path / "a")
    _keygen(tmp_path / "b")
    pin = tmp_path / "a" / "public.key"
    before = pin.read_bytes()
    result = _invoke("keyring", "add", str(pin), str(tmp_path / "b" / "public.key"))
    assert result.exit_code == 1
    assert "[config.invalid]" in result.stderr
    assert pin.read_bytes() == before


def test_keyring_edit_refuses_a_missing_keyring(tmp_path: Path) -> None:
    result = _invoke("keyring", "revoke", str(tmp_path / "absent.json"), "0" * 16)
    assert result.exit_code == 1
    assert "[config.missing]" in result.stderr


def test_keyring_edit_refuses_a_symlinked_keyring(tmp_path: Path) -> None:
    _keygen(tmp_path / "a")
    _keygen(tmp_path / "b")
    real = _init(tmp_path, "a")
    link = tmp_path / "link.json"
    link.symlink_to(real)
    before = real.read_bytes()
    result = _invoke("keyring", "add", str(link), str(tmp_path / "b" / "public.key"))
    assert result.exit_code == 1
    assert real.read_bytes() == before


def test_keyring_show_is_deterministic_json(tmp_path: Path) -> None:
    id_a = _keygen(tmp_path / "a")
    id_b = _keygen(tmp_path / "b")
    ring_path = _init(tmp_path, "a", "b")
    assert _invoke("keyring", "revoke", str(ring_path), id_a).exit_code == 0

    first = _invoke("keyring", "show", str(ring_path))
    second = _invoke("keyring", "show", str(ring_path))
    assert first.exit_code == 0
    assert first.stdout == second.stdout
    doc = json.loads(first.stdout)
    status = {k["key_id"]: k["status"] for k in doc["keys"]}
    assert status == {id_a: "revoked", id_b: "active"}
    assert doc["revoked"] == [id_a]


def test_keyring_show_pretty_lists_each_key_and_orphan_revocations(tmp_path: Path) -> None:
    id_a = _keygen(tmp_path / "a")
    id_b = _keygen(tmp_path / "b")
    ring_path = _init(tmp_path, "b")
    assert _invoke("keyring", "revoke", str(ring_path), id_a).exit_code == 0
    result = _invoke("keyring", "show", str(ring_path), "--pretty")
    assert result.exit_code == 0
    assert result.stdout.splitlines() == [f"{id_b}  active", f"{id_a}  revoked (no public key)"]


def test_keyring_show_reads_a_legacy_raw_key_as_a_keyring_of_one(tmp_path: Path) -> None:
    id_a = _keygen(tmp_path / "a")
    result = _invoke("keyring", "show", str(tmp_path / "a" / "public.key"), "--pretty")
    assert result.exit_code == 0
    assert result.stdout.splitlines() == [f"{id_a}  active"]


def test_keyring_show_refuses_a_malformed_file(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{}")
    result = _invoke("keyring", "show", str(bad))
    assert result.exit_code == 1
    assert "[config.invalid]" in result.stderr


# --- publish stamping + sync against a keyring -----------------------------------------


def _src(tmp_path: Path) -> Path:
    src = tmp_path / "src"
    src.mkdir(exist_ok=True)
    (src / "data.txt").write_text("hello")
    return src


def _publish(tmp_path: Path, keys: str, sequence: int, *extra: str) -> Result:
    return _invoke(
        "publish",
        "--src",
        str(_src(tmp_path)),
        "--origin-dir",
        str(tmp_path / "origin"),
        "--key",
        str(tmp_path / keys / "private.key"),
        "--bundle-id",
        "demo",
        "--version",
        f"{sequence}.0.0",
        "--sequence",
        str(sequence),
        *extra,
    )


@pytest.fixture
def fixed_now(monkeypatch: pytest.MonkeyPatch) -> int:
    monkeypatch.setattr(_cli_app_module, "_now", lambda: _NOW)
    monkeypatch.delenv("EDGEPROC_PUBLISH_STAMP_KEY_ID", raising=False)
    monkeypatch.delenv("EDGEPROC_PUBLISH_EXPIRES_IN", raising=False)
    return _NOW


def test_publish_default_output_is_unchanged(tmp_path: Path, fixed_now: int) -> None:
    _keygen(tmp_path / "a")
    result = _publish(tmp_path, "a", 1)
    assert result.exit_code == 0, result.output
    assert "key_id" not in result.stdout
    assert "expires_at" not in result.stdout
    latest = (tmp_path / "origin" / "latest").read_bytes()
    assert b"key_id" not in latest
    assert b"expires_at" not in latest


def test_publish_stamps_key_id_and_expiry_on_request(tmp_path: Path, fixed_now: int) -> None:
    id_a = _keygen(tmp_path / "a")
    result = _publish(tmp_path, "a", 1, "--stamp-key-id", "--expires-in", "7d")
    assert result.exit_code == 0, result.output
    pointer = VersionPointer.model_validate_json((tmp_path / "origin" / "latest").read_bytes())
    assert pointer.key_id == id_a
    assert pointer.expires_at == fixed_now + 7 * 86_400


def test_publish_reads_stamping_defaults_from_settings(
    tmp_path: Path, fixed_now: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    id_a = _keygen(tmp_path / "a")
    monkeypatch.setenv("EDGEPROC_PUBLISH_STAMP_KEY_ID", "true")
    monkeypatch.setenv("EDGEPROC_PUBLISH_EXPIRES_IN", "3600")
    assert _publish(tmp_path, "a", 1).exit_code == 0
    pointer = VersionPointer.model_validate_json((tmp_path / "origin" / "latest").read_bytes())
    assert pointer.key_id == id_a
    assert pointer.expires_at == fixed_now + 3600


def test_publish_flags_override_settings(
    tmp_path: Path, fixed_now: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    _keygen(tmp_path / "a")
    monkeypatch.setenv("EDGEPROC_PUBLISH_STAMP_KEY_ID", "true")
    monkeypatch.setenv("EDGEPROC_PUBLISH_EXPIRES_IN", "3600")
    assert _publish(tmp_path, "a", 1, "--no-stamp-key-id", "--expires-in", "60s").exit_code == 0
    pointer = VersionPointer.model_validate_json((tmp_path / "origin" / "latest").read_bytes())
    assert pointer.key_id is None
    assert pointer.expires_at == fixed_now + 60


@pytest.mark.parametrize("bad", ["0", "-5", "7x", "", "1.5h", "d"])
def test_publish_refuses_a_malformed_expiry(tmp_path: Path, fixed_now: int, bad: str) -> None:
    _keygen(tmp_path / "a")
    result = _publish(tmp_path, "a", 1, "--expires-in", bad)
    assert result.exit_code == 1
    assert "[config.invalid]" in result.stderr
    assert not (tmp_path / "origin" / "latest").exists()


def test_publish_refuses_a_malformed_expiry_setting(
    tmp_path: Path, fixed_now: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    _keygen(tmp_path / "a")
    monkeypatch.setenv("EDGEPROC_PUBLISH_EXPIRES_IN", "soon")
    result = _publish(tmp_path, "a", 1)
    assert result.exit_code == 1
    assert "[config.invalid]" in result.stderr


def _sync(tmp_path: Path, key: Path, cache: str = "cache") -> Result:
    return _invoke(
        "sync",
        "--base-url",
        str(tmp_path / "origin"),
        "--cache-dir",
        str(tmp_path / cache),
        "--key",
        str(key),
    )


def test_sync_accepts_a_json_keyring_trust_root(tmp_path: Path, fixed_now: int) -> None:
    _keygen(tmp_path / "a")
    _keygen(tmp_path / "b")
    ring_path = _init(tmp_path, "a", "b")
    assert _publish(tmp_path, "b", 1, "--stamp-key-id").exit_code == 0
    result = _sync(tmp_path, ring_path)
    assert result.exit_code == 0, result.output


def test_sync_refuses_a_revoked_key_with_the_integrity_code(tmp_path: Path, fixed_now: int) -> None:
    id_a = _keygen(tmp_path / "a")
    _keygen(tmp_path / "b")
    ring_path = _init(tmp_path, "a", "b")
    assert _invoke("keyring", "revoke", str(ring_path), id_a).exit_code == 0
    assert _publish(tmp_path, "a", 1, "--stamp-key-id").exit_code == 0
    result = _sync(tmp_path, ring_path)
    assert result.exit_code == 1
    assert "[bundle.integrity_failed]" in result.stderr
    assert "revoked" in result.stderr


def test_sync_refuses_an_expired_pointer(
    tmp_path: Path, fixed_now: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    _keygen(tmp_path / "a")
    monkeypatch.setattr(_cli_app_module, "_now", lambda: 1_000)  # published long ago
    assert _publish(tmp_path, "a", 1, "--expires-in", "60").exit_code == 0
    result = _sync(tmp_path, tmp_path / "a" / "public.key")
    assert result.exit_code == 1
    assert "[bundle.integrity_failed]" in result.stderr
    assert "expired" in result.stderr


def test_sync_with_a_malformed_keyring_fails_closed(tmp_path: Path) -> None:
    bad = tmp_path / "keyring.json"
    bad.write_text(json.dumps({"schema": KEYRING_SCHEMA, "keys": [], "revoked": []}))
    result = _sync(tmp_path, bad)
    assert result.exit_code == 1
    assert "malformed trust-root key" in result.stderr
    assert "[config.invalid]" in result.stderr


def test_now_seam_reads_the_wall_clock() -> None:
    import time  # noqa: PLC0415

    assert abs(_cli_app_module._now() - int(time.time())) <= 2


def test_keyring_file_written_by_cli_is_not_a_symlink_follower(tmp_path: Path) -> None:
    # A planted symlink at the init target is refused, never followed.
    _keygen(tmp_path / "a")
    victim = tmp_path / "victim"
    victim.write_text("victim")
    link = tmp_path / "keyring.json"
    os.symlink(victim, link)
    result = _invoke("keyring", "init", str(tmp_path / "a" / "public.key"), "--out", str(link))
    assert result.exit_code == 1
    assert victim.read_text() == "victim"


def test_keyring_init_into_a_missing_directory_fails_closed(tmp_path: Path) -> None:
    _keygen(tmp_path / "a")
    out = tmp_path / "absent-dir" / "keyring.json"
    result = _invoke("keyring", "init", str(tmp_path / "a" / "public.key"), "--out", str(out))
    assert result.exit_code == 1
    assert "[config.invalid]" in result.stderr
    assert "Traceback" not in result.output


def test_keyring_edit_refuses_when_its_temp_sibling_is_taken(tmp_path: Path) -> None:
    # The temp file is created O_EXCL|O_NOFOLLOW: a pre-planted file there is never reused.
    _keygen(tmp_path / "a")
    _keygen(tmp_path / "b")
    ring_path = _init(tmp_path, "a")
    planted = ring_path.with_name(f".{ring_path.name}.{os.getpid()}.tmp")
    planted.write_text("planted")
    before = ring_path.read_bytes()
    result = _invoke("keyring", "add", str(ring_path), str(tmp_path / "b" / "public.key"))
    assert result.exit_code == 1
    assert ring_path.read_bytes() == before
    assert planted.read_text() == "planted"


def test_keyring_edit_leaves_the_file_intact_when_the_swap_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _keygen(tmp_path / "a")
    _keygen(tmp_path / "b")
    ring_path = _init(tmp_path, "a")
    before = ring_path.read_bytes()

    def _broken_replace(src: object, dst: object) -> None:
        raise OSError(5, "simulated I/O failure")

    monkeypatch.setattr(_cli_app_module.os, "replace", _broken_replace)
    result = _invoke("keyring", "add", str(ring_path), str(tmp_path / "b" / "public.key"))
    assert result.exit_code == 1
    assert "simulated I/O failure" in result.stderr
    assert ring_path.read_bytes() == before
    assert list(tmp_path.glob(".keyring.json.*.tmp")) == []
