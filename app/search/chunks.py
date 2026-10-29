"""What gets indexed, and as what text. Pure: takes parsed rows, returns chunk specs.

- slide: each kept page (build steps and repeats point at a kept page and are skipped):
  title, content markdown, visual descriptions and key facts, equations, code
- notes: a slide's speaker notes as their own chunk (cited as "lecturer notes")
- transcript: each cleaned segment, titled with the slide it was aligned to; segments the
  cleaning pass flagged as admin are skipped

`embed_text` adds context (deck and slide titles) that helps embeddings but would make
keyword matches noisy, so keyword search sees only `title` and `body`.
"""

import hashlib
from dataclasses import dataclass, field

from app.db.models import Slide, TranscriptSegment

EMBED_CHARS = 6000  # cap per chunk for embedding
MIN_NOTES_CHARS = 20


@dataclass(frozen=True)
class ChunkSpec:
    source_type: str
    source_key: str
    position: int
    title: str
    body: str
    embed_text: str
    meta: dict = field(default_factory=dict)

    def content_hash(self, model: str, dimensions: int) -> str:
        data = "\x1f".join([model, str(dimensions), self.title, self.body, self.embed_text])
        return hashlib.sha256(data.encode()).hexdigest()


def slide_body(slide: Slide) -> str:
    content = slide.content_json or {}
    parts = [content.get("content_markdown") or ""]
    for visual in content.get("visuals") or []:
        kind = (visual.get("kind") or "figure").replace("_", " ")
        parts.append(f"Figure ({kind}): {visual.get('description') or ''}".strip())
        parts.extend(f"- {fact}" for fact in visual.get("key_facts") or [])
    for eq in content.get("equations") or []:
        context = f" ({eq['context']})" if eq.get("context") else ""
        parts.append(f"Equation{context}: {eq.get('latex', '')}")
    for code in content.get("code_blocks") or []:
        parts.append(f"```{code.get('language') or ''}\n{code.get('code', '')}\n```")
    return "\n".join(p for p in parts if p.strip()).strip()


def slide_chunks(slides: list[Slide], deck_title: str) -> list[ChunkSpec]:
    """Kept slides and their speaker notes."""
    out = []
    for s in sorted(slides, key=lambda s: s.page_no):
        if s.duplicate_of is not None:
            continue
        title = s.title or ""
        meta = {"page_no": s.page_no, "slide_id": str(s.id)}
        body = slide_body(s)
        if title or body:
            out.append(ChunkSpec(
                "slide", f"slide:{s.id}", s.page_no, title, body,
                f"{deck_title} · {title}\n{body}"[:EMBED_CHARS], meta,
            ))  # fmt: skip
        notes = (s.notes or "").strip()
        if len(notes) >= MIN_NOTES_CHARS:
            out.append(ChunkSpec(
                "notes", f"notes:{s.id}", s.page_no, title, notes,
                f"Lecturer notes on {title or 'a slide'}\n{notes}"[:EMBED_CHARS], meta,
            ))  # fmt: skip
    return out


def is_admin(segment: TranscriptSegment) -> bool:
    return any(f.get("type") == "admin" for f in segment.flags or [])


def transcript_chunks(
    segments: list[TranscriptSegment], transcript_sha: str, slides: dict
) -> list[ChunkSpec]:
    """`slides` maps aligned slide ids to their Slide rows (for titles and pages)."""
    out = []
    for seg in sorted(segments, key=lambda s: s.idx):
        text = (seg.clean_text or "").strip()
        if not text or is_admin(seg):
            continue
        slide = slides.get(seg.slide_id) if seg.slide_id else None
        title = (slide.title or "") if slide else ""
        meta = {
            "idx": seg.idx,
            "start_ms": seg.start_ms,
            "end_ms": seg.end_ms,
            "slide_id": str(slide.id) if slide else None,
            "slide_page": slide.page_no if slide else None,
            "flags": sorted({f["type"] for f in seg.flags or []}),
        }
        context = f"Lecture, on the slide: {title}\n" if title else "Lecture\n"
        out.append(ChunkSpec(
            "transcript", f"transcript:{transcript_sha}:{seg.idx}", seg.idx, title, text,
            (context + text)[:EMBED_CHARS], meta,
        ))  # fmt: skip
    return out
