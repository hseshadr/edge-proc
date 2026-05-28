"""Reciprocal Rank Fusion — pure, deterministic, order-only fusion of two ranked lists."""

from __future__ import annotations

from edgeproc.localvec.fusion import reciprocal_rank_fusion


def test_document_in_both_lists_outranks_singletons() -> None:
    keyword = [("a", 9.0), ("b", 3.0)]
    vector = [("a", 0.9), ("c", 0.8)]
    ranked = reciprocal_rank_fusion(keyword, vector)
    assert ranked[0][0] == "a"  # appears in both → highest fused score
    assert {doc for doc, _ in ranked} == {"a", "b", "c"}


def test_empty_inputs_yield_empty_output() -> None:
    assert reciprocal_rank_fusion([], []) == []


def test_rank_position_dominates_raw_score() -> None:
    # RRF ignores raw scores; only rank position matters.
    keyword = [("first", 0.001), ("second", 999.0)]
    ranked = reciprocal_rank_fusion(keyword, [])
    assert [doc for doc, _ in ranked] == ["first", "second"]
