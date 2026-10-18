# Subject Wiki — Project Spec

Oct 18, 2026 · @Karen

## Overview

Subject Wiki turns a subject's scattered resources into one searchable, cited knowledge base, then generates study material from it: explanations, practice exams and worksheets grounded in what was actually taught.

The problem is not a lack of content. A typical subject has 12 weeks of slides, recordings, readings, tutorial sheets and past papers spread across the LMS, and students waste time finding things, cramming before exams, and getting generic answers from chatbots that don't match their lecturer's notation or scope.

**One-line pitch:** a study companion that only knows your subject, always shows where its answer came from, and tells you what to study next.

## What students actually need

The most valuable features answer "what do I study, and am I ready?" rather than "summarise this PDF". Ranked by how much time they save:

| Rank | Pain point | What fixes it |
| --- | --- | --- |
| 1 | Not knowing what's examinable or how ready they are | Topic map with coverage and a readiness score per topic |
| 2 | Practice questions run out; past papers have no solutions | Unlimited exam-style questions matched to past-paper format, with worked solutions |
| 3 | "Where was that covered?" | Search and Q&A that cite the exact lecture and slide |
| 4 | Falling behind, then a week of slides piles up | Weekly digest: what was covered, key definitions, what links to earlier weeks |
| 5 | Lecturer notation differs from textbooks and the internet | Answers grounded only in course material, using the course's own notation |
| 6 | Forgetting earlier weeks before the final | Spaced-repetition flashcards generated from the material |
| 7 | Not understanding a step in a derivation or diagram | "Explain this slide" at three levels: quick, detailed, from first principles |

The common thread: everything must be grounded and cited. A student who can't verify an answer won't trust it before an exam.

## Features

The MVP is ingestion, cited Q&A and practice exams for one subject; everything else builds on that core.

### MVP

1. **Get the material in.** In v1, a web page where you create a subject and drag in its files. The app suggests each file's type and week from its name and content, and you confirm or fix them in one table. In v2, a browser extension pulls the files straight from the LMS instead.
2. **Parse slides and transcripts.** Slides become per-slide chunks; lecture transcripts are cleaned, segmented and aligned to the slide being shown, so each slide carries what the lecturer actually said about it.
3. **Auto-generated wiki.** One page per topic, built from the material: definitions, key ideas, worked examples, and links back to every slide and transcript moment that covers it.
4. **Cited Q&A.** Ask anything; the answer cites the slide or the transcript timestamp it came from, and says so when the material doesn't cover it.
5. **Summarise and explain.** Per lecture, per week, or per slide, at three depth levels, including points the lecturer stressed verbally that aren't on the slides.
6. **Practice exam generator.** Pick topics, difficulty and length; get a paper in the same format as past exams (MCQ, short answer, long answer) with a separate marking guide.

### Next

- **Practice sheets and booklets** exported to PDF: a topic's questions, grouped easy to hard, with space to write and solutions at the back.
- **Answer marking.** Type or photograph a handwritten answer; get marked against the guide with feedback on what's missing.
- **Readiness tracker.** Each attempt updates a per-topic mastery score, so the app can say "spend tonight on dynamic programming".
- **Flashcards** with spaced repetition, generated from definitions and key results.
- **Concept graph.** Prerequisite links between topics, so a weak answer on topic B points back to topic A.

### Stretch

- Study planner: exam date in, daily plan out, weighted by readiness and topic weighting.
- Shared subject wikis for a class, with student annotations.
- Formula sheet builder for exams that allow one.

## Source priority

Ingest only what is examinable and taught; readings are skipped by default because they add bulk and dilute retrieval with material the subject rarely tests.

| Priority | Source | v1: upload | v2: extension | How it's processed |
| --- | --- | --- | --- | --- |
| 1 | Lecture slides | Drag in the PDFs or PPTX | Read from the LMS files and modules | Per-slide chunks: text layer plus vision model for diagrams and equations |
| 2 | Lecture transcripts | Upload caption files (VTT, SRT) or text transcripts; upload the recording if none exist | Fetch captions from the lecture-capture tool | Clean filler words, segment by topic, align to slides by timestamp or text similarity; Whisper for recordings |
| 3 | Tutorials and solutions | Drag in the files | Read from the LMS | Per-question chunks, linked to the topics they practise |
| 4 | Past papers, assignment specs, subject outline | Drag in the files | Read from the LMS | Past papers as style examples for the exam generator; outline sets the topic list and weighting |
| Off | Readings | Not uploaded unless chosen | Skipped unless chosen | Not ingested by default |

Transcripts are where the high-value hints live: "this will be on the exam", worked examples done live, and explanations the slides compress into one line. Flag these moments when parsing.

Transcripts can be downloaded from the lecture-capture tool, so v1 uploads them directly and Whisper is only a fallback. Open question: whether the downloads keep timestamps (VTT or SRT) or are plain text, since timestamps make slide alignment much more accurate.

## Architecture

Parse and index each subject once; every study skill then runs on retrieval over that index, which is what lets every output cite its slides.

&#91;embedded content: system architecture · ingest once, retrieve for every skill\]

The top row runs as a background job when material arrives; the bottom row runs per request. Key design decisions:

