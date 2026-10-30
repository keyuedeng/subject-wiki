"""Transcript pairing: by lecture number, by week (the week's only deck and transcript),
and text-similarity suggestions to confirm when a week has several decks."""

from sqlalchemy import select

from app.classify.types import FileType
from app.db.models import PairingSuggestion
from app.transcripts.pairing import (
    decks_for,
    refresh_suggestions,
    suggestions_by_subject,
    sync_auto_pairs,
)
from app.transcripts.suggest import suggest_deck
from tests.search_fixtures import _file, add_deck

AUTH = [("Authentication", "passwords hashing salts multi factor authentication tokens"),
        ("Password storage", "bcrypt hashing salted password database breach")]  # fmt: skip
NETWORK = [("Firewalls", "firewall packet filtering ports network perimeter"),
           ("Intrusion detection", "intrusion signatures network traffic alerts")]  # fmt: skip


def vtt(text: str) -> bytes:
    return f"WEBVTT\n\n00:00:00.000 --> 00:00:30.000\n{text}\n".encode()


def add_vtt(db, storage, subject, name, week, lecture=None, said="", status="failed"):
    f = _file(db, subject, name, FileType.TRANSCRIPT, week, lecture)
    f.status = status
    f.status_detail = (
        "Pair this transcript with its slides first (no deck has the same lecture number)"
    )
    storage.put(f.storage_path, vtt(said), "text/vtt")
    db.flush()
    return f


def paired(db, t):
    return [f.filename for f, _ in decks_for(db, t.id)]


def test_week_with_one_deck_and_one_transcript_pairs_without_lecture_numbers(db, storage,
                                                                             subject):  # fmt: skip
    deck = add_deck(db, subject, "Wk 1 B - Info Sec.pdf", 1, 1, AUTH)  # numbered deck
    t1 = add_vtt(db, storage, subject, "F-transcript.vtt", 1)  # unnumbered transcript
    deck2 = add_deck(db, subject, "Wk 2 - Authentication.pdf", 2, None, AUTH)
    t2 = add_vtt(db, storage, subject, "F-transcript (1).vtt", 2)
    db.commit()
    sync_auto_pairs(db, subject.id)
    assert paired(db, t1) == [deck.filename] and paired(db, t2) == [deck2.filename]
    # The "pair it first" failure is cleared: ready to process.
    assert (t1.status, t1.status_detail) == ("uploaded", None)


def test_conflicting_numbers_or_a_second_transcript_dont_pair_by_week(db, storage, subject):
    add_deck(db, subject, "L5.pdf", 3, 5, AUTH)
    t = add_vtt(db, storage, subject, "L6.vtt", 3, lecture=6)
    add_deck(db, subject, "W4.pdf", 4, None, AUTH)
    a = add_vtt(db, storage, subject, "W4 a.vtt", 4)
    b = add_vtt(db, storage, subject, "W4 b.vtt", 4)
    db.commit()
    sync_auto_pairs(db, subject.id)
    assert paired(db, t) == paired(db, a) == paired(db, b) == []
    assert t.status == "failed"


def test_several_decks_get_a_suggestion_by_text_to_confirm(db, storage, subject):
    auth = add_deck(db, subject, "Wk 5 Authentication.pdf", 5, None, AUTH)
    add_deck(db, subject, "Wk 5 Networks.pdf", 5, None, NETWORK)
    t = add_vtt(db, storage, subject, "F-transcript.vtt", 5,
                said="So today: passwords. We store a salted hash, ideally bcrypt, and add "
                     "multi factor authentication so a breach of the password database "
                     "doesn't give everything away.")  # fmt: skip
    db.commit()
    sync_auto_pairs(db, subject.id)
    assert paired(db, t) == []  # never used until confirmed
    refresh_suggestions(db, storage, subject.id)
    s = suggestions_by_subject(db, subject.id)[t.id]
    assert s.slides_file_id == auth.id and s.score > s.runner_up
    # Stored and reused: no second read of the transcript while the decks are the same.
    storage.objects.pop(t.storage_path)
    refresh_suggestions(db, storage, subject.id)
    assert suggestions_by_subject(db, subject.id)[t.id].slides_file_id == auth.id
    # A new deck in the week makes it stale (hidden until recomputed).
    add_deck(db, subject, "Wk 5 Extra.pdf", 5, None, NETWORK)
    db.commit()
    assert t.id not in suggestions_by_subject(db, subject.id)


def test_suggestion_goes_once_paired(db, storage, subject):
    auth = add_deck(db, subject, "A.pdf", 6, None, AUTH)
    add_deck(db, subject, "B.pdf", 6, None, NETWORK)
    t = add_vtt(db, storage, subject, "t.vtt", 6, said="salted password hashing")
    db.commit()
    refresh_suggestions(db, storage, subject.id)
    from app.transcripts.pairing import set_manual

    set_manual(db, t.id, [auth.id])
    refresh_suggestions(db, storage, subject.id)
    assert db.scalars(select(PairingSuggestion)).all() == []
    assert t.status == "uploaded"  # confirming also clears the "pair it first" failure


def test_suggest_deck_downweights_words_on_every_slide():
    decks = {"auth": ["INFO30006 passwords hashing", "INFO30006 salts"],
             "net": ["INFO30006 firewalls", "INFO30006 ports"]}  # fmt: skip
    slides = [x for v in decks.values() for x in v]
    s = suggest_deck("INFO30006 INFO30006 INFO30006 hashing", decks, slides)
    assert s.deck == "auth" and s.runner_up < s.score
    assert suggest_deck("", decks, slides) is None


def test_board_offers_the_suggestion_and_confirming_pairs_and_processes(db, storage, subject,
                                                                       client):  # fmt: skip
    from app.jobs.worker import get_queue
    from app.main import app

    queued = []

    class Queue:
        def enqueue(self, db, files):
            queued.extend(f.id for f in files)
            return files

    app.dependency_overrides[get_queue] = lambda: Queue()
    auth = add_deck(db, subject, "Wk 5 Authentication.pdf", 5, None, AUTH)
    add_deck(db, subject, "Wk 5 Networks.pdf", 5, None, NETWORK)
    t = add_vtt(db, storage, subject, "F-transcript.vtt", 5, said="salted bcrypt password hashing")
    db.commit()
    board = client.get(f"/subjects/{subject.id}/view/board").text
    week5 = board.split(">Week 5<", 1)[1].split(">Week 6<", 1)[0]
    assert "Pairing needed" in week5 and "Suggested: Authentication" in week5
    assert f'hx-post="/files/{t.id}/pairing/confirm?view=board"' in week5
    r = client.post(f"/files/{t.id}/pairing/confirm?view=board", data={"slides_id": str(auth.id)})
    assert r.status_code == 200 and queued == [t.id]
    db.expire_all()
    assert [(f.id, src) for f, src in decks_for(db, t.id)] == [(auth.id, "manual")]
