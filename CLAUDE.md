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
- **Not built yet:** transcript pipeline (phase 3), embeddings and search (phase 4).
  pgvector is enabled but unused.

## Commands

- `uv sync`: install (Python 3.13, pinned in `.python-version`)
- `uv run fastapi dev app/main.py`: run locally
- `uv run pytest`: tests (SQLite in memory + `FakeStorage`; no network)
- `uv run ruff check . && uv run ruff format .`: lint and format (line length 100)
- `uv run alembic upgrade head`: apply migrations to the database in `DATABASE_URL`
- `uv run python -m scripts.setup_storage`: create the storage bucket if it's missing

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
  transcripts per lecture (`group_lectures`), and computes per-week completeness. A
  week is complete when it has slides, a transcript for every lecture, and a tutorial.
  The subject cards' "weeks complete" bar uses the same calculation.
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
  The only custom JS is `upload.js`, which handles page-wide file drops, per-file
  progress, placeholder chips and instant chip moves (the server re-renders the board
  afterwards). The subjects page uses a native `<dialog>`. Every page shares one
  content width (`--width`, 1120px). There is no build step. Phase 7 may add a SvelteKit/Next.js app against
  the `/api` routes.
- **Uploads:** one request per file, 3 at a time. The accepted and rejected extension
  lists are in `patterns.py` and passed to the page as JSON, so the browser and server
  agree. The size limit is `max_upload_bytes` (50 MB, the Supabase free-tier limit).

## Paid model calls (all phases)

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

## Supabase notes

- `DATABASE_URL` uses the **session pooler** (port 5432), which works for both Alembic
  and the app.
- `SUPABASE_URL` may include `/rest/v1/`. The config strips it to the project origin.
- The storage bucket `subject-files` is private, and the service key is server-side only.
