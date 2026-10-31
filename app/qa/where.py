""" "Where is X taught?" questions: detection, the coverage prompt and grouping by week.

Instead of explaining X, these return every source that covers it, labelled main
coverage or mentioned, grouped by week. Detection is by pattern (below); the Ask page can
also force this mode. Grouping is pure: neighbouring slides or transcript moments from
one file at the same level collapse into one item ("slides 12–16", "23:40–26:10").
"""

import re
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, create_model

from app.qa.labels import SourceRef, span_label, span_short

WHERE_PROMPT_VERSION = "where-v3"
MAX_OUTPUT_TOKENS = 4000
EXCERPT_WORDS = 80

WHERE_PROMPT = """\
A student wants to know where a topic is taught in their subject. You get the topic and \
numbered excerpts from the subject's slides, lecturer notes, lecture transcripts and \
tutorial sheets. \
For each excerpt that involves the topic, give its level:
- "main": the topic is what the excerpt is about, or the lecturer spends real time on it \
(defines it, explains it, works through an example of it).
- "mentioned": the topic is only referred to in passing (named in a list, contrasted \
with, recalled briefly).
Leave out excerpts that don't involve the topic, including ones that only share a word \
with it or are about a different concept with a similar name. Judge only from the \
excerpt text. List each excerpt at most once.
Also give:
- summary: one sentence for the student naming where the topic is mainly taught, using \
the week and slide numbers or lecture times from the excerpt headers (e.g. "Information \
Expert is taught in Week 4, mostly on slides 18–22, with a worked example in the \
lecture."). Empty if no excerpt involves the topic.
- follow_ups: two short questions the student might ask next, e.g. "Explain Information \
Expert" or where another closely related topic is taught.
"""

Level = Literal["main", "mentioned"]

_TOPIC = r"(?P<x>.+?)"
_END = r"\s*[?.!]*\s*$"
WHERE_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in [
        # where is/was/are X taught/covered/... ; in which week was X covered
        r"^(?:where|in which (?:weeks?|lectures?|slides?|tutorials?|workshops?))\s+"
        r"(?:is|are|was|were|did we|do we|"
        r"have we|does the (?:course|subject|lecturer))\s+" + _TOPIC + r"\s+(?:first\s+)?"
        r"(?:taught|covered|explained|introduced|discussed|mentioned|defined|presented|"
        r"talked about|learn(?:ed|t)?|cover(?:ed)?|teach|explain|introduce|discuss)"
        r"(?:\s+(?:in (?:the )?(?:course|subject|lectures?|slides?)))?" + _END,
        # which week covers X ; which lecture was about X
        r"^(?:which|what)\s+(?:weeks?|lectures?|slides?|tutorials?|workshops?|"
        r"parts? of the (?:course|subject))\s+"
        r"(?:covers?|covered|teach(?:es)?|taught|discuss(?:es|ed)?|introduces?|introduced|"
        r"explains?|explained|talks? about|talked about|mentions?|mentioned|"
        r"(?:is|was|are|were) (?:about|on)|did we (?:do|cover|learn|see))\s+" + _TOPIC + _END,
        # which week is X in ; what lecture was X covered
        r"^(?:which|what)\s+(?:weeks?|lectures?)\s+(?:is|was|are|were)\s+"
        + _TOPIC
        + r"\s+(?:in|on|taught|covered|introduced|explained)"
        + _END,
        # when did we do/learn/cover X
        r"^when\s+(?:did|do|will|have)\s+(?:we|they|you|the lecturer)\s+(?:first\s+)?"
        r"(?:do|learn|cover|study|go over|talk about|discuss|see|get to|introduce|"
        r"look at)\s+" + _TOPIC + _END,
        # where can I find X ; where do we learn about X
        r"^where\s+(?:can|do|should|would)\s+(?:i|we)\s+(?:find|learn about|read about|"
        r"revise|review|study)\s+" + _TOPIC + r"(?:\s+in (?:the )?(?:course|subject|"
        r"material|lectures?|slides?))?" + _END,
    ]
]
_LEADING = re.compile(r"^(?:the|a|an|about)\s+", re.IGNORECASE)


def detect_where(question: str) -> str | None:
    """The topic X if the question asks where/when X is taught, else None."""
    q = " ".join(question.split())
    for pattern in WHERE_PATTERNS:
        if m := pattern.match(q):
            topic = _LEADING.sub("", m.group("x").strip(" \"'“”‘’")).strip()
            if topic:
                return topic
    return None


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


def where_schema(ids: list[str]) -> type[BaseModel]:
    Handle = Literal[tuple(ids)]  # type: ignore[valid-type]
    item = create_model("Coverage", __base__=_Strict, id=(Handle, ...), level=(Level, ...))
    return create_model(
        "WhereCoverage",
        __base__=_Strict,
        summary=(str, Field(description="One sentence on where the topic is mainly taught.")),
        items=(list[item], Field(description="Excerpts that involve the topic, with a level.")),
        follow_ups=(list[str], Field(description="Two short follow-up questions.")),
    )


