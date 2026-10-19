"""Write a blank labelling sheet for one processed transcript.

    uv run python -m scripts.align_labels "SWEN30006_2026_SM2"   (filename part or file id)

Creates, in docs/samples/ (git-ignored):
- <transcript>-labels.csv: one row per window (idx, start, end, text). Fill `page` with
  the slide page shown while that was said; leave it blank or 0 if it wasn't about any
  slide (admin, a tangent). With several decks, also fill `deck` (1, 2, ...).
- <transcript>-slides.txt: every page with its title, to label against.
Predictions are deliberately not included, so they can't bias the labels.
"""

import csv
import sys
from pathlib import Path

from app.web.routes import _clock
from scripts.transcript_tools import load

OUT = Path("docs/samples")


def main(ref: str) -> None:
    data = load(ref)
    stem = Path(data.transcript.filename).stem
    sheet = OUT / f"{stem}-labels.csv"
    if sheet.exists():
        raise SystemExit(f"{sheet} already exists; not overwriting your labels")
    with sheet.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["idx", "start", "end", "deck", "page", "text"])
        for seg in data.segments:
            w.writerow([seg.idx, _clock(seg.start_ms), _clock(seg.end_ms),
                        1 if len(data.decks) == 1 else "", "", seg.clean_text])  # fmt: skip
    pages = OUT / f"{stem}-slides.txt"
    with pages.open("w") as f:
        for deck_no, deck in enumerate(data.decks, 1):
            f.write(f"Deck {deck_no}: {deck.filename}\n")
            for s in (s for s in data.slides if s.file_id == deck.id):
                note = ""
                if s.duplicate_of:
                    kept = next(k for k in data.slides if k.id == s.duplicate_of)
                    note = f"   (build step / repeat of page {kept.page_no})"
                f.write(f"  {s.page_no:3}  {s.title or '(untitled)'}{note}\n")
    print(f"Wrote {sheet} ({len(data.segments)} windows) and {pages}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "")
