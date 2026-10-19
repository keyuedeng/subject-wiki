"""DP alignment on synthetic similarity matrices (no embeddings)."""

import numpy as np
import pytest

from app.transcripts.align import AlignParams, align, cosine_matrix, move_costs

HIT, BASE = 0.40, 0.20


def matrix(truth: list[int | None], m: int, noise: float = 0.01, seed: int = 0) -> np.ndarray:
    """Similarities: BASE everywhere, HIT on the true slide, small fixed noise."""
    rng = np.random.default_rng(seed)
    sim = BASE + rng.normal(0, noise, (len(truth), m))
    for w, k in enumerate(truth):
        if k is not None:
            sim[w, k] = HIT + rng.normal(0, noise)
    return sim


def slides_of(truth, **kw):
    m = kw.pop("m", None) or max(k for k in truth if k is not None) + 2
    return [a.slide for a in align(matrix(truth, m), **kw)]


@pytest.mark.parametrize(
    ("name", "truth"),
    [
        ("in order", [0, 0, 1, 1, 2, 2, 3, 3, 4, 4, 5, 5]),
        ("skips", [0, 0, 1, 4, 4, 5, 8, 8, 9, 9]),
        ("one revisit", [0, 0, 1, 1, 2, 2, 3, 3, 1, 1, 4, 4, 5, 5]),
        ("off-topic tangent", [0, 0, 1, 1, None, None, None, 2, 2, 3, 3]),
    ],
)
def test_alignment_cases(name, truth):
    assert slides_of(truth) == truth, name


def test_single_noisy_window_does_not_cause_a_back_jump():
    truth = [0, 0, 1, 1, 2, 2, 3, 3]
    sim = matrix(truth, 6)
    sim[5, 0] = BASE + 0.06  # window 5 mildly resembles slide 0 too
    assert [a.slide for a in align(sim)] == truth


def test_tangent_windows_score_zero_against_every_slide():
    # Generic chatter: all similarities equal -> unaligned, path continues afterwards.
    sim = matrix([0, 0, 1, 1, 2, 2], 4)
    sim[2:4] = BASE
    got = [a.slide for a in align(sim)]
    assert got[2:4] == [None, None]
    assert got[4:] == [2, 2]


def test_confidence_is_high_on_clear_windows_and_low_on_ambiguous_ones():
    sim = matrix([0, 0, 1, 1, 2, 2], 4)
    sim[3, 2] = sim[3, 1]  # window 3 equally like slides 1 and 2
    got = align(sim)
    assert got[0].confidence > 0.9
    assert got[3].confidence < got[0].confidence


def test_low_confidence_windows_are_left_unaligned():
    sim = matrix([0, 0, 1, 1, 2, 2], 4)
    out = align(sim, params=AlignParams(min_confidence=1.01))  # nothing is certain enough
    assert all(a.slide is None for a in out)


def test_pacing_penalises_fast_long_jumps_with_timestamps():
    # Slide 7 fits windows 2-3 a bit better than slide 1: enough to pay for a 6-slide
    # skip, but not also for skipping that far within 10 seconds.
    truth = [0, 0, 7, 7]
    sim = matrix(truth, 9)
    sim[2:, 7] = BASE + 0.17
    sim[2:, 1] = BASE + 0.10
    fast = [a.slide for a in align(sim, [0, 10_000, 20_000, 30_000])]
    untimed = [a.slide for a in align(sim)]
    assert untimed[2:] == [7, 7]
    assert fast[2:] == [1, 1]  # within 10 s, the long jump costs more than it earns


def test_move_costs_shape():
    p = AlignParams()
    c = move_costs(5, p)
    assert c[2, 2] == 0 and c[2, 3] == p.next_slide
    assert c[0, 2] == pytest.approx(p.skip_base + p.skip_step)  # skips one slide
    assert c[0, 3] == pytest.approx(p.skip_base + 2 * p.skip_step)
    assert c[3, 1] == pytest.approx(p.back_base + 2 * p.back_step)
    assert c[3, 1] > c[1, 3]  # going back costs more than skipping ahead


