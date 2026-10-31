from app.search.evaluate import Case, Retrieved, Scores, first_hit


def test_first_hit_strict_and_relaxed():
    case = Case("c1", frozenset({"c9"}), slide_id="s1")
    results = [Retrieved("x", "s1"), Retrieved("y"), Retrieved("c9"), Retrieved("c1", "s1")]
    assert first_hit(case, results) == 3  # also_ok counts
    assert first_hit(case, results, relaxed=True) == 1  # notes/moment on the same slide
    assert first_hit(Case("z"), results) is None


def test_scores():
    s = Scores()
    for rank in (1, 3, 7, None):
        s.add(rank)
    assert (s.recall(1), s.recall(5), s.recall(10)) == (0.25, 0.5, 0.75)
    assert round(s.mrr, 4) == round((1 + 1 / 3 + 1 / 7) / 4, 4)
