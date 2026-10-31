"""Retrieval accuracy on the question set from scripts.qa_generate.

    uv run python -m scripts.qa_eval [docs/samples/qa-eval.csv] [--append] [--sweep]

For each question, searches its subject with keyword (BM25) only, embeddings only, and
hybrid (both, by reciprocal rank fusion), and reports recall@1/5/10 and MRR: strict (the
source the question was written from, or a chunk listed in `also_ok`) and relaxed (also
another chunk about the same slide: its lecturer notes or an aligned lecture moment).
Query embeddings are cached, so re-runs are free. --append adds the tables to
docs/eval.md. --sweep compares hybrid fusion settings (RRF k, results taken from each
list).
"""

import argparse
import csv
from datetime import date
from pathlib import Path

from sqlalchemy import select

from app.db.models import Chunk
from app.db.session import _session_factory
from app.search.evaluate import KS, Case, Retrieved, Scores, first_hit
from app.services.ask import get_ask_service
from scripts.reindex import find_subject

MODES = ("keyword", "embedding", "hybrid")
EVAL_DOC = Path("docs/eval.md")


def table(title: str, scores: dict[str, Scores]) -> str:
    n = next(iter(scores.values())).n
    lines = [f"**{title}** ({n} questions)", "",
             "| | " + " | ".join(f"Recall@{k}" for k in KS) + " | MRR |",
             "|---" * (len(KS) + 2) + "|"]  # fmt: skip
    best = {k: max(s.recall(k) for s in scores.values()) for k in KS}
    for mode, s in scores.items():
        cells = []
        for k in KS:
            v = f"{s.recall(k):.1%}"
            cells.append(f"**{v}**" if s.recall(k) == best[k] else v)
        lines.append(f"| {mode} | " + " | ".join(cells) + f" | {s.mrr:.3f} |")
    return "\n".join(lines)


def sweep(rows, service) -> None:
    from app.search.search import Searcher

    grid = [(k, depth) for k in (10, 30, 60, 100) for depth in (20, 50)]
    print("| RRF k | depth | Recall@1 | Recall@5 | Recall@10 | MRR |\n|---|---|---|---|---|---|")
    with _session_factory()() as db:
        subjects = {}
        for k, depth in grid:
            searcher = Searcher(service.backend, service._embed_query, depth=depth, rrf_k=k)
            s = Scores()
            for row in rows:
                code = row["subject"]
                subject = subjects.get(code) or subjects.setdefault(code, find_subject(db, code))
                case = Case(row["chunk_id"], frozenset(row.get("also_ok", "").split()))
                hits = searcher.search(db, subject.id, row["question"], k=max(KS)).hits
                s.add(first_hit(case, [Retrieved(str(h.chunk_id)) for h in hits]))
            print(f"| {k} | {depth} | " + " | ".join(f"{s.recall(n):.1%}" for n in KS)
                  + f" | {s.mrr:.3f} |")  # fmt: skip


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("csv", nargs="?", default="docs/samples/qa-eval.csv")
    parser.add_argument("--append", action="store_true")
    parser.add_argument("--sweep", action="store_true")
    args = parser.parse_args()
    with open(args.csv, newline="") as f:
        rows = list(csv.DictReader(f))
    service = get_ask_service()
    if args.sweep:
        return sweep(rows, service)

    strict = {m: Scores() for m in MODES}
    relaxed = {m: Scores() for m in MODES}
    by_type: dict[str, dict[str, Scores]] = {}
    misses: list[str] = []
    with _session_factory()() as db:
        subjects = {}
        for row in rows:
            code = row["subject"]
            subject = subjects.get(code) or subjects.setdefault(code, find_subject(db, code))
            case = Case(row["chunk_id"], frozenset(row.get("also_ok", "").split()),
                        row.get("slide_id") or None, row["source_type"])  # fmt: skip
            for mode in MODES:
                found = service.search(db, subject, row["question"], k=max(KS), mode=mode)
                ids = [s.ref.chunk_id for s in found]
                slides = {c.id: (c.meta or {}).get("slide_id") for c in db.scalars(
                    select(Chunk).where(Chunk.id.in_(ids)))}  # fmt: skip
                results = [Retrieved(str(i), slides.get(i)) for i in ids]
                rank = first_hit(case, results)
                strict[mode].add(rank)
                relaxed[mode].add(first_hit(case, results, relaxed=True))
                by_type.setdefault(case.group, {m: Scores() for m in MODES})[mode].add(rank)
                if mode == "hybrid" and (rank is None or rank > 5):
                    misses.append(f"- {row['label']}: \"{row['question']}\" "
                                  f"(rank {rank or '>10'})")  # fmt: skip

    parts = [table("Strict: the source itself", strict),
             table("Relaxed: or another chunk about the same slide", relaxed)]  # fmt: skip
    for group, scores in sorted(by_type.items()):
        parts.append(table(f"Strict, {group} questions", scores))
    report = "\n\n".join(parts)
    print(report)
    print("\nHybrid misses (not in the top 5):\n" + ("\n".join(misses) or "none"))

    if args.append:
        subjects_text = ", ".join(sorted({r["subject"] for r in rows}))
        section = (
            f"\n\n## Retrieval: keyword vs embedding vs hybrid (phase 4)\n\n"
            f"**Measured on:** {len(rows)} generated questions ({subjects_text}), "
            f"{date.today().isoformat()}. Each question was written by the model from one "
            f"randomly chosen kept slide or tutorial section (`scripts.qa_generate`, "
            f"`docs/samples/qa-eval.csv`); the answer key is that source. Search: BM25 over "
            f"Postgres full-text term counts, pgvector cosine (`text-embedding-3-large`, "
            f"1536 dims), and the two fused by RRF (k=60, 50 from each).\n\n{report}\n\n"
            f"Reproduce with `uv run python -m scripts.qa_eval` (cached embeddings, no API "
            f"cost).\n"
        )
        with EVAL_DOC.open("a") as f:
            f.write(section)
        print(f"\nAppended to {EVAL_DOC}")


if __name__ == "__main__":
    main()