- **Chunk per slide, not per N tokens.** Slides are natural units, and "Week 4, slide 12" is a citation students can check.
- **Retrieval before generation, for every skill.** An exam question on recursion is generated from the recursion chunks plus past-paper examples, never from the model's general knowledge alone.
- **Structured outputs.** Questions, solutions and marks come back as JSON with a schema, so they can be rendered, exported and scored.
- **Student data feeds back.** Attempts update per-topic mastery, which steers what the planner and exam generator focus on.

## Tech stack

Python end to end for the backend keeps the ML and parsing work in one ecosystem; a light web frontend is enough.

| Layer | Choice | Why |
| --- | --- | --- |
| Ingestion | v1: drag-and-drop web upload. v2: Chrome extension (Manifest V3) on the LMS and lecture-capture pages | No API token needed; the extension uses the student's own logged-in session |
| Parsing | PyMuPDF for text and page renders; vision LLM for image-heavy slides | Cheap path for text, accurate path for diagrams |
| Other formats | python-pptx, python-docx, webvtt-py for captions, Whisper when no captions exist | Covers what lecturers actually post |
| Storage | Postgres + pgvector | Metadata and embeddings in one database |
| Retrieval | Hybrid search: BM25 + embeddings, then a reranker | Keyword search catches exact terms like "Dijkstra" |
| LLM | One API model for generation, structured JSON outputs | Questions and marking guides need a fixed schema |
| Backend | FastAPI + a job queue (Celery or RQ) | Parsing a subject is slow; run it in the background |
| Frontend | Next.js or SvelteKit | Wiki pages, chat, exam view |
| Export | Typst or LaTeX to PDF | Clean maths in booklets and exams |
| Eval | pytest + a small labelled question set | Measures retrieval and answer quality over time |

## Build plan

Build against one real subject you're taking, so every phase is tested on material you know well. Each phase ends with something usable.

1. **Upload site and storage.** A web page to create a subject and drag in files; auto-suggest each file's type and week, confirmed by you in a table; store files and metadata, dedupe by file hash. Done when a semester of material uploads in a few minutes.
2. **Slide parsing.** Text path plus vision path, build-slide dedupe, caching by file hash. Done when a full deck parses with diagrams described and equations in LaTeX.
3. **Transcript pipeline.** Accept caption files and text transcripts; run Whisper on uploaded recordings; clean, segment by topic and align segments to slides. Done when each slide shows what was said while it was on screen.
4. **Retrieval and cited Q&A.** Chunk, embed, hybrid search over slides and transcripts, answer with slide and timestamp citations. Write 30 to 50 questions you know the answers to and measure how often the right source is retrieved.
5. **Topic extraction and wiki.** Cluster chunks into topics, generate one page per topic with links back to sources. Hand-check that the topic list matches the subject outline.
6. **Practice exams.** Feed past papers as style examples; generate questions plus marking guides as structured JSON; render to PDF. Validate that every question cites the material it tests.
7. **Study app.** Wiki, chat and exam generator on top of the upload site. Use it yourself through a real exam period.
8. **Marking and readiness.** Answer marking, per-topic mastery, "study this next". Then flashcards and the concept graph.
9. **v2: browser extension.** Runs on the LMS while the student is logged in, lists a subject's files, skips readings, and sends the chosen files to the backend. Then extend it to the lecture-capture tool for captions.

Phases 1 to 6 are the v1 core and a strong project on their own; 7 and 8 turn it into a product; 9 is v2.

## Evaluation, risks and policy

The biggest risk is confidently wrong answers before an exam, so measurement is part of the build, not an afterthought.

**What to measure**

- Retrieval: recall@5 on your labelled question set (is the right slide in the top 5?).
- Faithfulness: share of answer claims supported by the cited chunk, checked by hand on a sample or with an LLM judge.
- Question quality: rate generated questions on correctness, difficulty match and whether they test the stated topic.
- Marking agreement: compare the app's marks with your own on 20 answers.
- Parsing: spot-check diagram descriptions and equations on 20 random slides.

**Risks**

| Risk | Mitigation |
| --- | --- |
| Hallucinated answers or wrong solutions | Answer only from retrieved chunks; show citations; say "not covered" when retrieval is weak |
| Generated exam questions with wrong answers | Have the model solve its own question independently and flag disagreements |
| Vision-model cost on large subjects | Route only image-heavy slides to it; cache all parses |
| Copyright and academic integrity | Keep each user's materials private to them; no redistribution; check your uni's AI and acceptable-use policies |
| LMS terms of use | v1 uses files the student downloaded themselves; the v2 extension reads only the student's own courses, rate-limited, and never stores credentials |
| Over-reliance replacing learning | Default to hints before full solutions; make active recall the main mode |

## Making it portfolio-worthy

What separates this from the many "chat with your PDF" projects is evidence: numbers showing it works, and engineering choices you can defend.

- **Publish an eval writeup.** Compare parsing approaches (text-only vs vision vs Docling or Marker) and retrieval setups (embeddings vs hybrid vs reranked) on your labelled set, with a results table. This is the ML/research signal.
- **Show the pipeline engineering.** Background jobs, caching, idempotent re-ingestion, cost per subject parsed. This is the software engineering signal.
- **Use it for real.** Report how you used it through one exam period and what changed.
- **Get five classmates on it.** Even small usage numbers and quotes beat a demo nobody has used.
- **Demo video.** 90 seconds: upload a subject, ask a cited question, generate an exam, mark an answer.

Open question: which subject to build against first. One with diagrams, maths and past papers will stress every part of the pipeline.
