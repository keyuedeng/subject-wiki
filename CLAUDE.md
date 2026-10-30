# CLAUDE.md

Subject Wiki: turns a subject's resources into a cited knowledge base. The product spec
is `docs/spec.md`, which lists the phases under "Build plan".

## Status

- **Phase 1 is done:** upload site and storage.
- **Phase 2 is functionally complete: slide parsing.** Checked end to end on three real
  SWEN30006 decks: L6 and L7 (PDF), L4 GRASP (PPTX through LibreOffice). UML class and
  state machine diagrams were checked against the slides and described accurately, build
  slides and repeated pages collapse, and re-parsing makes no model calls.
  - Slides files become one `slides` row per page. Each page takes the text path
    (PyMuPDF text layer, no model call) or the vision path (OpenAI `gpt-6.1-sol`, page
    image plus text layer, structured output).
  - Model results are cached by a hash of the full request (`app/llm/cache.py`), so
    only requests that actually changed are paid for again. The PDF that LibreOffice
    makes from a PPTX is stored too, so a PPTX converts only once.
  - Speaker notes from a PPTX are stored in `slides.notes` and shown in the viewer.
    They are not sent to the model, so parsed slide content holds only what's on the
    slide.
  - Current prompt: `slides-v2` (stopped sending notes). GRASP is parsed with it. L6
    and L7 are still on `slides-v1` (their content isn't affected; there are no notes)
    and show a "parsed with an older prompt" flag in the viewer. Their next re-parse
    pays again (about $0.46 for both), because the system prompt is part of every
    request.
- **Known gaps:** the model sometimes copies the running header into its markdown.
  Section-divider collages go to vision (about $0.012 each); this was left as is on
  purpose.
- **Queued for the next slide prompt change** (don't edit `SYSTEM_PROMPT` or re-parse
  just for these; batch them, since any edit re-pays every vision page):
  - Comics, memes and third-party images: describe what they show and their point
    instead of transcribing them, so OpenAI's content filter doesn't stop the output
    (INFO30006 "Wk 2 - Authentication" p68, an xkcd comic, currently falls back to its
    text layer). Bump `PROMPT_VERSION` with it.
- **Phase 3 is done: the transcript pipeline.** Checked on the real SWEN30006 GRASP
  Part I lecture (55 min VTT, filed under Week 4 with its deck). 63 windows were cleaned
  and flagged for $0.27; re-runs and deck re-parses cost $0.
  - Alignment against a hand-made timeline: **74.6% exact / 88.9% within ±1** on all
    windows, **79.4% / 91.2%** on non-boundary windows. Full write-up in `docs/eval.md`.
  - Accuracy is good enough for now: **no more tuning or labelling** unless asked.
  - Most remaining misses are one slide early (rough label times, plus lecturers
    previewing the next slide).
  - Known gap: "worked example" is flagged too broadly (analogies count). Tighten it in
    the next cleaning-prompt change (that re-runs every window, about $0.27 per
    lecture).
- **Phase 4, part 1 is done: index, hybrid search and cited Q&A over lectures** (slides,
  lecturer notes, transcripts). Checked on SWEN30006 (154 chunks, $0.0065 to embed).
  Answers cost about $0.005–0.009 and "where" lists about $0.014; repeats cost $0.
  - **Next (part 2, after the user has tried Q&A):** tutorials and solutions (sections,
    type/coverage labels, pairing, viewer), the board's four-column layout, then the
    eval (`qa-eval.csv`, recall@5 for keyword/embedding/hybrid).
  - The approved decisions are in the phase 4 plan. Tutorials belong to their board
    week; ignore week numbers inside tutorial files.
  - The database has two SWEN30006 subjects; the older one (05:11) is empty.
- **Phase 4 UI redesign is done** (matches `docs/design/` mockups): Ask is a chat with
  saved conversations, streamed answers and a source preview panel; Board and Review
  table restyled. Prompts are now `qa-v3` (topic + follow-ups, earlier turns as context)
  and `where-v2` (summary + follow-ups).

## Commands

- `uv sync`: install (Python 3.13, pinned in `.python-version`)
- `uv run fastapi dev app/main.py`: run locally
- `uv run pytest`: tests (SQLite in memory + `FakeStorage`; no network)
- `uv run ruff check . && uv run ruff format .`: lint and format (line length 100)
- `uv run alembic upgrade head`: apply migrations to the database in `DATABASE_URL`
- `uv run python -m scripts.setup_storage`: create the storage bucket if it's missing
- `uv run python -m scripts.align_labels <transcript>`: write a blank alignment labelling
  sheet to `docs/samples/` (`<transcript>` is a file id or part of the filename)
- `uv run python -m scripts.timeline_labels <timeline.txt> <transcript>`: build that sheet
  from a hand-made slide timeline (`mm.ss pN` lines), by majority overlap, marking
  boundary windows
- `uv run python -m scripts.align_eval <sheet.csv> [--sweep]`: alignment accuracy, and
  optionally a sweep over penalty settings (cached embeddings, no API cost)
- `uv run python -m scripts.reindex <subject>`: build or refresh a subject's search index
  (code, name or id; unchanged chunks cost nothing)
- `uv run python -m scripts.compare_clean <transcript> --model gpt-6-luna --n 15`: compare
  two cleaning models side by side in HTML

## Architecture and conventions

- **Module boundaries:** `api/` (JSON) and `web/` (HTML/HTMX) are thin. Logic lives
  in `services/`, so both use the same code. `classify/` is pure: no DB, no I/O
  beyond reading the bytes it is given.
- **Classifier rules:** every regex and keyword table lives in
  `app/classify/patterns.py`. Each rule change gets a case in `tests/test_classify.py`
  that uses a realistic filename. Each guess stores `classify_confidence` and a
  human-readable `classify_reason`. Guesses below `LOW_CONFIDENCE` (0.6) are highlighted
  until `reviewed` is set.
- **Titles:** `files.title` holds the topic read from the document at upload. It is
  the largest text on the first 3 pages, and it only counts if it's set larger than
  the body text (`classify/peek.py` + `classify/titles.py`). The UI shows
  `display_title()`: the document title, else a cleaned filename, else a label such as
  "Lecture 5". Week/lecture numbers are stripped from titles because the UI shows them
  separately.
- **Week board:** `services/board.py` places files into slots, pairs slides with
  transcripts per lecture (`group_lectures`), groups tutorials with their solutions
  (`group_tutorials`: shared filename number, else the week's only tutorial), and computes
  per-week completeness. Four columns: Lecture, Lecture transcript, Tutorial (questions +
  solutions in one card), Other. A week is complete when it has slides, a transcript for
  every lecture, and a tutorial, and none of its lecture files is blocked; missing
  solutions are never a gap. Blockers, shown instead of "Complete": Pairing needed,
  Processing, Processing failed, Not processed yet. Solutions with a week
  are stored in the `tutorials` slot. `transcripts` and `week` slots are drop targets
  only. The subject cards' "weeks complete" bar uses the same calculation.
- **Status lines and type labels** for the board and table live in `web/status.py`.
- **The classifier's guess is never overwritten.** User edits change
  type/week/lecture and set `reviewed = true`. `classify_*` keeps the original guess.
- **Dedupe:** unique `(subject_id, sha256)`. A duplicate is detected before anything is
  written. Storage paths are content-addressed (`<subject_id>/<sha256>.<ext>`), so
  writing the same path twice is harmless.
- **DB and Storage consistency:** an upload writes the object first and the row second,
  and removes the object if the insert fails. A delete removes the row, deletes the
  object, then commits.
- **Readings** default to `included = false`. Changing a file's type to or from
  `reading` resets `included`.
- **Schema changes go through Alembic only.** Migrations are hand-written in
  `migrations/versions/` and must match `app/db/models.py`. Types and statuses are text
  columns with CHECK constraints, not PG enums.
- **Secrets:** they come from `.env` through `app/config.py` as `SecretStr`. Never
  hard-code, print or log them. Call `.get_secret_value()` only where the value is
  used. `.env` is git-ignored, and `.env.example` documents the keys.
- **Frontend:** server-rendered Jinja + HTMX (vendored at `app/web/static/htmx.min.js`).
  Custom JS: `upload.js` (page-wide file drops, per-file progress, placeholder cards,
  instant moves; the server re-renders the board afterwards; any `[data-upload]` opens
  the picker) and `ask.js` (composer, streaming, source panel). The subjects page uses a
  native `<dialog>`. Design tokens (mockup colours, Inter, JetBrains Mono) are CSS
  variables at the top of `style.css`, with dark-mode values. Content width is
  `--width`, 1240px; the Ask page is full-bleed. Subject pages set
  `{% block nav_active %}` to highlight the top nav (Wiki/Practice are placeholders). There is no build step. Phase 7 may add a SvelteKit/Next.js app against
  the `/api` routes.
- **Uploads:** one request per file, 3 at a time. The accepted and rejected extension
  lists are in `patterns.py` and passed to the page as JSON, so the browser and server
  agree. The size limit is `max_upload_bytes` (50 MB, the Supabase free-tier limit).

## Paid model calls (all phases)

- **Provider interface:** `app/llm/client.py`. `OpenAIProvider` makes raw calls
  (`structured`, `embed`); `LlmClient` adds the cache. Use
  `structured_request` / `call` / `store` when calls run in worker threads (only `call`
  runs in the thread; database reads and writes stay on the main thread), or
  `structured(...)` for one-off calls. `embed()` caches each text on its own and makes
  only one call per batch of missing texts.
- **Every LLM and embedding call goes through `app/llm/cache.py`.** Build a fingerprint
  dict of everything that determines the output: provider, model, full prompt text,
  schema and parameters, input text, and sha256 of any image (never the raw bytes).
  Then use `LlmCache.get_or_call(fingerprint, kind=..., prompt_version=..., call=...)`,
  or `get_many` and `put` for batches. Never key on a version label alone.
- `llm_cache` rows are keyed by the request hash and store the response JSON, tokens,
  cost, kind, provider, model and `prompt_version` (for reporting). Failed calls and
  refusals are not stored.
- Editing text shared by every request (a system prompt) re-runs every request. Keep
  per-item instructions out of the shared prompt when only some items need them.
- Phase 3 kinds to use: `transcript_clean`, `transcript_segment`, `embedding` (store
  embeddings as `{"embedding": [...]}`).

## Slide parsing (phase 2)

- **Pipeline:** `app/parse/` runs `document` (open, convert, render, measure), then
  `dedupe`, then `routing`, then `text_path` or `vision`. `pipeline.Parser` writes
  `slides` rows and runs in `app/jobs/worker.py`: one file at a time in a thread, pages
  concurrently. Job state lives in `files.status` (uploaded, parsing, parsed or failed,
  with the message in `status_detail`). Files left in `parsing` are re-queued on startup.
- **Template:** images, vector paths and text lines repeated on at least 60% of pages
  (logos, header bars, running headers) are ignored by routing and dedupe.
- **Routing thresholds** in `parse/routing.py` were tuned on SWEN30006 L6/L7. A page
  goes to vision if it has non-template images covering ≥8% of the page, ≥20 vector
  paths, ≥5 paths spread over ≥15% of the page, or maths fonts. Every decision stores
  its reason and raw signals (`route_signals`) for re-tuning.
- **Dedupe:** build steps (same title, and the text or the visual grows) point at the
  last page of the run. Exact repeats point at their first occurrence. Only kept pages
  can go to vision; earlier build steps' captions are passed to the kept page's call.
- **Cache:** vision calls go through `app/llm/cache.py` with `kind="slide_vision"`.
  The key is the hash of `OpenAIVision.fingerprint()`: provider, API, model, full
  system prompt, input text (text layer and build captions), image sha256, image
  detail, output schema, reasoning effort and token limit. `slides.request_hash` links
  each row to its `llm_cache` entry. Still bump `PROMPT_VERSION` when you edit the
  prompt: it is stored for reporting (the viewer's "older prompt" flag), but it is not
  part of the key. Page images are stored at `pages/<sha256>/<page>.png`, shared across
  subjects, and removed with the last file that has that content.
- **Cost:** `slides.cost_usd` is the cost of producing that content. `cached=true`
  means this parse paid nothing for it. Prices are in `parse/pricing.py`; add a row
  when changing `SLIDES_MODEL`.
- **PPTX:** converted with LibreOffice (`soffice`, hidden slides excluded) and stored at
  `converted/<pptx sha256>.pdf`. Speaker notes are matched to pages by position, and
  only attached when the PDF has one page per visible slide; otherwise they're dropped
  with a warning rather than misaligned.
- **Speaker notes** stay out of the model request and out of `content_json`.
- **Model output** is rendered as markdown with raw HTML disabled.
- **Control characters** (incl. U+0000, which some PDFs use for unmapped bullet glyphs)
  are stripped from the text layer and speaker notes at extraction (`clean_text`):
  Postgres text/JSONB can't store NUL, and one bad glyph failed a whole deck.
- **Incomplete responses:** `OpenAIProvider.structured` checks the response status
  before the SDK parses the JSON, so a cut-off output reports why. A `content_filter`
  stop (e.g. transcribing a copyrighted comic) raises `OutputFiltered`; slide vision
  treats it like a refusal (text layer, reason recorded, not cached) so the deck still
  parses. Failed files show their stored error on hover on the board and table.

## Transcripts (phase 3)

- **Pipeline:** `app/transcripts/` runs `formats` (VTT/SRT/TXT into cues, keeping
  Echo360's per-word confidence), then `windows` (cues into sentences into windows of
  30–60 s / 100–150 words, or words only without timestamps), then `clean` (one call per
  window: cleaned text plus flags), then embeddings, then `align`, then
  `transcript_segments` rows.
  `TranscriptProcessor` runs in the same worker as slides; a deck that isn't parsed yet
  is parsed first.
- **Pairing:** `transcript_decks` holds one or more decks per transcript, in order. Auto
  pairing: lecture number (and week, when both have one); else the week's only deck when
  it's the week's only transcript (unless both have differing lecture numbers). Anything
  else gets a `pairing_suggestions` row: the deck in its week with the most similar
  slide text (TF-IDF, `transcripts/suggest.py`, no model call), shown on the board,
  table and transcript view for the user to confirm ("Confirm and process" pairs it
  manually and queues processing). Suggestions are recomputed when the week's decks or
  their parse status change, and are never used until confirmed. Pairing clears a
  "pair it first" failure. Manual pairing (transcript view, or
  `PUT /api/files/{id}/pairing`) is never replaced; "reset" goes back to automatic.
- **Cleaning** (`clean-v1`): the glossary is built from the paired decks' slide text,
  visual facts and speaker notes, with no model call (`glossary.py`). The request puts
  the shared parts first (prompt, then glossary) so they get cached-input prices.
  - Flags are judged on the raw text, and each quote is checked against it.
  - Guards: at least 85% of cleaned words must come from the raw text or the glossary,
    and exam-hint quotes must survive. Otherwise the raw text is kept and
    `clean_status = rejected`.
  - `[Other speaker]` marks speech from anyone but the main speaker.
- **Alignment** (`align.py`, pure numpy): cosine similarity, centred per window, then
  Viterbi over (slide, on or tangent) states with move penalties, then forward-backward
  confidence. Low confidence or tangent leaves the window unaligned (`slide_id` null).
  Targets are kept slides only; build steps and repeats resolve to their kept page.
  - **Admin signal:** windows the cleaning pass flagged as admin score
    `admin_tangent = 0.15` instead of `tangent = 0.04`, so intro and after-class talk
    stays unaligned. Embeddings alone can't separate it from content. On GRASP this
    was +9 points exact on non-boundary windows.
  - The `AlignParams` defaults are measured, not guessed (`docs/eval.md`). Change them
    only with a labelled lecture and `scripts.align_eval --sweep`. Treating repeat
    pages as their own deck positions was tried and made things worse.
- **Embedding model:** `text-embedding-3-large` at 1536 dimensions (`EMBEDDING_*`
  settings), shared with phase 4.
- **Slide ids are derived:** `uuid5(subject_id:sha256:page_no)` (`models.slide_id`).
  Re-parsing updates rows in place, so transcript links survive. After a deck is
  re-parsed, its paired transcripts are re-queued, which costs nothing unless slide
  text changed. `/slides/{id}` resolves a collapsed page to its kept page.
- **Decks take their title** from the parsed first pages.

## Search and Q&A (phase 4)

- **Index** (`app/search/`): `chunks.py` decides what gets indexed and as what text.
  Kept slides, speaker notes (their own `notes` chunk) and transcript segments are
  indexed; admin-flagged segments are not. `index.py` upserts chunks by derived id
  (`chunk_id(subject, source_key)`) and only embeds chunks whose `content_hash` changed.
  It runs after every parse or transcript job (`ParseQueue._index`), and the result goes
  in `status_detail`.
- **`chunks.tsv`** is a generated Postgres column (title A, body B, GIN index) that is
  not mapped in the models, because SQLite tests can't have it. `embedding` is
  `vector(1536)` with an HNSW index, so `EMBEDDING_DIMENSIONS` must stay 1536.
- **Week, type and `included`** are read from `files` at query time, so board edits
  apply without re-indexing.
- **Search:** BM25 is computed in Python (`rank.py`) from term counts that Postgres FTS
  returns, because `ts_rank` has no IDF. That is fused with pgvector nearest neighbours
  by RRF (k=60, 50 from each list). `PgBackend` holds all the SQL; tests use
  `tests/search_fixtures.MemoryBackend`, which has the same interface.
- **Answers** (`app/qa/answer.py`, `qa-v2`): the top 8 chunks are sent as handles
  S1..S8. `cited` is an enum of those handles, so the list can't name a source that
  wasn't sent, and inline markers are validated by `check()`. If the best similarity is
  under 0.2, the app answers "not covered" without a call. In-domain off-topic questions
  still score about 0.4, so the model's `not_covered` is the real check.
- **"Where" questions** (`app/qa/where.py`, `where-v1`): detected by regex, or forced
  with mode=where. The top 40 fused results plus phrase matches are judged main or
  mentioned in one call. Results are grouped by week, and neighbouring slides and
  moments are collapsed.
- **Citation labels and links** come from `qa/labels.py:SourceRef`. Transcript chips go
  to `#seg-{idx}`, and notes chips go to the slide's `#notes`.
- **Chats** (`chats`, `chat_messages`; `services/chat.py`, `web/ask.py`): titled from the
  first question. An answer is stored as a snapshot (sources with labels, citations,
  follow-ups) so old chats render the same after re-indexing or deletes. Follow-ups:
  retrieval fuses a search of the question with one including the previous question;
  the answer request gets the last 3 turns (citations stripped). "Where is this taught?"
  with a pronoun uses the previous answer's topic.
- **Streaming:** `POST /subjects/{id}/ask/stream` returns NDJSON events (start, status,
  delta, done, error). A worker thread with its own session (`get_session_factory`)
  answers and saves, so the answer is kept if the browser leaves. `LlmClient.structured(
  on_text=...)` streams through `OpenAIProvider.structured_stream`; same cache key as a
  non-streamed call, and cache hits return at once. `qa/stream.py` decodes the partial
  `answer_markdown` and drops half-written `[S` markers.
- **Source panel:** chips carry `data-preview` (`/ask/source/{chunk_id}`): slide image,
  prev/next kept slide, lecturer notes, "What was said" with the cited moment
  highlighted.

## Supabase notes

- `DATABASE_URL` uses the **session pooler** (port 5432), which works for both Alembic
  and the app.
- `SUPABASE_URL` may include `/rest/v1/`. The config strips it to the project origin.
- The storage bucket `subject-files` is private, and the service key is server-side only.
