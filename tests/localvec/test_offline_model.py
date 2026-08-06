"""The offline claim, measured against a COLD cache — not the developer's warm one.

The README promises that after one ``sync`` a device needs no network at all to keep
answering queries. That is a claim about the *model*, not only the index. ``sync`` ships
the FAISS index; ``sentence-transformers`` resolves a bare repo id by calling
huggingface.co. On any machine that already has the model in its Hub cache that fetch is
invisible, every test passes, and the claim reads as true. It was not.

So these tests do two things nothing else in this suite does:

1. **Redirect every Hugging Face cache variable at a fresh empty directory**, so the model
   is genuinely absent. The developer's warm cache cannot make the assertion pass.
2. **Assert the loader is never constructed**, which is what "no egress" actually means
   here.

Point 2 is not the obvious choice, and the obvious choice is wrong. The first draft of
this file blocked ``socket.socket``/``socket.getaddrinfo`` and asserted the ledger stayed
empty. It went green while the model downloaded in front of it: ``huggingface_hub`` fetches
through ``hf_xet``, a Rust extension that never enters Python's ``socket`` module. The
ledger read zero because it was blind, not because nothing happened — a textbook guard
measuring shape. So the property under test is the one thing no native transport can slip
past: **``SentenceTransformer`` is never called at all.** A downloader that is never
reached cannot download, in any language.

``test_cold_device_refuses_...`` deliberately uses the real ``SentenceTransformer``.
Faking the download away is precisely the blind spot that let the false claim ship —
``test_encoder.py`` admits as much in its own docstring.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from edgeproc.core.settings import DEFAULT_MODEL, EdgeProcSettings
from edgeproc.errors import (
    BUNDLE_INTEGRITY_FAILED,
    CONFIG_INVALID,
    CONFIG_MISSING,
    code_of,
)
from edgeproc.localvec.encoder import TextEncoder
from edgeproc.localvec.model_source import (
    ModelDigestMismatchError,
    ModelNotLocalError,
    ModelPathInvalidError,
    digest_model_dir,
    resolve_model_source,
)

_HF_CACHE_VARS = (
    "HF_HOME",
    "HF_HUB_CACHE",
    "SENTENCE_TRANSFORMERS_HOME",
    "TRANSFORMERS_CACHE",
    "XDG_CACHE_HOME",
)

_EDGEPROC_MODEL_VARS = (
    "EDGEPROC_MODEL_PATH",
    "EDGEPROC_MODEL_DIGEST",
    "EDGEPROC_ALLOW_MODEL_DOWNLOAD",
    "HF_TOKEN",
)


@pytest.fixture
def cold_hf_cache(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point every Hugging Face cache variable at an empty dir: a genuinely cold device.

    Without this the machine's own warm cache answers the load and the assertion below
    passes for the wrong reason — which is exactly how the false claim survived review.
    """
    cold = tmp_path / "cold-hf"
    cold.mkdir()
    for var in _HF_CACHE_VARS:
        monkeypatch.setenv(var, str(cold))
    for var in _EDGEPROC_MODEL_VARS:
        monkeypatch.delenv(var, raising=False)
    return cold


@pytest.fixture
def loader_spy(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    """Record every ``SentenceTransformer`` construction, and never perform one.

    This is the honest instrument. A socket-level block cannot see ``hf_xet``'s Rust
    transport, but nothing downloads through a constructor that is never invoked, so an
    empty ledger here is a real zero-egress proof rather than a blind one.
    """
    calls: list[dict[str, object]] = []

    def _record(reference: str, **kwargs: object) -> object:
        calls.append({"reference": reference, **kwargs})
        raise AssertionError("loader must not be constructed in this test")

    monkeypatch.setattr("edgeproc.localvec.encoder.SentenceTransformer", _record)
    return calls


# --------------------------------------------------------------------------------------
# The claim itself: a cold device answers, or refuses — it never silently phones home.
# --------------------------------------------------------------------------------------


@pytest.mark.usefixtures("cold_hf_cache")
def test_cold_device_refuses_instead_of_fetching_the_model() -> None:
    """Default config + no local model = a refusal, using the REAL loader.

    This is the regression test for the false claim. Before the fix this call reached
    huggingface.co and returned a working encoder; the download is what made the README's
    "needs no network at all" read as true on any machine that had run it once.

    No fake is installed here on purpose. If the refusal ever regresses into a fetch, this
    test starts downloading 87 MB and stops passing for the right reason.
    """
    with pytest.raises(ModelNotLocalError):
        TextEncoder()


@pytest.mark.usefixtures("cold_hf_cache")
def test_cold_device_never_constructs_the_loader(loader_spy: list[dict[str, object]]) -> None:
    """The un-bypassable half: the refusal happens before any downloader exists.

    ``hf_xet`` fetches from Rust, so no Python-level network block can observe it. Proving
    the loader is never constructed is what makes "no egress" a fact instead of a hope.
    """
    with pytest.raises(ModelNotLocalError):
        TextEncoder()

    assert loader_spy == [], f"loader was constructed on a cold device: {loader_spy}"


@pytest.mark.usefixtures("cold_hf_cache")
def test_refusal_carries_the_canonical_config_missing_code() -> None:
    with pytest.raises(ModelNotLocalError) as caught:
        TextEncoder()

    assert code_of(caught.value) == CONFIG_MISSING


@pytest.mark.usefixtures("cold_hf_cache")
def test_refusal_names_both_supported_remedies() -> None:
    """A fail-closed refusal an operator cannot act on is just an outage."""
    with pytest.raises(ModelNotLocalError) as caught:
        TextEncoder()

    message = str(caught.value)
    assert "EDGEPROC_MODEL_PATH" in message
    assert "EDGEPROC_ALLOW_MODEL_DOWNLOAD" in message


@pytest.mark.usefixtures("cold_hf_cache")
def test_opt_in_is_the_only_path_that_reaches_the_loader(
    monkeypatch: pytest.MonkeyPatch, loader_spy: list[dict[str, object]]
) -> None:
    """The permission flag is load-bearing in BOTH directions.

    A guard that refuses everything is not a guard, it is an outage. The same cold cache
    that refuses above must reach the loader once — and only once — an operator has said
    the network is allowed, with ``local_files_only`` off so the fetch can actually happen.
    """
    monkeypatch.setenv("EDGEPROC_ALLOW_MODEL_DOWNLOAD", "1")

    with pytest.raises(AssertionError, match="must not be constructed"):
        TextEncoder()

    assert len(loader_spy) == 1, "opt-in did not reach the loader; the flag is not wired"
    assert loader_spy[0]["reference"] == DEFAULT_MODEL
    assert loader_spy[0]["local_files_only"] is False


@pytest.mark.usefixtures("cold_hf_cache")
def test_local_model_reaches_the_loader_pinned_to_local_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, loader_spy: list[dict[str, object]]
) -> None:
    """The offline path: a synced model dir loads, and is pinned local-only in the loader.

    ``local_files_only=True`` is defense in depth, not the guard — the guard already ran.
    It stops a hub round-trip for revision metadata on a path that must stay offline.
    """
    model_dir = _write_model_dir(tmp_path / "model")
    monkeypatch.setenv("EDGEPROC_MODEL_PATH", str(model_dir))

    with pytest.raises(AssertionError, match="must not be constructed"):
        TextEncoder()

    assert loader_spy[0]["reference"] == str(model_dir)
    assert loader_spy[0]["local_files_only"] is True


