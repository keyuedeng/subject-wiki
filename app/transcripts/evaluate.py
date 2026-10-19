"""Alignment accuracy against hand labels, and labels from a hand-made slide timeline.
Pure.

Labels are sets of acceptable answers per window: kept-slide indices (a slide's position
among the deck's pages that weren't collapsed), with None meaning "not about any slide".
A label like "p36/37" accepts either page. A label on a build step or repeat counts as
the page it was collapsed into. "±1" means the prediction is an acceptable slide or the
kept slide just before or after one.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass

BOUNDARY_SECONDS = 15  # a slide change this close to a window's start/end is "boundary"
BOUNDARY_SLIDES = 3  # a window spanning this many timeline entries is "boundary"

Label = frozenset  # of int | None


@dataclass
class Report:
    windows: int
    labelled: int
    exact: float  # share of labelled windows predicted acceptably (None == None counts)
    within_one: float
    aligned_precision: float  # of windows the model aligned, share on an acceptable slide
    aligned_share: float  # share of labelled windows the model aligned
    unaligned_correct: int  # windows labelled "no slide" that the model left unaligned
    confusions: list[tuple[int, Label, int | None]]  # (window idx, label, predicted)


def _as_label(value) -> Label:
    return value if isinstance(value, frozenset) else frozenset([value])


def is_exact(label: Label, pred: int | None) -> bool:
    return pred in label


def is_near(label: Label, pred: int | None) -> bool:
    return pred in label or (
        pred is not None and any(a is not None and abs(a - pred) <= 1 for a in label)
    )


def score(labels: dict, predicted: dict[int, int | None]) -> Report:
    labels = {i: _as_label(v) for i, v in labels.items()}
    idx = sorted(i for i in labels if i in predicted)
    n = len(idx)
    if n == 0:
        return Report(len(predicted), 0, 0.0, 0.0, 0.0, 0.0, 0, [])
    aligned = [i for i in idx if predicted[i] is not None]
    return Report(
        windows=len(predicted),
        labelled=n,
        exact=sum(is_exact(labels[i], predicted[i]) for i in idx) / n,
        within_one=sum(is_near(labels[i], predicted[i]) for i in idx) / n,
        aligned_precision=(
            sum(is_exact(labels[i], predicted[i]) for i in aligned) / len(aligned)
            if aligned
            else 0.0
        ),
        aligned_share=len(aligned) / n,
        unaligned_correct=sum(None in labels[i] and predicted[i] is None for i in idx),
        confusions=[
            (i, labels[i], predicted[i]) for i in idx if not is_exact(labels[i], predicted[i])
        ],
    )


# Timelines


@dataclass(frozen=True)
class TimelineEntry:
    start_ms: int
    pages: frozenset[int]  # empty = not a slide (intro, tangent, after the end)
    text: str


ENTRY_RE = re.compile(r"^\s*(\d{1,3})[.:](\d{2})\s+(.+?)\s*$")


def parse_timeline(text: str) -> list[TimelineEntry]:
    """Lines like `15.17 p7`, `45.58 p36/37` or `0.00 0 (intro)`; `#` lines are comments.
    Times are minutes.seconds."""
    entries: list[TimelineEntry] = []
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = ENTRY_RE.match(line)
        if not m:
            raise ValueError(f"Can't read timeline line: {line!r}")
        minutes, seconds, rest = int(m.group(1)), int(m.group(2)), m.group(3)
        if seconds >= 60:
            raise ValueError(f"Seconds must be under 60: {line!r}")
        token = rest.split()[0].lower()
        pages = frozenset() if token == "0" else frozenset(
            int(p) for p in token.lstrip("p").split("/") if p
        )  # fmt: skip
        entries.append(TimelineEntry((minutes * 60 + seconds) * 1000, pages, rest))
    if entries != sorted(entries, key=lambda e: e.start_ms):
        raise ValueError("Timeline times must be in order")
    return entries


@dataclass(frozen=True)
class WindowLabel:
    pages: frozenset[int]  # majority entry's pages; empty = not a slide
    share: float  # share of the window covered by that entry
    boundary: bool
    reason: str | None


def label_window(entries: list[TimelineEntry], start_ms: int, end_ms: int) -> WindowLabel:
    """The timeline entry on screen for most of the window (majority overlap)."""
    overlaps: list[tuple[int, int]] = []  # (overlap ms, entry index)
    for i, e in enumerate(entries):
        e_end = entries[i + 1].start_ms if i + 1 < len(entries) else float("inf")
        overlap = min(end_ms, e_end) - max(start_ms, e.start_ms)
        if overlap > 0:
            overlaps.append((overlap, i))
    if not overlaps:
        return WindowLabel(frozenset(), 0.0, True, "outside the timeline")
    best_overlap, best = max(overlaps, key=lambda x: (x[0], -x[1]))  # ties: earlier entry
    span = max(1, end_ms - start_ms)
    reasons = []
    if len(overlaps) >= BOUNDARY_SLIDES:
        reasons.append(f"spans {len(overlaps)} slides")
    near = [
        e.start_ms for e in entries[1:]
        if min(abs(e.start_ms - start_ms), abs(e.start_ms - end_ms)) <= BOUNDARY_SECONDS * 1000
    ]  # fmt: skip
    if near:
        reasons.append("slide change within 15 s of an edge")
    return WindowLabel(
        entries[best].pages, best_overlap / span, bool(reasons), "; ".join(reasons) or None
    )


def labels_from_timeline(
    entries: list[TimelineEntry], windows: Iterable[tuple[int, int, int]]
) -> dict[int, WindowLabel]:
    """`windows`: (idx, start_ms, end_ms)."""
    return {idx: label_window(entries, s, e) for idx, s, e in windows}
