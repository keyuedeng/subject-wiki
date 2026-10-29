"""Cited answers: the prompt, the request text, the output schema and citation checks.

Sources are given to the model as short handles (S1..S8) rather than chunk ids: models
copy short ids reliably, and the schema makes `cited` an enum of exactly the handles that
were sent, so the cited list can't name anything that wasn't retrieved. Inline markers in
the text are checked against the same set; unknown ones are removed.
"""

import re
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, create_model

from app.qa.labels import SourceRef

QA_PROMPT_VERSION = "qa-v3"
MAX_OUTPUT_TOKENS = 4000
SOURCE_CHARS = 1800  # per source sent to the model
HISTORY_TURNS = 3  # earlier question/answer pairs sent as context for a follow-up
HISTORY_ANSWER_CHARS = 600

QA_PROMPT = """\
You answer a student's question about one university subject, using only the numbered \
sources you are given: slides, the lecturer's speaker notes and lecture transcript \
excerpts from this subject.

Rules:
- Use only what the sources say. Never add facts, definitions, examples or steps from \
general knowledge, even when you know them. If something the question needs isn't in the \
sources, leave it out and say it isn't covered.
- Cite every claim inline with the id of the source that supports it, in square brackets: \
"Information Expert gives a responsibility to the class that has the information it \
needs [S2]." Put several supporting sources side by side: [S2][S5]. When a whole list \
comes from one source, cite it once, after the sentence that introduces the list. Cite \
only ids from the list.
- status: "answered" if the sources answer the question. "partial" if they answer only \
part of it: answer that part, and say in `missing` what isn't covered. "not_covered" if \
they don't answer it: answer_markdown says briefly that the uploaded material doesn't \
cover it, and `missing` says what was asked. A source that only shares words with the \
question doesn't count as covering it.
- Use the subject's own terms and notation, as the sources write them. Where a source is \
marked as likely to be examined, say so.
- Transcript excerpts are cleaned speech. Paraphrase or quote them faithfully.
- Be concise: the direct answer first, then supporting detail. Markdown lists and bold are \
fine; no headings, no raw HTML.
- `cited` lists every id you cited inline.
- The question may follow earlier turns of the conversation, shown before it. Use them \
only to understand what the question refers to ("it", "that example"); the facts must \
still come from the numbered sources, and only they can be cited.
- topic: the main concept the question is about, as the sources name it, in a few words \
(e.g. "Creator pattern"). Empty if there is none.
- follow_ups: two short questions the student might ask next, in their voice, that these \
sources could answer (e.g. "How is Creator different from Information Expert?"). Don't \
repeat the question.
"""

Status = Literal["answered", "partial", "not_covered"]
CITE_RE = re.compile(r"\[\s*(S\d+(?:\s*[,;]\s*S\d+)*)\s*\]")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


def handles(n: int) -> list[str]:
    return [f"S{i}" for i in range(1, n + 1)]


def answer_schema(ids: list[str]) -> type[BaseModel]:
    Handle = Literal[tuple(ids)]  # type: ignore[valid-type]
    return create_model(
        "CitedAnswer",
        __base__=_Strict,
        status=(Status, ...),
        answer_markdown=(str, Field(description="The answer, with inline [S1]-style citations.")),
        cited=(list[Handle], Field(description="Every source id cited inline.")),
        missing=(str | None, Field(description="What the sources don't cover, if anything.")),
        topic=(str, Field(description="The main concept asked about, in a few words.")),
        follow_ups=(list[str], Field(description="Two short follow-up questions.")),
    )


def source_text(handle: str, ref: SourceRef) -> str:
    body = ref.body if len(ref.body) <= SOURCE_CHARS else ref.body[:SOURCE_CHARS] + " …"
    return f"[{handle}] {ref.describe()}\n{body}"


@dataclass(frozen=True)
class Turn:
    """An earlier question and the answer it got (citation markers removed)."""

    question: str
    answer: str
    topic: str = ""


def strip_citations(text: str) -> str:
    return re.sub(r"[ \t]+([.,;:])", r"\1", CITE_RE.sub("", text)).strip()


def history_text(history: list[Turn]) -> str:
    lines = []
    for turn in history[-HISTORY_TURNS:]:
        answer = strip_citations(turn.answer)
        if len(answer) > HISTORY_ANSWER_CHARS:
            answer = answer[:HISTORY_ANSWER_CHARS].rsplit(" ", 1)[0] + " …"
        lines.append(f"Student: {turn.question.strip()}\nAnswer: {answer}")
    return "\n\n".join(lines)


def request_text(
    question: str, sources: list[tuple[str, SourceRef]], history: list[Turn] | None = None
) -> str:
    blocks = "\n\n".join(source_text(h, ref) for h, ref in sources)
    earlier = (
        f"Earlier in this conversation (context only, not a source):\n\n{history_text(history)}\n\n"
        if history
        else ""
    )
    return f"{earlier}Question: {question.strip()}\n\nSources:\n\n{blocks}"


@dataclass
class Checked:
    status: Status
    answer_markdown: str  # inline citations normalised to [S1][S3]; unknown ones removed
    cited: list[str]  # valid handles, in order of first use
    missing: str | None
    invalid: list[str] = field(default_factory=list)  # markers that named no source
    warnings: list[str] = field(default_factory=list)


def check(response: dict, ids: list[str]) -> Checked:
    """Validate the citations in a model response against the handles that were sent."""
    valid = set(ids)
    cited: list[str] = []
    invalid: list[str] = []

    def fix(match: re.Match) -> str:
        out = []
        for h in re.split(r"\s*[,;]\s*", match.group(1)):
            if h in valid:
                out.append(f"[{h}]")
                if h not in cited:
                    cited.append(h)
            else:
                invalid.append(h)
        return "".join(out)

    text = CITE_RE.sub(fix, response.get("answer_markdown") or "")
    text = re.sub(r"[ \t]+([.,;:])", r"\1", text).strip()  # space left by a removed marker
    status = response.get("status") or "not_covered"
    warnings = []
    if invalid:
        warnings.append(f"Removed {len(invalid)} citation(s) that pointed to no retrieved source.")
    if status != "not_covered" and not cited:
        warnings.append("This answer cites no sources, so treat it as unverified.")
    return Checked(status, text, cited, response.get("missing"), invalid, warnings)
