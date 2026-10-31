"""Labelling tutorial sections: prompts, schemas, request texts, and turning the answers
into stored labels. Pure (the pipeline makes the calls).

Call A, once per tutorial: each section's type and the concepts it uses.
Call B, per section with concepts: given lecture excerpts found for each concept (hybrid
search over slides, lecturer notes and transcripts), is the concept explained in the
lectures? For a concept that isn't, a short summary drawn only from the tutorial's own
text, or nothing if the tutorial only uses it ("used in tutorial, not explained in your
material"). Never general knowledge.

A section's coverage is "new_concept" when any of its concepts isn't covered, else
"application". Lecture weeks are compared with the tutorial's board week by the caller
("ahead of lectures"); nothing here assumes a tutorial's week matches its content.
"""

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, create_model

from app.qa.labels import SourceRef
from app.qa.where import excerpt

SECTIONS_PROMPT_VERSION = "tute-sections-v2"
COVERAGE_PROMPT_VERSION = "tute-coverage-v1"
SECTION_CHARS = 2500  # per section, in either call
MAX_CONCEPTS = 6
EVIDENCE_PER_CONCEPT = 4
EVIDENCE_WORDS = 70
MAX_OUTPUT_TOKENS = 6000

SectionType = Literal["explanation", "worked_example", "exercise", "setup_admin"]

SECTIONS_PROMPT = """\
You label the sections of one tutorial sheet from a university subject. For each \
numbered section give:
- type:
  - "exercise": tasks or questions for the student to do (including their sub-parts)
  - "worked_example": the sheet works through a problem step by step and shows the result
  - "explanation": background or teaching text that explains ideas, including a \
scenario or case study that the exercises work on
  - "setup_admin": logistics, instructions for installing or setting up tools, readings \
lists, marking, agendas, the sheet's title block
- concepts: the subject concepts the section uses or teaches, at most 6, named the way \
lecture slides would name them ("Information Expert", "attack tree", "salted hash"), \
not generic words ("design", "security") and not tools' installation steps. Empty for \
setup_admin.
Label every section exactly once.
"""

COVERAGE_PROMPT = """\
A tutorial section uses some concepts. For each concept you get excerpts from the \
subject's lectures (slides, lecturer notes, lecture transcripts) that a search found for \
it. Decide, for each concept:
- status "covered" if at least one excerpt explains, defines or teaches the concept \
itself. List those excerpts in `evidence`. An excerpt that only mentions the word, or \
is about a different concept with a similar name, doesn't count.
- status "not_covered" otherwise, with `evidence` empty.
For a not_covered concept, `summary` is 2-4 sentences giving its definition and key \
idea, using ONLY what the tutorial section itself says. If the section uses the concept \
without explaining it, `summary` is an empty string. Never use general knowledge, never \
add an example. For a covered concept, `summary` is an empty string.
"""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


def ids(prefix: str, n: int) -> list[str]:
    return [f"{prefix}{i}" for i in range(1, n + 1)]


def _clip(text: str, chars: int = SECTION_CHARS) -> str:
    return text if len(text) <= chars else text[:chars].rsplit(" ", 1)[0] + " …"


# Call A


def sections_schema(section_ids: list[str]) -> type[BaseModel]:
    Sid = Literal[tuple(section_ids)]  # type: ignore[valid-type]
    item = create_model(
        "SectionLabel",
        __base__=_Strict,
        id=(Sid, ...),
        type=(SectionType, ...),
        concepts=(list[str], Field(description="At most 6 subject concepts, lecture names.")),
    )
    return create_model("TutorialSections", __base__=_Strict, sections=(list[item], ...))


def sections_request(title: str, sections: list[tuple[str, str, str]]) -> str:
    """`sections`: (id, title, markdown)."""
    body = "\n\n".join(f"[{sid}] {heading}\n{_clip(md)}" for sid, heading, md in sections)
    return f"Tutorial: {title}\n\nSections:\n\n{body}"


@dataclass
class SectionLabel:
    type: str
    concepts: list[str]


def read_sections(response: dict, section_ids: list[str]) -> dict[str, SectionLabel]:
    out: dict[str, SectionLabel] = {}
    for item in response.get("sections") or []:
        sid = item.get("id")
        if sid in section_ids and sid not in out:
            names = list(dict.fromkeys(c.strip() for c in item.get("concepts") or [] if c.strip()))
            if item["type"] == "setup_admin":
                names = []
            out[sid] = SectionLabel(item["type"], names[:MAX_CONCEPTS])
    return out


# Call B


