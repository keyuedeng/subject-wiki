# Evaluation notes

Results to come back to later. Re-run any of these with the scripts named below: they
work from cached embeddings and cost nothing.

## Transcript-to-slide alignment (phase 3)

**Measured on:** SWEN30006 Week 4, GRASP Part I (2026 S2)
- Deck: `Lecture 4 - GRASP Part I.pptx`, 44 pages (1 repeat collapsed, so 43 alignment
  targets)
- Transcript: `SWEN30006_2026_SM2 M-transcript (1).vtt`, 55 minutes, Echo360 VTT, 63
  windows
- Ground truth: a hand-made slide timeline (`docs/samples/week4-grasp-part1-timeline.txt`,
  times ±10–15 s), turned into per-window labels by `scripts.timeline_labels`

**Labelling rules:**
- Each window gets the page on screen for most of its duration (majority overlap).
- Intro, "not a slide" and after-the-end stretches are page 0 (the model should leave
  them unaligned).
- "p36/37" accepts either page. A repeated page counts as the page it repeats (p41 →
  p27).
- A window is **boundary** if a slide change falls within 15 s of its start or end, or
  it spans 3+ slides. That's 29 of 63 windows; their labels are least reliable given
  the rough times.

**Settings:**
- Windows: 30–60 s / 100–150 words.
- `text-embedding-3-large` at 1536 dimensions. Each slide is embedded from its title,
  content, visual facts and speaker notes; each window from its cleaned text.
- Alignment: Viterbi with `AlignParams` defaults (tangent 0.04, admin_tangent 0.15,
  back_base 0.06, skip_step 0.015, min_confidence 0.35).

### Results

| | All windows (63) | Non-boundary (34) |
|---|---|---|
| Exact slide | **74.6%** | **79.4%** |
| Within ±1 slide | **88.9%** | **91.2%** |
| Windows the model aligned | 90.5% | 85.3% |
| Aligned windows that are correct | 75.4% | 79.3% |

Reproduce with
`uv run python -m scripts.align_eval "docs/samples/SWEN30006_2026_SM2 M-transcript (1)-labels.csv"`
(add `--sweep` for the parameter grid).

### The admin signal

Embeddings can't tell intro and logistics talk from content: the lecturer's
self-introduction scored as far above the median slide (+0.28) as a typical on-topic
window does, so no tangent threshold separates them. The cleaning pass already flags
such windows as **admin**, so admin-flagged windows score `admin_tangent = 0.15` instead
of 0.04, which makes "not about a slide" much easier to choose for them.

| | Exact (all / non-boundary) | ±1 (all / non-boundary) |
|---|---|---|
| Before (embeddings only) | 69.8% / 70.6% | 84.1% / 82.4% |
| **With admin signal** | **74.6% / 79.4%** | **88.9% / 91.2%** |

- Changed windows: 4 gains (the three intro windows and the after-class chatter are now
  unaligned, as labelled) and 1 loss. The loss is a student Q&A on page 17 whose only
  admin content was a one-line aside ("we'll revisit this in week 9").
- On-topic windows with a short admin aside (3, 19, 58) kept their slides.
- Possible refinement, not built: weight by how much of the window the admin quote
  covers, which might avoid the one loss. Needs a second labelled lecture before it's
  worth tuning.

### Where the remaining misses are

16 misses with the admin signal:
- **Most are one slide early.** The model moves to the next slide before the labels do
  (e.g. 7→8, 14→15, 15→16, 19→20, 29→30, 36/37→38). That's partly the ±15 s in the
  hand-made times (most are boundary windows), and partly lecturers starting to talk
  about the next slide before clicking. Nearly all are inside ±1, which is why ±1
  accuracy is ~89–91%.
- **The repeated slide:** page 41 repeats page 27, so phase 2 collapsed it. When the
  lecturer reaches page 41, the model picks page 42 rather than jumping "back" to 27.
  Treating repeat pages as their own positions in the deck was tried and made overall
  accuracy worse (68.3% exact), so it was dropped.
- **Labelled "not a slide" but aligned:** window 24 (a stretch away from the slides,
  with no admin flag).

### What didn't help

- **Penalty tuning:** a 108-setting sweep over tangent, back-jump cost, skip cost and
  confidence threshold found nothing better than the defaults on non-boundary windows.
  The best setting (back_base 0.10) only added ~1.6 points, all on boundary windows.
  That's not enough to change defaults on one lecture.
- **Repeat pages as their own states:** worse (see above).

