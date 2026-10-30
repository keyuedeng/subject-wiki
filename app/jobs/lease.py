"""Leases on files being processed, so two processes never work on the same file at once.

A process claims a file by writing its owner id and an expiry (`files.lease_owner`,
`files.lease_until`) in one conditional UPDATE: it succeeds only if the file has no lease,
the lease has expired, or the process already holds it. While the process is alive, a
daemon thread renews all its leases every RENEW_EVERY seconds, so a lease only expires
when its process has stopped (crashed, killed, or reloaded by `fastapi dev`).

Queuing a file claims it (so queued files are protected too), every job claims again
when it starts (scripts that call `Parser.parse` / `TranscriptProcessor.process`
directly are covered), and a job releases the lease when it ends. Restarts re-queue only
files whose lease has expired (`ParseQueue.reap`).

Times are UTC from the processes' own clocks: fine for processes on one machine or with
synced clocks; the TTL leaves a minute of slack.
"""

import logging
import os
import socket
import threading
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, update
from sqlalchemy.orm import Session

from app.db.models import File

logger = logging.getLogger(__name__)

LEASE_TTL = timedelta(seconds=90)
RENEW_EVERY = 30.0  # seconds


def new_owner() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"[:80]


class Leases:
    def __init__(
        self,
        session_factory: Callable[[], Session],
        owner: str | None = None,
        ttl: timedelta = LEASE_TTL,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        heartbeat: bool = True,  # off in tests, which call renew() themselves
    ):
        self.session_factory = session_factory
        self.owner = owner or new_owner()
        self.ttl = ttl
        self.clock = clock
        self.heartbeat = heartbeat
        self._on_tick: list[Callable[[], None]] = []
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()

    def claim(self, file_id: uuid.UUID) -> bool:
        """Take or renew the lease on a file; False if another live process holds it."""
        now = self.clock()
        with self.session_factory() as db:
            result = db.execute(
                update(File)
                .where(
                    File.id == file_id,
                    or_(
                        File.lease_until.is_(None),
                        File.lease_until < now,
                        File.lease_owner == self.owner,
                    ),
                )
                .values(lease_owner=self.owner, lease_until=now + self.ttl)
                .execution_options(synchronize_session=False)
            )
            db.commit()
        claimed = result.rowcount == 1
        if claimed:
            self.start()
        return claimed

    def release(self, file_id: uuid.UUID) -> None:
        with self.session_factory() as db:
            db.execute(
                update(File)
                .where(File.id == file_id, File.lease_owner == self.owner)
                .values(lease_owner=None, lease_until=None)
                .execution_options(synchronize_session=False)
            )
            db.commit()

    def renew(self) -> int:
        """Extend every lease this process holds. Returns how many."""
        with self.session_factory() as db:
            result = db.execute(
                update(File)
                .where(File.lease_owner == self.owner)
                .values(lease_until=self.clock() + self.ttl)
                .execution_options(synchronize_session=False)
            )
            db.commit()
        return result.rowcount

    @contextmanager
    def hold(self, file_id: uuid.UUID) -> Iterator[bool]:
        """Claim for the duration of a job. Yields whether this process got it."""
        if not self.claim(file_id):
            yield False
            return
        try:
            yield True
        finally:
            try:
                self.release(file_id)
            except Exception:  # the lease expires on its own
                logger.exception("couldn't release the lease on file %s", file_id)

    # Heartbeat

    def on_tick(self, fn: Callable[[], None]) -> None:
        """Also run `fn` on every heartbeat (the queue re-queues orphaned files)."""
        self._on_tick.append(fn)
        self.start()

    def start(self) -> None:
        if not self.heartbeat:
            return
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._stop.clear()
                self._thread = threading.Thread(target=self._run, name="lease", daemon=True)
                self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(RENEW_EVERY):
            try:
                self.renew()
                for fn in self._on_tick:
                    fn()
            except Exception:
                logger.exception("lease heartbeat failed")


@contextmanager
def held(leases: Leases | None, file_id: uuid.UUID, what: str) -> Iterator[bool]:
    """`Leases.hold`, or always True when leasing is off (tests that don't need it)."""
    if leases is None:
        yield True
        return
    with leases.hold(file_id) as ok:
        if not ok:
            logger.info("skipping %s of file %s: another process is working on it", what,
                        file_id)  # fmt: skip
        yield ok
