"""Build or refresh a subject's search index (normally done after each file is processed).

    uv run python -m scripts.reindex <subject>     # subject code, name or id

Only chunks whose text changed are embedded, and embeddings are cached by text, so
re-running on unchanged material costs nothing.
"""

import argparse
import uuid

from sqlalchemy import func, or_, select

from app.db.models import File, Subject
from app.db.session import _session_factory
from app.jobs.worker import get_queue


def find_subject(db, ref: str) -> Subject:
    """By id, else by code or part of the name. Several matches (e.g. an empty duplicate
    subject): the one with the most files."""
    try:
        return db.get(Subject, uuid.UUID(ref)) or _no_match(ref)
    except ValueError:
        pass
    matches = db.execute(
        select(Subject, func.count(File.id))
        .outerjoin(File, File.subject_id == Subject.id)
        .where(or_(Subject.code.ilike(ref), Subject.name.ilike(f"%{ref}%")))
        .group_by(Subject.id)
        .order_by(func.count(File.id).desc(), Subject.created_at.desc())
    ).all()
    if not matches:
        _no_match(ref)
    if len(matches) > 1:
        print(f"{len(matches)} subjects match {ref!r}; using {matches[0][0].id} "
              f"({matches[0][1]} files)")  # fmt: skip
    return matches[0][0]


def _no_match(ref: str):
    raise SystemExit(f"No subject matches {ref!r}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("subject")
    args = parser.parse_args()
    with _session_factory()() as db:
        subject = find_subject(db, args.subject)
        names = {
            f.id: f.filename for f in db.scalars(select(File).where(File.subject_id == subject.id))
        }
    indexer = get_queue().indexer
    results = indexer.index_subject(subject.id)
    total = cost = 0.0
    for file_id, r in results.items():
        if r.chunks or r.removed:
            print(
                f"{names[file_id]}: {r.summary}" + (f", {r.removed} removed" if r.removed else "")
            )
        total += r.chunks
        cost += r.cost_usd
    print(f"{subject.code or subject.name}: {int(total)} chunks · this run ${cost:.4f}")


if __name__ == "__main__":
    main()
