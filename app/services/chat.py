"""Ask conversations: chats, their messages, and the history a follow-up is asked with.

An answer is stored as a snapshot of what was shown: its sources (labels, links, a short
excerpt), citations, follow-ups and coverage list. Rendering an old chat reads only the
snapshot, so it looks the same after files are re-indexed, renamed or deleted.
"""

import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import Chat, ChatMessage, Chunk, Subject
from app.qa import where as wq
from app.qa.answer import Turn
from app.qa.labels import SourceRef
from app.search.backend import Filters
from app.search.search import Hit
from app.services.ask import Answer, Source, WhereAnswer

TITLE_CHARS = 60
SNAPSHOT_BODY_CHARS = 700  # per source: enough for the source list's excerpt


def chat_title(question: str) -> str:
    """The first question, cut at a word boundary."""
    text = " ".join(question.split())
    if len(text) <= TITLE_CHARS:
        return text
    return text[:TITLE_CHARS].rsplit(" ", 1)[0].rstrip(",;:") + "…"


# Snapshots


def ref_to_dict(ref: SourceRef) -> dict:
    d = asdict(ref)
    d["chunk_id"], d["file_id"] = str(ref.chunk_id), str(ref.file_id)
    d["body"] = ref.body[:SNAPSHOT_BODY_CHARS]
    return d


def ref_from_dict(d: dict) -> SourceRef:
    return SourceRef(**{**d, "chunk_id": uuid.UUID(d["chunk_id"]),
                        "file_id": uuid.UUID(d["file_id"])})  # fmt: skip


def _hit_to_dict(hit: Hit | None) -> dict | None:
    if hit is None:
        return None
    return {**asdict(hit), "chunk_id": str(hit.chunk_id)}


def _hit_from_dict(d: dict | None) -> Hit | None:
    return Hit(**{**d, "chunk_id": uuid.UUID(d["chunk_id"])}) if d else None


def answer_payload(a: Answer) -> dict:
    return {
        "status": a.status,
        "missing": a.missing,
        "warnings": a.warnings,
        "topic": a.topic,
        "follow_ups": a.follow_ups,
        "model_called": a.model_called,
        "handles": {h: str(ref.chunk_id) for h, ref in a.handles.items()},
        "cited": [str(r.chunk_id) for r in a.cited],
        "sources": [{"ref": ref_to_dict(s.ref), "hit": _hit_to_dict(s.hit)} for s in a.sources],
    }


def where_payload(w: WhereAnswer) -> dict:
    return {
        "topic": w.topic,
        "summary": w.summary,
        "follow_ups": w.follow_ups,
        "missing_note": w.missing_note,
        "introduced_in_tutorial": w.introduced_in_tutorial,
        "tutorial_summary": w.tutorial_summary,
        "model_called": w.model_called,
        "candidates": len(w.sources),
        "weeks": [
            {"week": wk.week,
             "items": [{"level": i.level, "refs": [ref_to_dict(r) for r in i.refs]}
                       for i in wk.items]}
            for wk in w.weeks
        ],
    }  # fmt: skip


def answer_from(msg: ChatMessage, question: str) -> Answer:
    p = msg.payload
    sources = [Source(ref_from_dict(s["ref"]), _hit_from_dict(s.get("hit")))
               for s in p.get("sources", [])]  # fmt: skip
    by_id = {str(s.ref.chunk_id): s.ref for s in sources}
    handles = {h: by_id[c] for h, c in p.get("handles", {}).items() if c in by_id}
    return Answer(
        question, p.get("status", "answered"), msg.content, handles,
        [by_id[c] for c in p.get("cited", []) if c in by_id], sources, p.get("missing"),
        p.get("warnings", []), msg.cost_usd, msg.cached, p.get("model_called", True),
        p.get("topic", ""), p.get("follow_ups", []),
    )  # fmt: skip


def where_from(msg: ChatMessage, question: str) -> WhereAnswer:
    p = msg.payload
    weeks = [
        wq.WhereWeek(wk["week"], [wq.WhereItem(i["level"], [ref_from_dict(r) for r in i["refs"]])
                                  for i in wk["items"]])
        for wk in p.get("weeks", [])
    ]  # fmt: skip
    return WhereAnswer(
        question, p.get("topic", ""), weeks, [], p.get("missing_note"), msg.cost_usd,
        msg.cached, p.get("model_called", True), p.get("summary", ""), p.get("follow_ups", []),
        candidates=p.get("candidates", 0),
        introduced_in_tutorial=p.get("introduced_in_tutorial", False),
        tutorial_summary=p.get("tutorial_summary", ""),
    )  # fmt: skip


# Chats


@dataclass
class Exchange:
    """A question and what it got back, ready to render."""

    question: ChatMessage
    reply: Answer | WhereAnswer | None  # None: no reply (failed)
    error: str | None = None


def new_chat(db: Session, subject: Subject, question: str) -> Chat:
    chat = Chat(subject_id=subject.id, title=chat_title(question))
    db.add(chat)
    db.flush()
    return chat


def add_question(
    db: Session, chat: Chat, question: str, mode: str, filters: Filters
) -> ChatMessage:
    msg = ChatMessage(
        chat_id=chat.id, idx=_next_idx(db, chat), role="user", kind="question",
        content=question.strip(), mode=mode,
        filters={"weeks": list(filters.weeks), "types": list(filters.types)},
    )  # fmt: skip
    db.add(msg)
    chat.updated_at = func.now()
    db.flush()
    return msg