def test_edge_cases():
    assert align(np.zeros((0, 3))) == []
    assert all(a.slide is None for a in align(np.zeros((2, 0))))
    sim = cosine_matrix(np.array([[1.0, 0.0], [0.0, 1.0]]), np.array([[2.0, 0.0], [0.0, 3.0]]))
    assert np.allclose(sim, np.eye(2))


def test_evaluate_scores():
    from app.transcripts.evaluate import score

    labels = {0: 0, 1: 1, 2: None, 3: 3, 4: 4}
    predicted = {0: 0, 1: 2, 2: None, 3: None, 4: 4, 5: 5}
    r = score(labels, predicted)
    assert r.labelled == 5 and r.windows == 6
    assert r.exact == 3 / 5  # 0, 2 (both "no slide"), 4
    assert r.within_one == 4 / 5  # + window 1 (one slide off); window 3 unaligned is wrong
    assert r.aligned_precision == 2 / 3 and r.unaligned_correct == 1
    assert [c[0] for c in r.confusions] == [1, 3]


def test_score_accepts_either_page_of_a_pair():
    from app.transcripts.evaluate import score

    r = score({0: frozenset({5, 6}), 1: frozenset({None})}, {0: 6, 1: None})
    assert r.exact == 1.0
    r = score({0: frozenset({5, 6})}, {0: 7})
    assert r.exact == 0.0 and r.within_one == 1.0


TIMELINE = """# comment
0.00 0 (intro)
2.43 p3
5.34 p4
45.58 p36/37
51.45 0 (done)
"""


def test_parse_timeline():
    from app.transcripts.evaluate import parse_timeline

    e = parse_timeline(TIMELINE)
    assert [(x.start_ms, sorted(x.pages)) for x in e] == [
        (0, []), (163_000, [3]), (334_000, [4]), (2_758_000, [36, 37]), (3_105_000, []),
    ]  # fmt: skip
    with pytest.raises(ValueError):
        parse_timeline("5.61 p3")
    with pytest.raises(ValueError):
        parse_timeline("5.00 p3\n4.00 p4")


def test_majority_overlap_and_boundaries():
    from app.transcripts.evaluate import label_window, parse_timeline

    e = parse_timeline(TIMELINE)
    # 2:30-3:20: mostly p3 (37 s) vs intro (13 s), and the change is 13 s from the start.
    w = label_window(e, 150_000, 200_000)
    assert w.pages == {3} and w.boundary and round(w.share, 2) == 0.74
    # 3:30-4:20: all p3, no change nearby.
    w = label_window(e, 210_000, 260_000)
    assert w.pages == {3} and not w.boundary and w.share == 1.0
    # After "done": not a slide.
    assert label_window(e, 3_200_000, 3_250_000).pages == frozenset()
    # Spanning 3 entries is a boundary even without a nearby edge.
    e3 = parse_timeline("0.00 p1\n1.00 p2\n1.20 p3\n3.00 p4")
    assert label_window(e3, 30_000, 120_000).boundary


def test_admin_windows_lean_tangent():
    # Window 0 is intro chatter that happens to resemble slide 0 a little.
    truth = [None, 0, 0, 1, 1]
    sim = matrix([0, 0, 0, 1, 1], 3)
    sim[0, 0] = BASE + 0.10
    plain = [a.slide for a in align(sim)]
    flagged = [a.slide for a in align(sim, admin=[True, False, False, False, False])]
    assert plain[0] == 0  # embeddings alone pin it to the slide
    assert flagged == truth  # the admin flag frees it
    # A clearly on-topic window keeps its slide even when flagged.
    assert align(sim, admin=[False, True, False, False, False])[1].slide == 0
