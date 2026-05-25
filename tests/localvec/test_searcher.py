"""KeywordSearcher: BM25 over text, decoupled from any domain model via from_texts."""

from __future__ import annotations

from edgeproc.localvec.searcher import KeywordSearcher


def test_ranks_documents_by_keyword_overlap() -> None:
    searcher = KeywordSearcher.from_texts(
        texts=["red running shoes", "blue hiking boots", "red dress"],
        ids=["a", "b", "c"],
    )
    results = searcher.search("red shoes", k=3)
    assert results[0][0] == "a"  # best keyword overlap
    assert "b" not in {doc for doc, _ in results}  # no overlap, score 0


def test_blank_query_returns_empty() -> None:
    searcher = KeywordSearcher.from_texts(texts=["anything"], ids=["a"])
    assert searcher.search("   ", k=5) == []


def test_empty_corpus_returns_empty() -> None:
    searcher = KeywordSearcher.from_texts(texts=[], ids=[])
    assert searcher.search("query", k=5) == []


def test_respects_k() -> None:
    # Distinct rare terms → positive IDF for each, so three docs genuinely match
    # and k actually limits the result (a term in *every* doc gets negative BM25 IDF).
    searcher = KeywordSearcher.from_texts(
        texts=["alpha", "beta", "gamma", "delta"],
        ids=["a", "b", "c", "d"],
    )
    assert len(searcher.search("alpha beta gamma", k=2)) == 2
