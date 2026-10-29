"""BM25 and reciprocal rank fusion (pure)."""

from app.search.rank import TermStats, bm25, rrf


def test_bm25_rare_terms_outweigh_common_ones():
    # "creator" is in one chunk of 10; "pattern" is in all of them.
    stats = TermStats(10, 20.0, {
        "a": (20, {"pattern": 3}),
        "b": (20, {"creator": 1}),
        **{f"x{i}": (20, {"pattern": 1}) for i in range(8)},
    })  # fmt: skip
    ranked = [cid for cid, _ in bm25(stats)]
    assert ranked[0] == "b"
    assert ranked[1] == "a"  # more occurrences of the common term still beats one


def test_bm25_prefers_shorter_chunks_for_the_same_count():
    stats = TermStats(3, 50.0, {"long": (120, {"guard": 2}), "short": (15, {"guard": 2})})
    assert [cid for cid, _ in bm25(stats)] == ["short", "long"]


def test_bm25_empty():
    assert bm25(TermStats(0, 0.0)) == []
    assert bm25(TermStats(5, 10.0)) == []


def test_rrf_rewards_agreement_between_lists():
    keyword = ["k1", "both", "k3"]
    embedding = ["e1", "e2", "both"]
    fused = [item for item, _ in rrf([keyword, embedding])]
    assert fused[0] == "both"  # 1/62 + 1/63 beats 1/61 alone
    assert set(fused) == {"k1", "k3", "e1", "e2", "both"}


def test_rrf_ties_go_to_the_better_single_rank_then_id():
    fused = rrf([["a", "b"], ["c", "a"], ["b", "c"]])
    # a: 1/61 + 1/62; b: 1/62 + 1/61; c: 1/61 + 1/62 -> equal scores, all best rank 1
    assert [item for item, _ in fused] == ["a", "b", "c"]
    fused = rrf([["x", "y"], ["y"]], k=1)
    assert [item for item, _ in fused] == ["y", "x"]  # 1/3 + 1/2 > 1/2
