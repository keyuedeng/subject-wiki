"""Alignment accuracy against a labels sheet (from scripts.align_labels or
scripts.timeline_labels).

    uv run python -m scripts.align_eval docs/samples/<transcript>-labels.csv [--sweep]

Re-runs the alignment from cached embeddings (no API calls), so `--sweep` can try many
penalty settings for free. `page` may be "36|37" (either is correct) or 0 (not a slide);
empty rows are skipped. Windows marked `boundary` are reported separately, because
hand-made slide times are rough.
"""

import argparse
import csv
import itertools
from dataclasses import asdict, replace
from pathlib import Path

from app.transcripts.align import AlignParams, align
from app.transcripts.evaluate import Report, score
from scripts.transcript_tools import load, similarity

GRID = {
    "tangent": [0.02, 0.04, 0.06, 0.08],
    "back_base": [0.03, 0.06, 0.10],
    "skip_step": [0.005, 0.015, 0.03],
    "min_confidence": [0.2, 0.35, 0.5],
}


def read_labels(path: Path, data) -> tuple[dict, set[int], dict[int, str]]:
    """(labels by window, boundary windows, text by window)."""
    labels, boundary, text = {}, set(), {}
    with path.open() as f:
        for row in csv.DictReader(f):
            page = (row.get("page") or "").strip()
            if page == "":
                continue
            idx = int(row["idx"])
            deck = int((row.get("deck") or "1").strip() or 1)
            accepted = set()
            for p in page.replace("/", "|").split("|"):
                p = p.strip().lstrip("p")
                accepted.add(None if p in ("0", "-") else data.kept_index_of_page(deck, int(p)))
            labels[idx] = frozenset(accepted)
            if (row.get("boundary") or "").strip():
                boundary.add(idx)
            text[idx] = row.get("text") or ""
    return labels, boundary, text


def predictions(sim, data, params: AlignParams) -> dict[int, int | None]:
    admin = [any(f["type"] == "admin" for f in s.flags) for s in data.segments]
    out = align(sim, [s.start_ms for s in data.segments], params, admin)
    return {seg.idx: a.slide for seg, a in zip(data.segments, out, strict=True)}


def both(labels, boundary, predicted) -> tuple[Report, Report]:
    clean = {i: v for i, v in labels.items() if i not in boundary}
    return score(labels, predicted), score(clean, predicted)


def pages(data, label) -> str:
    return "/".join(sorted((data.label_of_kept(k) for k in label), key=lambda s: (s == "-", s)))


def print_report(all_r: Report, clean_r: Report, data, boundary, text) -> None:
    print(f"{'':28}{'all windows':>14}{'non-boundary':>16}")
    print(f"{'Windows labelled':28}{all_r.labelled:>14}{clean_r.labelled:>16}")
    print(f"{'Accuracy (exact slide)':28}{all_r.exact:>14.1%}{clean_r.exact:>16.1%}")
    print(f"{'Accuracy (within ±1)':28}{all_r.within_one:>14.1%}{clean_r.within_one:>16.1%}")
    print(f"{'Aligned windows correct':28}{all_r.aligned_precision:>14.1%}"
          f"{clean_r.aligned_precision:>16.1%}")  # fmt: skip
    print(
        f"{'Share the model aligned':28}{all_r.aligned_share:>14.1%}{clean_r.aligned_share:>16.1%}"
    )
    if all_r.confusions:
        print(f"\nMisses ({len(all_r.confusions)}): window  label -> predicted  [B = boundary]")
        for i, label, pred in all_r.confusions:
            mark = "B" if i in boundary else " "
            print(f"  {i:3} {mark} {pages(data, label):>7} -> {data.label_of_kept(pred):<4} "
                  f"{text[i][:110]}")  # fmt: skip


def sweep(sim, data, labels, boundary, base: AlignParams) -> None:
    results = []
    for values in itertools.product(*GRID.values()):
        params = replace(base, **dict(zip(GRID, values, strict=True)))
        a, c = both(labels, boundary, predictions(sim, data, params))
        results.append((c.exact, a.exact, c.within_one, a.within_one, params))
    results.sort(key=lambda x: (-x[0], -x[1], -x[2], -x[3]))
    base_a, base_c = both(labels, boundary, predictions(sim, data, base))
    defaults = {k: asdict(base)[k] for k in GRID}

    def row(c_exact, a_exact, c_near, a_near, params) -> str:
        changed = {k: getattr(params, k) for k in GRID}
        diff = {k: v for k, v in changed.items() if v != defaults[k]} or "(defaults)"
        scores = f"{c_exact:6.1%} {a_exact:6.1%}   {c_near:6.1%} {a_near:6.1%}"
        return f"  {scores}   {diff}"

    print(f"\nSweep: {len(results)} settings, ranked by non-boundary exact accuracy")
    print("          exact            ±1")
    print("  non-bd    all   non-bd    all   settings")
    print(
        row(base_c.exact, base_a.exact, base_c.within_one, base_a.within_one, base) + "  <- current"
    )
    for r in results[:8]:
        print(row(*r))
    print(f"\nDefaults: {defaults}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("labels", type=Path)
    ap.add_argument("--transcript", help="file id or filename part (default: from the sheet)")
    ap.add_argument("--sweep", action="store_true")
    args = ap.parse_args()
    ref = args.transcript or args.labels.name.removesuffix("-labels.csv")
    data = load(ref)
    labels, boundary, text = read_labels(args.labels, data)
    if not labels:
        raise SystemExit("No labelled rows yet: fill the `page` column")
    sim = similarity(data)
    params = AlignParams()
    print_report(*both(labels, boundary, predictions(sim, data, params)), data, boundary, text)
    if args.sweep:
        sweep(sim, data, labels, boundary, params)


if __name__ == "__main__":
    main()