def excerpt(body: str, topic: str, words: int = EXCERPT_WORDS) -> str:
    """About `words` words of the body, centred on the first mention of the topic."""
    tokens = body.split()
    if len(tokens) <= words:
        return " ".join(tokens)
    keys = [w.lower() for w in re.findall(r"\w+", topic) if len(w) > 3] or [topic.lower()]
    hit = next(
        (i for i, t in enumerate(tokens) if any(t.lower().startswith(k[:6]) for k in keys)), 0
    )
    start = max(0, min(hit - words // 3, len(tokens) - words))
    text = " ".join(tokens[start : start + words])
    return ("… " if start else "") + text + (" …" if start + words < len(tokens) else "")


def request_text(topic: str, sources: list[tuple[str, SourceRef]]) -> str:
    blocks = "\n\n".join(
        f"[{h}] {ref.describe()}\n{excerpt(ref.body, topic)}" for h, ref in sources
    )
    return f"Topic: {topic.strip()}\n\nExcerpts:\n\n{blocks}"


@dataclass
class WhereItem:
    level: Level
    refs: list[SourceRef]  # one source, or a run of neighbouring ones

    @property
    def first(self) -> SourceRef:
        return self.refs[0]

    @property
    def label(self) -> str:
        return span_label(self.refs[0], self.refs[-1])

    @property
    def href(self) -> str:
        return self.first.href

    @property
    def short_label(self) -> str:
        return span_short(self.refs[0], self.refs[-1])

    @property
    def description(self) -> str:
        """Slide titles the span runs over ("Creator … Creator: Contraindications"); for a
        lecture moment, the slide it was matched to."""
        titles = list(dict.fromkeys(r.title for r in self.refs if r.title))
        if not titles:
            return (
                "Not matched to a slide"
                if self.first.source_type == "transcript"
                else self.first.kind
            )
        text = titles[0] if len(titles) == 1 else f"{titles[0]} … {titles[-1]}"
        return f"On slide: {text}" if self.first.source_type == "transcript" else text

    @property
    def flags(self) -> list[str]:
        return list(dict.fromkeys(f for r in self.refs for f in r.flags))


@dataclass
class WhereWeek:
    week: int | None
    items: list[WhereItem] = field(default_factory=list)

    @property
    def main(self) -> int:
        return sum(i.level == "main" for i in self.items)

    @property
    def main_items(self) -> list[WhereItem]:
        return [i for i in self.items if i.level == "main"]

    @property
    def mentioned_items(self) -> list[WhereItem]:
        return [i for i in self.items if i.level == "mentioned"]

    @property
    def deck_titles(self) -> list[str]:
        """Titles of the lecture decks this week's items come from, for the heading."""
        titles = [r.file_title for i in self.items for r in i.refs if r.source_type == "slide"]
        return list(dict.fromkeys(titles))


KIND_ORDER = {"slide": 0, "notes": 1, "transcript": 2, "tutorial": 3}
# Neighbours: slide pages up to 2 apart (collapsed build steps leave gaps), consecutive
# transcript segments.
MAX_GAP = {"slide": 2, "notes": 2, "transcript": 1}


def group_by_week(found: list[tuple[SourceRef, Level]]) -> list[WhereWeek]:
    """Order by week (no week last), then kind, file, position; collapse runs."""
    ordered = sorted(
        found,
        key=lambda x: (
            x[0].week is None, x[0].week or 0, KIND_ORDER.get(x[0].source_type, 9),
            x[0].lecture or 0, x[0].file_title, str(x[0].file_id), x[0].position,
        ),
    )  # fmt: skip
    weeks: list[WhereWeek] = []
    for ref, level in ordered:
        if not weeks or weeks[-1].week != ref.week:
            weeks.append(WhereWeek(ref.week))
        items = weeks[-1].items
        last = items[-1] if items else None
        prev = last.refs[-1] if last else None
        if (
            last is not None and prev is not None and last.level == level
            and prev.file_id == ref.file_id and prev.source_type == ref.source_type
            and 0 < ref.position - prev.position <= MAX_GAP.get(ref.source_type, 0)
        ):  # fmt: skip
            last.refs.append(ref)
        else:
            items.append(WhereItem(level, [ref]))
    return weeks


def week_ranges(weeks: list[int]) -> str:
    """[1, 2, 3, 5, 8, 9] -> "1–3, 5, 8–9"."""
    out: list[str] = []
    for w in sorted(set(weeks)):
        if out and int(out[-1].split("–")[-1]) == w - 1:
            out[-1] = out[-1].split("–")[0] + f"–{w}"
        else:
            out.append(str(w))
    return ", ".join(out)


LECTURE_TYPES = {"slide", "notes", "transcript"}


def introduced_in_tutorial(weeks: list[WhereWeek]) -> bool:
    """The topic turns up only in tutorial sheets, never in the lectures."""
    types = {r.source_type for w in weeks for i in w.items for r in i.refs}
    return bool(types) and not types & LECTURE_TYPES


def tutorial_summary(weeks: list[WhereWeek], topic: str) -> str:
    """What the tutorial itself says the topic is (its labelling's summary for a concept
    the lectures don't cover), when one of the found sections has it."""
    want = set(re.findall(r"[a-z0-9]+", topic.lower()))
    for w in weeks:
        for item in w.items:
            for ref in item.refs:
                for c in ref.meta.get("new_concepts") or []:
                    name = set(re.findall(r"[a-z0-9]+", c["name"].lower()))
                    if c.get("summary") and want and (want <= name or name <= want):
                        return c["summary"]
    return ""
