"""Cues -> sentences -> windows. Pure.

Caption cues break mid-sentence, so cues are first joined and re-split into sentences
(each with the start of its first piece and the end of its last). Sentences are then
grouped into windows sized to sit inside one slide's time on screen: long enough to
carry a topic (an embedding needs ~100 words), short enough that a slide change falls
between windows rather than inside one.

With timestamps a window closes at the first sentence end once it has >= MIN_SECONDS
and >= MIN_WORDS, or before it would pass MAX_SECONDS / MAX_WORDS, or at a pause of
>= PAUSE_SECONDS once past the minimum. Without timestamps, word counts only.
"""

import re
from collections import Counter
from dataclasses import dataclass, field

from app.transcripts.formats import Cue, Transcript

MIN_SECONDS, MAX_SECONDS = 30, 60
MIN_WORDS, MAX_WORDS = 100, 150
PAUSE_SECONDS = 3
MIN_TOO_LONG_WORDS = 30  # slow stretches still close at MAX_SECONDS once they have this
MAX_SENTENCE_WORDS = 60  # unpunctuated speech: split at cue boundaries past this
OTHER_SPEAKER = "[Other speaker]"
SENTENCE_END_RE = re.compile(r"(?<=[.?!])\s+")


@dataclass
class Sentence:
    text: str
    start_ms: int | None
    end_ms: int | None
    other_speaker: bool = False
    low_conf: list[str] = field(default_factory=list)

    @property
    def words(self) -> int:
        return len(self.text.split())


@dataclass
class Window:
    idx: int
    text: str
    start_ms: int | None
    end_ms: int | None
    low_conf: list[str]

    @property
    def words(self) -> int:
        return len(self.text.split())


def main_speaker(cues: list[Cue]) -> str | None:
    counts = Counter(c.speaker for c in cues if c.speaker)
    return counts.most_common(1)[0][0] if counts else None


def sentences(transcript: Transcript) -> list[Sentence]:
    cues = transcript.cues
    lecturer = main_speaker(cues)
    out: list[Sentence] = []
    current: Sentence | None = None

    def close() -> None:
        nonlocal current
        if current and current.text.strip():
            current.text = " ".join(current.text.split())
            out.append(current)
        current = None

    for cue in cues:
        other = bool(lecturer and cue.speaker and cue.speaker != lecturer)
        if current and current.other_speaker != other:
            close()  # a change of speaker always starts a new sentence
        pieces = SENTENCE_END_RE.split(cue.text)
        span = (cue.end_ms - cue.start_ms) if cue.start_ms is not None else None
        total = max(1, len(cue.text))
        offset = 0
        for i, piece in enumerate(pieces):
            # Interpolate piece times inside the cue by character position.
            p_start = p_end = None
            if span is not None:
                p_start = cue.start_ms + span * offset // total
                p_end = cue.start_ms + span * (offset + len(piece)) // total
            offset += len(piece) + 1
            if current is None:
                current = Sentence("", p_start, p_end, other_speaker=other)
            current.text += " " + piece
            current.end_ms = p_end if p_end is not None else current.end_ms
            current.low_conf += [
                w for w in cue.low_conf if w in piece and w not in current.low_conf
            ]
            last_piece = i == len(pieces) - 1
            if not last_piece or piece.rstrip().endswith((".", "?", "!")):
                close()
        if current and current.words >= MAX_SENTENCE_WORDS:
            close()
    close()
    return out


def make_windows(transcript: Transcript) -> list[Window]:
    timed = transcript.has_timestamps
    windows: list[Window] = []
    group: list[Sentence] = []

    def seconds(items: list[Sentence]) -> float:
        if not timed or items[0].start_ms is None or items[-1].end_ms is None:
            return 0.0
        return (items[-1].end_ms - items[0].start_ms) / 1000

    def flush() -> None:
        if not group:
            return
        text = " ".join((f"{OTHER_SPEAKER} " if s.other_speaker else "") + s.text for s in group)
        low = list(dict.fromkeys(w for s in group for w in s.low_conf))
        windows.append(Window(len(windows), text, group[0].start_ms, group[-1].end_ms, low))
        group.clear()

    for sentence in sentences(transcript):
        if group:
            words = sum(s.words for s in group)
            would_be = group + [sentence]
            too_long = words + sentence.words > MAX_WORDS or (
                timed and seconds(would_be) > MAX_SECONDS
            )
            past_min = words >= MIN_WORDS and (not timed or seconds(group) >= MIN_SECONDS)
            pause = (
                timed
                and sentence.start_ms is not None
                and group[-1].end_ms is not None
                and (sentence.start_ms - group[-1].end_ms) / 1000 >= PAUSE_SECONDS
            )
            if past_min or (too_long and words >= MIN_TOO_LONG_WORDS) or (pause and past_min):
                flush()
        group.append(sentence)
    flush()
    return windows