### Caveats

- One lecture and 63 windows: treat differences of 1–3 windows as noise.
- Labels come from rough times; the non-boundary column is the fairer measure.
- Status: accuracy is good enough for now, and further tuning and labelling are on hold.

## Cleaning model comparison (phase 3)

On the 15 GRASP windows with the most glossary terms, `gpt-6.1-sol` and `gpt-6-luna`
removed fillers equally well, and neither had a clean rejected. Only sol mapped spoken
class names onto the slides' identifiers ("sale line item" → `SaleLineItem`, "Monopoly
game" → `MonopolyGame`). Luna was ~16× cheaper ($0.0037 against ~$0.06 for those
windows). Kept **sol**: about $0.27 per 55-minute lecture. The side-by-side page is
`docs/samples/SWEN30006_2026_SM2 M-transcript (1)-clean-comparison.html`
(`scripts.compare_clean`).


## Retrieval: keyword vs embedding vs hybrid (phase 4)

**Measured on:** 40 generated questions (INFO30006, SWEN30006), 2026-10-31. Each question was written by the model from one randomly chosen kept slide or tutorial section (`scripts.qa_generate`, `docs/samples/qa-eval.csv`); the answer key is that source. Search: BM25 over Postgres full-text term counts, pgvector cosine (`text-embedding-3-large`, 1536 dims), and the two fused by RRF (k=60, 50 from each).

**Strict: the source itself** (40 questions)

| | Recall@1 | Recall@5 | Recall@10 | MRR |
|---|---|---|---|---|
| keyword | **67.5%** | 85.0% | 90.0% | 0.759 |
| embedding | 55.0% | **92.5%** | **95.0%** | 0.706 |
| hybrid | 65.0% | **92.5%** | **95.0%** | 0.753 |

**Relaxed: or another chunk about the same slide** (40 questions)

| | Recall@1 | Recall@5 | Recall@10 | MRR |
|---|---|---|---|---|
| keyword | **75.0%** | 90.0% | 92.5% | 0.809 |
| embedding | 67.5% | **95.0%** | **97.5%** | 0.783 |
| hybrid | 72.5% | 92.5% | 95.0% | 0.811 |

**Strict, slide questions** (28 questions)

| | Recall@1 | Recall@5 | Recall@10 | MRR |
|---|---|---|---|---|
| keyword | **60.7%** | 78.6% | 85.7% | 0.701 |
| embedding | 53.6% | **92.9%** | **92.9%** | 0.699 |
| hybrid | 57.1% | 89.3% | **92.9%** | 0.694 |

**Strict, tutorial questions** (12 questions)

| | Recall@1 | Recall@5 | Recall@10 | MRR |
|---|---|---|---|---|
| keyword | **83.3%** | **100.0%** | **100.0%** | 0.896 |
| embedding | 58.3% | 91.7% | **100.0%** | 0.722 |
| hybrid | **83.3%** | **100.0%** | **100.0%** | 0.889 |

Reproduce with `uv run python -m scripts.qa_eval` (cached embeddings, no API cost).

**Reading the results:**
- Hybrid is never the worst method and is best or tied at recall@5 and @10 (strict
  92.5% / 95.0%), but it does **not** beat both single methods outright: keyword search
  is one question ahead at recall@1 (67.5% vs 65.0%) and level on MRR (0.759 vs 0.753).
  With 40 questions, one question is 2.5 points, so the methods are within noise at the
  top rank; keyword's weakness shows lower down (recall@5 85.0%).
- Keyword search does best on tutorial questions (12 of 40: 100% recall@5) because
  generated questions about a tutorial reuse its specific terms (Hashcat, STRIDE).
  Embeddings do best on slide questions (92.9% recall@5), where questions paraphrase.
  Hybrid keeps most of each: 100% on tutorials, 89.3% on slides.
- Fusion settings don't matter here (`--sweep`): RRF k from 10 to 100 and 20 or 50
  results per list all give 65.0% / 92.5% / 95.0% hybrid recall, MRR 0.751–0.759. The
  defaults (k=60, 50 per list) are kept.
- Hybrid misses outside the top 5: three INFO30006 slides with broad questions ("What
  factors make computer systems difficult to keep secure?"), where many slides and
  transcript moments say similar things.
- Caveats: the questions are model-written from one source each, so they lean towards
  that source's wording, which favours keyword search; the answer key accepts only that
  source (relaxed adds the same slide's notes and lecture moments). A hand-written set
  would test paraphrase harder.
