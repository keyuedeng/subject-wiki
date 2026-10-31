"""Write a retrieval eval set: one student-style question per randomly chosen source.

    uv run python -m scripts.qa_generate SWEN30006 INFO30006 [--n 40] [--seed 7]

Samples kept slides (with real content) and tutorial sections, spread across subjects and
weeks, about 70/30, and asks the model for a question each source answers, worded the
way a student would ask it rather than copied from the source. Sources with nothing to
ask about (title slides, agendas) are skipped. Writes docs/samples/qa-eval.csv: skim it,
delete bad rows, and put other acceptable chunk ids in `also_ok` (separated by spaces).
Each question is one cached call (about $0.003), so re-running with the same seed is free.
"""

import argparse
import csv
import random
from pathlib import Path

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from app.config import get_settings
from app.db.models import Chunk, File
from app.db.session import _session_factory
from app.jobs.worker import get_queue
from app.services.ask import load_refs
from scripts.reindex import find_subject

OUT = Path("docs/samples/qa-eval.csv")
TUTORIAL_SHARE = 0.3
MIN_WORDS = 30
PROMPT_VERSION = "qa-eval-gen-v1"
PROMPT = """\
You write test questions for a study tool's search. You get one source from a \
university subject: a lecture slide or a tutorial sheet section. Write one question a \
student in the subject might type that this source answers.
- Ask about the idea, the way a student would, in your own words. Don't copy the source's \
title or distinctive phrases, and don't mention "the slide", "the tutorial" or numbers \
like "question 3".
- For an exercise, ask about the concept it practises or how to approach it, not for the \
answer to the exact task.
- answerable: false if the source has nothing a student would ask about (a title slide, an \
agenda, contact details, a reading list).
"""


class Generated(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str
    answerable: bool


def candidates(db, subject) -> tuple[list[Chunk], list[Chunk]]:
    rows = db.scalars(
        select(Chunk).join(File, File.id == Chunk.file_id).where(
            Chunk.subject_id == subject.id, File.included,
            Chunk.source_type.in_(["slide", "tutorial"]),
        )
    ).all()  # fmt: skip
    slides = [c for c in rows if c.source_type == "slide" and len(c.body.split()) >= MIN_WORDS]
    tutes = [c for c in rows if c.source_type == "tutorial" and len(c.body.split()) >= MIN_WORDS]
    return slides, tutes


def spread(chunks: list[Chunk], weeks: dict, rng: random.Random) -> list[Chunk]:
    """Shuffle, then interleave weeks so a sample covers them evenly."""
    by_week: dict = {}
    for c in rng.sample(chunks, len(chunks)):
        by_week.setdefault(weeks.get(c.file_id), []).append(c)
    out = []
    while any(by_week.values()):
        for w in sorted(by_week, key=lambda w: (w is None, w or 0)):
            if by_week[w]:
                out.append(by_week[w].pop())
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("subjects", nargs="+")
    parser.add_argument("--n", type=int, default=40)
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()
    rng = random.Random(args.seed)
    settings = get_settings()
    llm = get_queue().tutorials.llm

    rows, cost = [], 0.0
    with _session_factory()() as db:
        subjects = [find_subject(db, s) for s in args.subjects]
        pools = []
        for subject in subjects:
            weeks = dict(db.execute(select(File.id, File.week).where(
                File.subject_id == subject.id)).all())  # fmt: skip
            slides, tutes = candidates(db, subject)
            pools.append((subject, spread(slides, weeks, rng), spread(tutes, weeks, rng)))
        total_sources = sum(len(s) + len(t) for _, s, t in pools)
        want = {}
        for subject, slides, tutes in pools:
            share = (len(slides) + len(tutes)) / max(total_sources, 1)
            n = round(args.n * share)
            n_tute = min(len(tutes), round(n * TUTORIAL_SHARE))
            want[subject.id] = (n - n_tute, n_tute)

        for subject, slides, tutes in pools:
            n_slide, n_tute = want[subject.id]
            for pool, n in ((slides, n_slide), (tutes, n_tute)):
                kept = 0
                for chunk in pool:
                    if kept >= n:
                        break
                    ref = load_refs(db, subject, [chunk.id])[chunk.id]
                    body = chunk.body.split("\n\nSolution (")[0]  # ask about the task
                    call = llm.structured(
                        kind="qa_eval_question", prompt_version=PROMPT_VERSION,
                        model=settings.qa_model, instructions=PROMPT,
                        input_text=f"{ref.describe()}\n{body[:3000]}", schema=Generated,
                        effort="low", max_output_tokens=2000,
                    )  # fmt: skip
                    cost += 0.0 if call.cached else (call.call.cost_usd or 0.0)
                    if not call.response["answerable"]:
                        continue
                    kept += 1
                    rows.append({
                        "subject": subject.code or subject.name,
                        "question": call.response["question"].strip(),
                        "source_type": chunk.source_type,
                        "label": ref.label,
                        "title": chunk.title,
                        "chunk_id": str(chunk.id),
                        "slide_id": (chunk.meta or {}).get("slide_id") or "",
                        "also_ok": "",
                    })  # fmt: skip
                    print(f"{ref.label:32.32} | {rows[-1]['question']}")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    print(f"\n{len(rows)} questions -> {OUT} · this run ${cost:.2f}")


if __name__ == "__main__":
    main()
