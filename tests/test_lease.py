"""Job leases: a file is worked on by one process at a time, and a restart only picks up
jobs whose process has stopped (the duplicate re-clean of INFO30006 week 4)."""

from datetime import UTC, datetime, timedelta

import pytest

from app.db.models import File
from app.jobs.lease import LEASE_TTL, Leases
from app.jobs.worker import ParseQueue
from app.parse.pipeline import Parser
from app.services.uploads import ingest_upload
from tests.slide_fixtures import make_deck
from tests.test_pipeline import FakeVision
from tests.test_transcript_pipeline import InlineExecutor


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 30, 3, 0, tzinfo=UTC)

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


def leases(session_factory, clock, owner):
    return Leases(session_factory, owner=owner, clock=clock, heartbeat=False)


@pytest.fixture
def deck(db, storage, subject):
    pages = [{"title": "Creator", "lines": ["• creates objects"]}]
    return ingest_upload(db, storage, subject, "Week 4 Lecture 4.pdf", None, make_deck(pages),
                         max_bytes=50_000_000).file  # fmt: skip


def lease_of(session_factory, file_id):
    with session_factory() as db:
        f = db.get(File, file_id)
        return f.lease_owner, f.lease_until, f.status


def test_one_owner_at_a_time_until_released_or_expired(session_factory, clock, deck):
    a, b = leases(session_factory, clock, "a"), leases(session_factory, clock, "b")
    assert a.claim(deck.id) and a.claim(deck.id)  # re-claiming your own lease renews it
    assert not b.claim(deck.id)
    a.release(deck.id)
    assert b.claim(deck.id)
    clock.now += LEASE_TTL - timedelta(seconds=1)
    assert not a.claim(deck.id)  # still live
    b.renew()  # heartbeat: b is alive, so its lease keeps moving
    clock.now += timedelta(seconds=30)
    assert not a.claim(deck.id)
    clock.now += LEASE_TTL  # b stopped renewing (its process died)
    assert a.claim(deck.id)


def test_restart_skips_a_job_another_process_is_running(session_factory, storage, clock, deck):
    script = leases(session_factory, clock, "script")
    server = leases(session_factory, clock, "server")
    with session_factory() as db:  # the script marks the file and starts on it
        db.get(File, deck.id).status = "parsing"
        db.commit()
    assert script.claim(deck.id)

    vision = FakeVision()
    queue = ParseQueue(Parser(session_factory, storage, lambda: vision), executor=InlineExecutor(),
                       leases=server)  # fmt: skip
    assert queue.resume() == 0  # the dev server restarts: the live lease is left alone
    assert lease_of(session_factory, deck.id)[2] == "parsing"
    # Even a direct call (as a script makes) is skipped while someone else holds it.
    queue.parser.parse(deck.id)
    assert lease_of(session_factory, deck.id)[:1] == ("script",)
    assert lease_of(session_factory, deck.id)[2] == "parsing"

    clock.now += LEASE_TTL + timedelta(seconds=1)  # the script died mid-job
    assert queue.reap() == 1  # now it's picked up, once
    owner, until, status = lease_of(session_factory, deck.id)
    assert (owner, until, status) == (None, None, "parsed")  # released when done
    assert queue.reap() == 0


def test_enqueue_leaves_files_another_process_holds(db, session_factory, storage, clock, deck):
    other = leases(session_factory, clock, "other")
    assert other.claim(deck.id)
    queue = ParseQueue(Parser(session_factory, storage, FakeVision), executor=InlineExecutor(),
                       leases=leases(session_factory, clock, "me"))  # fmt: skip
    assert queue.enqueue(db, [deck]) == []
    db.refresh(deck)
    assert deck.status == "uploaded"
    other.release(deck.id)
    assert [f.id for f in queue.enqueue(db, [deck])] == [deck.id]
    db.refresh(deck)
    assert (deck.status, deck.lease_owner) == ("parsed", None)
