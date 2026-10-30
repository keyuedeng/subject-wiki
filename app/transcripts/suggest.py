"""Which deck a transcript most likely belongs to, by text similarity. Pure, no model call.

TF-IDF over words: inverse document frequency is taken over every slide of the subject
(so words on most slides, like the subject's own name, count for little), each deck's
vector is the sum of its slides', and the transcript's comes from everything that was
said. The deck with the highest cosine similarity is suggested; the runner-up's score is
kept so the UI can say how clear the choice was.
"""

import math
import re
from collections import Counter
from collections.abc import Hashable
from dataclasses import dataclass

WORD_RE = re.compile(r"[a-z][a-z0-9]{2,}")
_STOP_WORDS = """
the and for are but not you all any can had her was one our out day get has him his how man
new now old see two way who boy did its let put say she too use that with have this will your
from they know want been good much some time very when come here just like long make many more
only over such take than them well were what into also then there their about would could
should which these those other because where while being does doing done going okay yeah right
really actually basically something things thing kind sort lot lots gonna wanna just mean
yes see said says let lets maybe might must need still even back first last next each every
"""
STOP = frozenset(_STOP_WORDS.split())


def words(text: str) -> list[str]:
    return [w for w in WORD_RE.findall(text.lower()) if w not in STOP]


@dataclass(frozen=True)
class Suggestion:
    deck: Hashable
    score: float
    runner_up: float | None


def suggest_deck(
    transcript_text: str, decks: dict[Hashable, list[str]], all_slides: list[str]
) -> Suggestion | None:
    """`decks`: candidate deck -> its slides' texts. `all_slides`: every slide's text in
    the subject (for IDF; the candidates' slides should be among them)."""
    said = Counter(words(transcript_text))
    if not said or not decks:
        return None
    df: Counter = Counter()
    for text in all_slides:
        df.update(set(words(text)))
    n = max(len(all_slides), 1)

    def idf(w: str) -> float:
        return math.log((n + 1) / (df.get(w, 0) + 1)) + 1

    def vector(counts: Counter) -> dict[str, float]:
        return {w: (1 + math.log(c)) * idf(w) for w, c in counts.items()}

    def cosine(a: dict[str, float], b: dict[str, float]) -> float:
        dot = sum(v * b[w] for w, v in a.items() if w in b)
        na = math.sqrt(sum(v * v for v in a.values()))
        nb = math.sqrt(sum(v * v for v in b.values()))
        return dot / (na * nb) if na and nb else 0.0

    t = vector(said)
    scores = []
    for deck, slides in decks.items():
        counts: Counter = Counter()
        for text in slides:
            counts.update(words(text))
        scores.append((cosine(t, vector(counts)), str(deck), deck))
    scores.sort(key=lambda x: (-x[0], x[1]))
    best = scores[0]
    if best[0] <= 0:
        return None
    return Suggestion(
        best[2], round(best[0], 4), round(scores[1][0], 4) if len(scores) > 1 else None
    )
