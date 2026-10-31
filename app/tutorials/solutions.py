"""Link tutorial exercises to their solutions.

Files pair as they do on the board (`group_tutorials`, within a week: a shared filename
number, else the week's only tutorial). Within a pair, an exercise links to the solution
section with the same exercise number, else the same heading. Linked exercises get
`solution_source = "official"`; the rest "none", unless a solution was generated
(kept). Runs after any tutorial or solutions sheet is processed, and when the board
is drawn, since moving a file changes the pairs.
"""

import re
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.classify.types import FileType
from app.db.models import File, TutorialSection
from app.services.board import group_tutorials


def _norm(title: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", title.lower()))


def link_solutions(db: Session, subject_id: uuid.UUID) -> None:
    """Doesn't commit."""
    files = list(db.scalars(select(File).where(
        File.subject_id == subject_id,
        File.type.in_([FileType.TUTORIAL.value, FileType.SOLUTIONS.value]))))  # fmt: skip
    sections: dict[uuid.UUID, list[TutorialSection]] = {}
    for s in db.scalars(select(TutorialSection).where(
            TutorialSection.file_id.in_([f.id for f in files]))):  # fmt: skip
        sections.setdefault(s.file_id, []).append(s)

    answer_for: dict[uuid.UUID, uuid.UUID] = {}  # exercise section -> solution section
    by_week: dict[int, list[File]] = {}
    for f in files:
        if f.week is not None:
            by_week.setdefault(f.week, []).append(f)
    for week_files in by_week.values():
        for group in group_tutorials(week_files):
            answers = [s for sol in group.solutions for s in sections.get(sol.id, [])]
            if not answers:
                continue
            by_number: dict[int, TutorialSection] = {}
            for s in sorted(answers, key=lambda s: (str(s.file_id), s.idx)):
                if s.number is not None:
                    by_number.setdefault(s.number, s)  # a long answer's first part
            by_title = {_norm(s.title): s for s in answers}
            for tute in group.tutorials:
                for s in sections.get(tute.id, []):
                    if not s.is_exercise:
                        continue
                    match = by_number.get(s.number) if s.number is not None else None
                    match = match or by_title.get(_norm(s.title))
                    if match is not None:
                        answer_for[s.id] = match.id

    for f in files:
        if f.type != FileType.TUTORIAL.value:
            continue
        for s in sections.get(f.id, []):
            if s.id in answer_for:
                s.solution_section_id, s.solution_source = answer_for[s.id], "official"
            elif s.solution_source != "generated":
                s.solution_section_id, s.solution_source = None, "none"
