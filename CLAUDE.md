# CLAUDE.md

Subject Wiki: turns a subject's resources into a cited knowledge base. The product spec
is `docs/spec.md`, which lists the phases under "Build plan".

## Status

**Phase 1 in progress: upload site and storage.** It covers subjects, drag-and-drop
upload, SHA-256 dedupe, rule-based classification, a week board and a review table, and
delete. Parsing, LLM calls, embeddings and search are out of scope until later phases.
`files.status` (uploaded/parsed/failed) and `status_detail` exist for those phases, and
pgvector is enabled but unused until phase 4.

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

## Supabase notes

- `DATABASE_URL` uses the **session pooler** (port 5432), which works for both Alembic
  and the app.
- `SUPABASE_URL` may include `/rest/v1/`. The config strips it to the project origin.
- The storage bucket `subject-files` is private, and the service key is server-side only.
