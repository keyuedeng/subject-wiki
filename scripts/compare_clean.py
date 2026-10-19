"""Compare cleaning models side by side on the most technical windows of a transcript.

    uv run python -m scripts.compare_clean "SWEN30006_2026_SM2" --model gpt-6-luna --n 15

Picks the N windows with the most glossary terms in their raw text, cleans them with
`--model` (through the cache: re-runs are free), and writes an HTML page with raw,
the pipeline's cleaned text, and the other model's text side by side, plus each guard's
verdict and the cost of both.
"""

import argparse
import html
import re
from pathlib import Path

from sqlalchemy import select

from app.db.models import Slide
from app.db.session import _session_factory
from app.jobs.worker import get_queue
from app.transcripts.clean import (
    CLEAN_PROMPT,
    CLEAN_PROMPT_VERSION,
    MAX_OUTPUT_TOKENS,
    CleanedWindow,
    check,
    request_text,
)
from app.transcripts.glossary import build_glossary
from scripts.transcript_tools import load

OUT = Path("docs/samples")


def glossary_hits(text: str, glossary: list[str]) -> int:
    low = text.lower()
    return sum(len(re.findall(rf"\b{re.escape(t.lower())}\b", low)) for t in glossary)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("transcript")
    ap.add_argument("--model", default="gpt-6-luna")
    ap.add_argument("--n", type=int, default=15)
    args = ap.parse_args()

    data = load(args.transcript)
    with _session_factory()() as db:
        all_slides = db.scalars(select(Slide).where(Slide.file_id.in_([d.id for d in data.decks])))
        glossary = build_glossary(
            [{"title": s.title, "content_json": s.content_json, "notes": s.notes}
             for s in sorted(all_slides, key=lambda s: s.page_no)],
            data.subject_code,
        )  # fmt: skip
    segs = data.segments
    picked = sorted(segs, key=lambda s: -glossary_hits(s.raw_text, glossary))[: args.n]
    picked.sort(key=lambda s: s.idx)

    queue = get_queue()
    llm, cfg = queue.transcripts.llm, queue.transcripts.settings
    rows, cost = [], 0.0
    for seg in picked:
        prev = segs[seg.idx - 1].raw_text.split(". ")[-1] if seg.idx else None
        cached = llm.structured(
            kind="transcript_clean", prompt_version=CLEAN_PROMPT_VERSION, model=args.model,
            instructions=CLEAN_PROMPT,
            input_text=request_text(seg.raw_text, glossary, prev, seg.low_conf_words),
            schema=CleanedWindow, effort=cfg.clean_effort, max_output_tokens=MAX_OUTPUT_TOKENS,
        )  # fmt: skip
        if not cached.cached:
            cost += cached.call.cost_usd or 0.0
        other = check(seg.raw_text, CleanedWindow.model_validate(cached.response), glossary)
        rows.append((seg, other, cached.call.cost_usd))

    base_cost = sum(seg.cost_usd or 0 for seg, _, _ in rows)
    other_cost = sum(c or 0 for _, _, c in rows)
    name = Path(data.transcript.filename).stem
    out = OUT / f"{name}-clean-comparison.html"
    e = html.escape
    parts = [
        "<!doctype html><meta charset=utf-8><title>Cleaning comparison</title>",
        "<style>body{font:14px/1.5 system-ui;margin:24px;max-width:1500px}"
        "table{border-collapse:collapse}td,th{border:1px solid #ddd;padding:8px;"
        "vertical-align:top;width:33%}th{background:#f4f4f2;text-align:left}"
        ".meta{color:#777;font-size:12px}.bad{color:#b42318}</style>",
        f"<h1>{e(name)}: {e(cfg.clean_model)} vs {e(args.model)}</h1>",
        f"<p>{len(rows)} windows with the most glossary terms. Cleaning cost for these "
        f"windows: {e(cfg.clean_model)} ${base_cost:.4f}, {e(args.model)} ${other_cost:.4f}."
        "</p><table><tr><th>Raw</th>",
        f"<th>{e(cfg.clean_model)} (pipeline)</th><th>{e(args.model)}</th></tr>",
    ]
    for seg, other, _ in rows:

        def cell(text, status, note, flags):
            badge = (
                "" if status == "cleaned" else f"<div class=bad>{e(status)}: {e(note or '')}</div>"
            )
            fl = ", ".join(f["type"] for f in flags) or "no flags"
            return f"<td>{badge}{e(text)}<div class=meta>{e(fl)}</div></td>"

        parts.append(
            f"<tr><td><div class=meta>#{seg.idx} · {glossary_hits(seg.raw_text, glossary)} "
            f"terms · unsure: {e(', '.join(seg.low_conf_words) or 'none')}</div>"
            f"{e(seg.raw_text)}</td>"
            + cell(seg.clean_text, seg.clean_status, seg.clean_note, seg.flags)
            + cell(other.clean_text, other.status, other.note, other.flags)
            + "</tr>"
        )
    parts.append("</table>")
    out.write_text("\n".join(parts))
    print(f"Wrote {out} · {args.model} cost this run ${cost:.4f}")


if __name__ == "__main__":
    main()
