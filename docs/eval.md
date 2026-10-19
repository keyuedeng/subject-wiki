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
