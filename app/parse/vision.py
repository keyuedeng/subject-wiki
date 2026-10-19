"""The vision path: one model call per page, image + text layer in, SlideContent out.

Results are cached by a hash of the full request (see fingerprint() and app/llm/cache.py),
so any change to what is sent re-runs only the requests it changes. PROMPT_VERSION is a
label for reporting: bump it when you edit SYSTEM_PROMPT so the viewer can show which
pages were parsed with which prompt.
"""

import base64
from dataclasses import dataclass
from typing import Protocol

from app.parse.schema import SlideContent

# v2: speaker notes are no longer sent (they're stored separately, in slides.notes).
PROMPT_VERSION = "slides-v2"
MAX_OUTPUT_TOKENS = 16_000

SYSTEM_PROMPT = """\
You convert one lecture slide into structured notes for a study tool used by a student \
taking this subject. You receive the slide image and the text layer extracted from the \
PDF (accurate for wording, unreliable for layout and maths). You may also receive the \
captions of earlier build steps of this same slide.

Text
- Reproduce all slide text in content_markdown, preserving headings, bullet hierarchy, \
emphasis and tables (as markdown tables). Start with the title as a "# " heading.
- Prefer the text layer's wording; use the image for structure and for anything the \
text layer misses (text inside images, diagrams and screenshots).
- If earlier build-step captions are given, include them in order after the slide's own \
text, so the narration of the build is kept.
- Keep the lecturer's own notation and terminology. Do not correct, modernise or add \
explanations that are not on the slide.
- Skip page numbers, logos, copyright footers and the course name repeated in headers.

Visuals
- Add one entry to visuals per diagram, chart, table, screenshot or image.
- description: explicit prose, enough for a student to redraw it from your words alone. \
Never write only "a class diagram"; name every element.
- key_facts: short, independently checkable facts, one per element or relationship. \
Empty for decorative images.
- UML class and domain models: every class with stereotype, attributes (with types and \
visibility if shown) and operations; every association with both ends' multiplicities, \
role names, navigability, and whether it is aggregation, composition, generalisation, \
realisation or dependency. Example fact: "Member 0..1 — 0..5 Copy (association, borrows)".
- UML sequence diagrams: every lifeline (with its type), then every message in order \
with sender, receiver, label, and whether it is synchronous, asynchronous, a return or a \
create; name any alt/opt/loop fragment and its guard. Example fact: \
"3. :Register → :Sale makeLineItem(id, qty) (sync, inside loop [more items])".
- UML state machines: initial and final states, every state (and nesting), and every \
transition as "Source --trigger [guard] / action--> Target".
- Other diagrams: every node and every labelled edge. Charts: axes, series and the \
takeaway. Decorative photos: kind "photo" with a one-line description.
- If something is too small or blurry to read, write [illegible] rather than guessing.

Equations go in equations as LaTeX. Code goes in code_blocks with its language, \
transcribed exactly; also show it in content_markdown as a fenced block.
"""


class VisionError(Exception):
    pass


@dataclass(frozen=True)
class Usage:
    input_tokens: int
    cached_tokens: int
    output_tokens: int  # includes reasoning tokens
    reasoning_tokens: int


@dataclass(frozen=True)
class VisionResult:
    content: SlideContent | None  # None when the model refused
    usage: Usage
    model: str
    refusal: str | None = None


class VisionModel(Protocol):
    provider: str
    model: str

    def fingerprint(self, png_sha256: str, text: str, build_captions: list[str]) -> dict:
        """Everything that determines the output of describe(), for the cache key."""
        ...

    def describe(self, png: bytes, text: str, build_captions: list[str]) -> VisionResult: ...


def user_text(text: str, build_captions: list[str]) -> str:
    parts = [f"Extracted text layer:\n{text.strip() or '(none)'}"]
    if build_captions:
        steps = "\n".join(f"{i}. {c}" for i, c in enumerate(build_captions, 1))
        parts.append(f"Captions of earlier build steps of this slide:\n{steps}")
    return "\n\n".join(parts)


class OpenAIVision:
    """Slide vision on the shared provider (app/llm/client.py). The fingerprint is
    unchanged from phase 2, so existing cache entries stay valid."""

    provider = "openai"

    def __init__(self, api_key: str | None, model: str, reasoning_effort: str):
        from app.llm.client import OpenAIProvider

        self.model = model
        self.reasoning_effort = reasoning_effort
        self._provider = OpenAIProvider(api_key)

    def fingerprint(self, png_sha256: str, text: str, build_captions: list[str]) -> dict:
        return {
            "provider": self.provider,
            "api": "responses.parse",
            "model": self.model,
            "instructions": SYSTEM_PROMPT,
            "input_text": user_text(text, build_captions),
            "image_sha256": png_sha256,
            "image_detail": "high",
            "output_schema": SlideContent.model_json_schema(),
            "reasoning_effort": self.reasoning_effort,
            "max_output_tokens": MAX_OUTPUT_TOKENS,
        }

    def describe(self, png: bytes, text: str, build_captions: list[str]) -> VisionResult:
        from app.llm.client import ProviderError

        image_url = "data:image/png;base64," + base64.b64encode(png).decode()
        try:
            response = self._provider.structured(
                model=self.model,
                instructions=SYSTEM_PROMPT,
                content=[
                    {"type": "input_image", "image_url": image_url, "detail": "high"},
                    {"type": "input_text", "text": user_text(text, build_captions)},
                ],
                schema=SlideContent,
                effort=self.reasoning_effort,
                max_output_tokens=MAX_OUTPUT_TOKENS,
                cache_hint=PROMPT_VERSION,
            )
        except ProviderError as exc:
            raise VisionError(str(exc)) from exc
        u = response.usage
        usage = Usage(u.input_tokens, u.cached_tokens, u.output_tokens, u.reasoning_tokens)
        if response.parsed is None:
            return VisionResult(None, usage, self.model, response.refusal)
        return VisionResult(response.parsed, usage, self.model)