def coverage_schema(concept_ids: list[str], evidence_ids: list[str]) -> type[BaseModel]:
    Cid = Literal[tuple(concept_ids)]  # type: ignore[valid-type]
    evidence = (
        list[Literal[tuple(evidence_ids)]]  # type: ignore[valid-type]
        if evidence_ids
        else list[Literal["none"]]
    )
    item = create_model(
        "ConceptCoverage",
        __base__=_Strict,
        id=(Cid, ...),
        status=(Literal["covered", "not_covered"], ...),
        evidence=(evidence, Field(description="Excerpts that explain the concept.")),
        summary=(str, Field(description="Only for not_covered, from the section's text.")),
    )
    return create_model("SectionCoverage", __base__=_Strict, concepts=(list[item], ...))


def coverage_request(
    heading: str, markdown: str, concepts: list[tuple[str, str, list[tuple[str, SourceRef]]]]
) -> str:
    """`concepts`: (concept id, name, [(evidence id, source)])."""
    parts = []
    for cid, name, found in concepts:
        lines = [f"[{cid}] {name}"]
        for eid, ref in found:
            lines.append(f"  [{eid}] {ref.describe()}\n  {excerpt(ref.body, name, EVIDENCE_WORDS)}")
        if not found:
            lines.append("  (no lecture excerpts found)")
        parts.append("\n".join(lines))
    concepts_text = "\n\n".join(parts)
    return f"Tutorial section: {heading}\n{_clip(markdown)}\n\nConcepts:\n\n{concepts_text}"


def evidence_dict(ref: SourceRef) -> dict:
    return {
        "chunk_id": str(ref.chunk_id),
        "source_type": ref.source_type,
        "label": ref.label,
        "href": ref.href,
        "week": ref.week,
        "title": ref.title or ref.file_title,
    }


def read_coverage(
    response: dict,
    concepts: list[tuple[str, str, list[tuple[str, SourceRef]]]],
) -> list[dict]:
    """Stored concept entries, in the order the concepts were found. A concept the model
    left out counts as not covered, with no summary."""
    answers = {}
    for item in response.get("concepts") or []:
        answers.setdefault(item.get("id"), item)
    out = []
    for cid, name, found in concepts:
        a = answers.get(cid) or {}
        by_eid = dict(found)
        refs = [by_eid[e] for e in a.get("evidence") or [] if e in by_eid]
        covered = a.get("status") == "covered" and bool(refs)
        weeks = [r.week for r in refs if r.week is not None]
        out.append({
            "name": name,
            "status": "covered" if covered else "not_covered",
            "evidence": [evidence_dict(r) for r in refs] if covered else [],
            "earliest_week": min(weeks) if covered and weeks else None,
            "summary": "" if covered else (a.get("summary") or "").strip(),
        })  # fmt: skip
    return out


def section_coverage(concepts: list[dict]) -> tuple[str | None, int | None]:
    """(coverage, earliest lecture week) for a section's stored concepts."""
    if not concepts:
        return None, None
    coverage = "new_concept" if any(c["status"] != "covered" for c in concepts) else "application"
    weeks = [c["earliest_week"] for c in concepts if c.get("earliest_week") is not None]
    return coverage, (min(weeks) if weeks else None)


def _concepts(sections: list) -> list[dict]:
    return [c for s in sections for c in ((s["concepts"] if isinstance(s, dict) else s.concepts)
                                         or [])]  # fmt: skip


def practised_weeks(sections: list) -> list[int]:
    """Lecture weeks a tutorial practises: weeks that explain at least two of its
    distinct covered concepts, and at least half as many as the week that explains the
    most (a diagram type a later deck happens to show isn't what the sheet practises).
    A sheet whose concepts are mostly not lectured practises none."""
    weeks_of: dict[str, set[int]] = {}
    for c in _concepts(sections):
        if c.get("status") == "covered":
            name = c["name"].strip().lower()
            weeks_of.setdefault(name, set()).update(
                e["week"] for e in c.get("evidence") or [] if e.get("week") is not None
            )
    if not weeks_of:
        return []
    per_week: dict[int, int] = {}
    for weeks in weeks_of.values():
        for w in weeks:
            per_week[w] = per_week.get(w, 0) + 1
    top = max(per_week.values(), default=0)
    return sorted(w for w, n in per_week.items() if n >= 2 and n * 2 >= top)


def mostly_new(sections: list) -> bool:
    """Under half of a sheet's distinct concepts are explained in the lectures."""
    status: dict[str, bool] = {}
    for c in _concepts(sections):
        name = c["name"].strip().lower()
        status[name] = status.get(name, False) or c.get("status") == "covered"
    return len(status) >= 4 and sum(status.values()) * 2 < len(status)
