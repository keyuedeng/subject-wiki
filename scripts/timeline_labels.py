"""Turn a hand-made slide timeline into the labels sheet for scripts.align_eval.

    uv run python -m scripts.timeline_labels docs/samples/week4-grasp-part1-timeline.txt \\
        "SWEN30006_2026_SM2 M-transcript (1)"

Each window gets the page on screen for most of its duration (majority overlap). Windows
near a slide change (within 15 s of the start or end) or spanning 3+ slides are marked
`boundary`, because hand-made times are rough. Pages are the deck's original page
numbers; "36|37" accepts either, and 0 means not a slide.
"""

import csv
import sys
from pathlib import Path

from app.transcripts.evaluate import labels_from_timeline, parse_timeline
from app.web.routes import _clock
from scripts.transcript_tools import load

OUT = Path("docs/samples")


def main(timeline_path: str, ref: str) -> None:
    entries = parse_timeline(Path(timeline_path).read_text())
    data = load(ref)
    labels = labels_from_timeline(entries, [(s.idx, s.start_ms, s.end_ms) for s in data.segments])
    sheet = OUT / f"{Path(data.transcript.filename).stem}-labels.csv"
    if sheet.exists():
        with sheet.open() as f:
            if any((row.get("page") or "").strip() for row in csv.DictReader(f)):
                raise SystemExit(f"{sheet} already has labels; not overwriting")
    with sheet.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["idx", "start", "end", "deck", "page", "boundary", "share", "text"])
        for seg in data.segments:
            lab = labels[seg.idx]
            page = "|".join(str(p) for p in sorted(lab.pages)) or "0"
            w.writerow([seg.idx, _clock(seg.start_ms), _clock(seg.end_ms), 1, page,
                        "boundary" if lab.boundary else "", f"{lab.share:.2f}",
                        seg.clean_text])  # fmt: skip
    boundary = sum(lab.boundary for lab in labels.values())
    print(f"Wrote {sheet}: {len(labels)} windows, {boundary} marked boundary, "
          f"{sum(not lab.pages for lab in labels.values())} labelled 'not a slide'")  # fmt: skip


if __name__ == "__main__":
    main(*sys.argv[1:3])