# --------------------------------------------------------------------------------------
# Source resolution: a local directory is the supported offline path.
# --------------------------------------------------------------------------------------


def _write_model_dir(root: Path) -> Path:
    """A stand-in model directory. Resolution is pure, so real weights are not needed."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "modules.json").write_text('[{"idx": 0, "path": ""}]')
    (root / "config.json").write_text('{"hidden_size": 384}')
    nested = root / "1_Pooling"
    nested.mkdir()
    (nested / "config.json").write_text('{"word_embedding_dimension": 384}')
    return root


def test_model_path_resolves_to_a_local_only_source(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    model_dir = _write_model_dir(tmp_path / "model")
    monkeypatch.setenv("EDGEPROC_MODEL_PATH", str(model_dir))

    source = resolve_model_source(EdgeProcSettings())

    assert source.reference == str(model_dir)
    assert source.local_files_only is True


def test_model_path_stays_local_only_even_when_download_is_permitted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An explicit local model is never a reason to reach the Hub anyway."""
    model_dir = _write_model_dir(tmp_path / "model")
    monkeypatch.setenv("EDGEPROC_MODEL_PATH", str(model_dir))
    monkeypatch.setenv("EDGEPROC_ALLOW_MODEL_DOWNLOAD", "1")

    assert resolve_model_source(EdgeProcSettings()).local_files_only is True


def test_missing_model_path_is_refused_as_invalid_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("EDGEPROC_MODEL_PATH", str(tmp_path / "not-there"))

    with pytest.raises(ModelPathInvalidError) as caught:
        resolve_model_source(EdgeProcSettings())

    assert code_of(caught.value) == CONFIG_INVALID


# --------------------------------------------------------------------------------------
# The digest pin: a locally-supplied model is verified, not merely trusted.
# --------------------------------------------------------------------------------------


def test_digest_is_stable_across_repeated_walks(tmp_path: Path) -> None:
    model_dir = _write_model_dir(tmp_path / "model")

    assert digest_model_dir(model_dir) == digest_model_dir(model_dir)


def test_digest_changes_when_a_single_byte_changes(tmp_path: Path) -> None:
    model_dir = _write_model_dir(tmp_path / "model")
    before = digest_model_dir(model_dir)

    (model_dir / "config.json").write_text('{"hidden_size": 385}')

    assert digest_model_dir(model_dir) != before


def test_digest_changes_when_content_moves_between_files(tmp_path: Path) -> None:
    """Hashing bytes alone would collide here; the relative path is hashed too."""
    model_dir = _write_model_dir(tmp_path / "model")
    (model_dir / "a.txt").write_text("alpha")
    (model_dir / "b.txt").write_text("beta")
    before = digest_model_dir(model_dir)

    (model_dir / "a.txt").write_text("beta")
    (model_dir / "b.txt").write_text("alpha")

    assert digest_model_dir(model_dir) != before


def test_pinned_digest_admits_the_matching_model(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    model_dir = _write_model_dir(tmp_path / "model")
    monkeypatch.setenv("EDGEPROC_MODEL_PATH", str(model_dir))
    monkeypatch.setenv("EDGEPROC_MODEL_DIGEST", digest_model_dir(model_dir))

    assert resolve_model_source(EdgeProcSettings()).reference == str(model_dir)


def test_pinned_digest_refuses_a_tampered_model(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Swapped weights are a supply-chain compromise, not a warning."""
    model_dir = _write_model_dir(tmp_path / "model")
    monkeypatch.setenv("EDGEPROC_MODEL_PATH", str(model_dir))
    monkeypatch.setenv("EDGEPROC_MODEL_DIGEST", digest_model_dir(model_dir))

    (model_dir / "config.json").write_text('{"hidden_size": 999}')

    with pytest.raises(ModelDigestMismatchError) as caught:
        resolve_model_source(EdgeProcSettings())

    assert code_of(caught.value) == BUNDLE_INTEGRITY_FAILED
