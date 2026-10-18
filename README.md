# subject-wiki

Turns a subject's scattered resources (slides, lecture transcripts, tutorials, past
papers) into one searchable, cited knowledge base. See [docs/spec.md](docs/spec.md).

**Status: phase 1, the upload site and storage.** You create a subject, drop in a
semester of files, and each file is hashed, auto-classified (type, week, lecture,
timestamps) and stored. You fix any wrong guesses on a week board or in a review table.
Parsing, LLM calls, embeddings and search come in later phases.

## Stack

FastAPI · SQLAlchemy 2 + Alembic · Supabase Postgres (pgvector enabled, not used yet) ·
Supabase Storage · Jinja + HTMX pages · Python 3.13 via [uv](https://docs.astral.sh/uv/).

## Setup

1. **Install dependencies**

   ```sh
   uv sync
   ```

2. **Configure Supabase.** Copy `.env.example` to `.env` and fill in:

   | Variable | Where to find it |
   | --- | --- |
   | `SUPABASE_URL` | Project Settings → API → Project URL |
   | `SUPABASE_SERVICE_KEY` | Project Settings → API Keys → secret key (server-side only) |
   | `DATABASE_URL` | Connect → **Session pooler** connection string (port 5432) |

   `.env` is git-ignored. The app never logs these values.

3. **Create the tables** (this also enables the `vector` extension):

   ```sh
   uv run alembic upgrade head
   ```

4. **Create the storage bucket** (private, named `subject-files`; safe to re-run):

   ```sh
   uv run python -m scripts.setup_storage
   ```

## Run locally

```sh
uv run fastapi dev app/main.py
```

Open http://127.0.0.1:8000. The JSON API is documented at http://127.0.0.1:8000/docs.

## Using it

1. **Create a subject** with a name, code, semester and number of teaching weeks. The
   code (e.g. `COMP30027`) stops subject numbers being read as week or lecture numbers.
2. **Drop files** onto the big drop zone and the classifier decides where each one goes. You
   can also drop them onto a slot on the **week board** (e.g. Week 3 → Tutorials) to file
   them there directly. Accepted types: PDF, PPTX, DOCX, VTT, SRT, TXT, up to 50 MB each.
   Audio and video are rejected for now; upload the lecture's `.vtt` captions instead.
3. **Fix guesses.** The board has one row per week: lectures (slides and transcript
   paired, with the topic read from the slides), tutorials and solutions. Dots show
   whether the week has slides, a transcript for every lecture, and a tutorial. Drag
   chips between sections to fix them. In the **review table**, edit
   type, week and lecture inline. Low-confidence guesses are highlighted until you edit
   or confirm (✓) them. Readings are excluded by default and can be toggled back in.
4. **Re-uploading is safe.** Files are deduplicated by SHA-256 within a subject, so
   dropping the same folder again changes nothing and each file shows "Already uploaded".

## Transcripts

Prefer the **VTT** export from the lecture-capture tool. It keeps timestamps, which phase 3
uses to align speech to slides, plus per-word confidence (`NOTE CONF` lines). Plain-text
`.txt` transcripts are accepted too and recorded with `has_timestamps = false`.

## Development

```sh
uv run pytest            # tests: in-memory SQLite + fake storage, no network or secrets
uv run ruff check . && uv run ruff format .
uv run alembic revision -m "describe change"   # new migration (write it by hand)
```

## Layout

```
app/
  api/        JSON API (/api/...)
  db/         SQLAlchemy models and session
  storage/    Supabase Storage client
  classify/   filename + content rules (patterns.py), content peek, classifier
  services/   upload pipeline, file edits/deletes, week-board placement
  web/        Jinja templates, HTMX partials, upload.js, CSS
migrations/   Alembic migrations
scripts/      one-off setup (storage bucket)
tests/
```