def add_reply(db: Session, chat: Chat, reply: Answer | WhereAnswer) -> ChatMessage:
    if isinstance(reply, WhereAnswer):
        kind, content, payload = "where", reply.summary, where_payload(reply)
    else:
        kind, content, payload = "answer", reply.answer_markdown, answer_payload(reply)
    msg = ChatMessage(
        chat_id=chat.id, idx=_next_idx(db, chat), role="assistant", kind=kind, content=content,
        payload=payload, cost_usd=reply.cost_usd, cached=reply.cached,
    )  # fmt: skip
    db.add(msg)
    chat.updated_at = func.now()
    db.flush()
    return msg


def add_error(db: Session, chat: Chat, message: str) -> ChatMessage:
    msg = ChatMessage(chat_id=chat.id, idx=_next_idx(db, chat), role="assistant", kind="error",
                      content=message)  # fmt: skip
    db.add(msg)
    db.flush()
    return msg


def _next_idx(db: Session, chat: Chat) -> int:
    last = db.scalar(select(func.max(ChatMessage.idx)).where(ChatMessage.chat_id == chat.id))
    return 0 if last is None else last + 1


def exchanges(chat: Chat) -> list[Exchange]:
    out: list[Exchange] = []
    for msg in chat.messages:
        if msg.role == "user":
            out.append(Exchange(msg, None))
        elif out and out[-1].reply is None and out[-1].error is None:
            q = out[-1].question.content
            if msg.kind == "answer":
                out[-1].reply = answer_from(msg, q)
            elif msg.kind == "where":
                out[-1].reply = where_from(msg, q)
            else:
                out[-1].error = msg.content
    return out


def history(chat: Chat) -> list[Turn]:
    """Earlier answered questions, for a follow-up's retrieval and answer."""
    turns = []
    for ex in exchanges(chat):
        if isinstance(ex.reply, Answer):
            turns.append(Turn(ex.question.content, ex.reply.answer_markdown, ex.reply.topic))
        elif isinstance(ex.reply, WhereAnswer):
            turns.append(Turn(ex.question.content, ex.reply.summary, ex.reply.topic))
    return turns


def chat_groups(db: Session, subject: Subject, now: datetime | None = None) -> list[tuple]:
    """(heading, chats) for the sidebar, newest first: Today, Yesterday, Previous 7 days,
    Older. Days are local to the server."""
    now = (now or datetime.now().astimezone()).astimezone()
    today = now.date()
    chats = db.scalars(
        select(Chat).where(Chat.subject_id == subject.id).order_by(Chat.updated_at.desc())
    )
    groups: dict[str, list[Chat]] = {}
    for chat in chats:
        updated = chat.updated_at
        if updated.tzinfo is None:  # SQLite drops the zone; values are UTC
            updated = updated.replace(tzinfo=UTC)
        day = updated.astimezone(now.tzinfo).date()
        if day >= today:
            heading = "Today"
        elif day == today - timedelta(days=1):
            heading = "Yesterday"
        elif day > today - timedelta(days=8):
            heading = "Previous 7 days"
        else:
            heading = "Older"
        groups.setdefault(heading, []).append(chat)
    return list(groups.items())


def index_stats(db: Session, subject: Subject) -> dict:
    """What Ask searches: lecture decks (and transcripts) indexed, and chunk count."""
    rows = db.execute(
        select(Chunk.source_type, func.count(), func.count(func.distinct(Chunk.file_id)))
        .where(Chunk.subject_id == subject.id)
        .group_by(Chunk.source_type)
    ).all()
    counts = {t: (n, files) for t, n, files in rows}
    return {
        "sources": sum(n for n, _ in counts.values()),
        "lectures": counts.get("slide", (0, 0))[1],
        "transcripts": counts.get("transcript", (0, 0))[1],
        "tutorials": counts.get("tutorial", (0, 0))[1],
    }


# Titles too general to suggest ("Explain Summary").
GENERIC_TITLES = {"objectives", "learning objectives", "summary", "outline", "agenda", "overview",
                  "introduction", "questions", "references", "recap", "lecture identification",
                  "acknowledgement", "acknowledgements", "contents", "today", "admin"}  # fmt: skip


@dataclass
class Suggestion:
    text: str
    mode: str = "auto"


def suggestions(db: Session, subject: Subject, n: int = 3) -> list[Suggestion]:
    """Starter questions for an empty chat, from the indexed slides: each deck's most
    repeated slide title (a topic it spends several slides on)."""
    rows = db.execute(
        select(Chunk.file_id, Chunk.title, func.count())
        .where(Chunk.subject_id == subject.id, Chunk.source_type == "slide", Chunk.title != "")
        .group_by(Chunk.file_id, Chunk.title)
        .order_by(func.count().desc(), Chunk.title)
    ).all()
    topics: dict[uuid.UUID, str] = {}
    for file_id, title, _ in rows:
        clean = title.strip().rstrip(":")
        if file_id not in topics and clean.lower() not in GENERIC_TITLES and len(clean) <= 50:
            topics[file_id] = clean
    picked = list(topics.values())[:n]
    out = [Suggestion(f"Explain {t}") for t in picked[: n - 1]]
    if len(picked) == n:
        out.append(Suggestion(f"Where is {picked[-1]} taught?", "where"))
    return out
