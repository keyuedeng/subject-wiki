"""A fake model provider and a matching deck + lecture for transcript tests."""

import hashlib
import re
import threading

import numpy as np

from app.llm.client import StructuredResponse, Usage
from app.transcripts.clean import CleanedWindow, Flag

DIMS = 4096  # wide enough that different words practically never collide
WORD_RE = re.compile(r"[a-z]+")


def bag_of_words(text: str) -> list[float]:
    """Deterministic 'embedding': hashed word counts, so shared words mean similarity."""
    vec = np.zeros(DIMS)
    for word in WORD_RE.findall(text.lower()):
        if len(word) > 3:
            vec[int(hashlib.md5(word.encode()).hexdigest(), 16) % DIMS] += 1
    return vec.tolist()


class FakeProvider:
    name = "fake"

    def __init__(self):
        self.structured_calls: list[str] = []
        self.embedded: list[str] = []
        self._lock = threading.Lock()

    def structured(self, *, model, instructions, content, schema, effort, max_output_tokens,
                   cache_hint=None):  # fmt: skip
        text = content[-1]["text"]
        with self._lock:
            self.structured_calls.append(text)
        window = text.split("Window:\n", 1)[1]
        clean = re.sub(r"\b(?:um|uh),?\s*", "", window, flags=re.IGNORECASE)
        clean = clean.replace("you mel", "UML")
        flags = []
        if "on the exam" in window:
            sentence = next(s for s in re.split(r"(?<=[.?!])\s+", window) if "on the exam" in s)
            flags.append(Flag(type="exam_hint", reason="says it is examined", quote=sentence))
        if "[Other speaker]" in window:
            flags.append(Flag(type="student_question", reason="a student speaks",
                              quote=window.split("[Other speaker]", 1)[1][:40]))  # fmt: skip
        return StructuredResponse(
            CleanedWindow(clean_text=clean, flags=flags), Usage(1200, 0, 300, 0)
        )

    def embed(self, *, model, texts, dimensions):
        with self._lock:
            self.embedded.extend(texts)
        return [bag_of_words(t) for t in texts], 10 * len(texts)


# Five topics, each with its own vocabulary. The deck has one slide per topic plus a
# two-step build of topic 3 (collapsed in phase 2).
TOPICS = [
    ("Creator Pattern", "creator assigns creation responsibility containment aggregation"),
    ("Information Expert", "expert information knowledge responsibility fulfil data"),
    ("Low Coupling", "coupling dependency connected classes change impact"),
    ("High Cohesion", "cohesion focused related responsibilities manageable"),
    ("Monopoly Example", "monopoly board square player piece dice game"),
]


def deck_pages() -> list[dict]:
    pages = []
    for i, (title, words) in enumerate(TOPICS):
        lines = [f"• {w}" for w in words.split()]
        if i == 3:  # build: half the bullets, then all of them
            pages.append({"title": title, "lines": lines[:2]})
        pages.append({"title": title, "lines": lines})
    return pages


def lecture_vtt(order: list[int | None], seconds_per_window: int = 45) -> bytes:
    """One ~110-word window per entry of `order` (topic index, or None = chatter)."""
    lines, t = ["WEBVTT", ""], 0
    for n, topic in enumerate(order):
        if topic is None:
            body = "the weather today is nice and the parking was full again sorry"
        else:
            body = TOPICS[topic][1]
        sentence_words = (body + " ") * 5
        # Five sentences of ~22 words: one window per topic
        for s in range(5):
            words = sentence_words.split()[:21]  # + "Um" = 22 words; 5 sentences = 110
            text = "Um " + " ".join(words) + (" this will be on the exam" if (n, s) == (2, 0)
                                               else "") + "."  # fmt: skip
            start, end = t + s * seconds_per_window / 5, t + (s + 1) * seconds_per_window / 5

            def stamp(x):
                return f"00:{int(x // 60):02d}:{x % 60:06.3f}"

            lines += [f"{stamp(start)} --> {stamp(end)}", f"<v Speaker 0>{text}", ""]
            if (n, s) == (4, 2):  # a student chips in, mid-window
                lines += [f"{stamp(end)} --> {stamp(end + 1)}",
                          "<v Speaker 1>is this examinable for the assignment?", ""]  # fmt: skip
        t += seconds_per_window
    return "\n".join(lines).encode()
