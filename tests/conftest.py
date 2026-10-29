"""Tests run against in-memory SQLite and an in-memory storage fake: no network, no secrets."""

import os

# Settings are required at import time of some modules; tests never use real credentials.
os.environ.setdefault("SUPABASE_URL", "http://supabase.test")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "test-key")
os.environ.setdefault("DATABASE_URL", "sqlite://")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, event  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app.db import models  # noqa: E402,F401
from app.db.base import Base  # noqa: E402
from app.db.models import Subject  # noqa: E402
from app.db.session import get_db, get_session_factory  # noqa: E402
from app.main import app  # noqa: E402
from app.storage.client import get_storage  # noqa: E402


class FakeStorage:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.puts = 0
        self.fail_delete = False

    def put(self, path: str, data: bytes, content_type: str) -> None:
        self.puts += 1
        self.objects[path] = data

    def get(self, path: str) -> bytes:
        from app.storage.client import StorageNotFound

        if path not in self.objects:
            raise StorageNotFound(f"missing {path}")
        return self.objects[path]

    def delete(self, path: str) -> None:
        self.delete_many([path])

    def delete_many(self, paths: list[str]) -> None:
        if self.fail_delete:
            from app.storage.client import StorageError

            raise StorageError("simulated failure")
        for path in paths:
            self.objects.pop(path, None)

    def list(self, prefix: str) -> list[str]:
        folder = prefix.rstrip("/") + "/"
        return [p for p in self.objects if p.startswith(folder) and "/" not in p[len(folder) :]]


@pytest.fixture
def session_factory():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    # Enforce foreign keys (cascades, SET NULL) like Postgres does.
    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _record):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, expire_on_commit=False)
    engine.dispose()


@pytest.fixture
def db(session_factory) -> Session:
    with session_factory() as session:
        yield session


@pytest.fixture
def storage() -> FakeStorage:
    return FakeStorage()


@pytest.fixture
def subject(db) -> Subject:
    s = Subject(name="Machine Learning", code="COMP30027", semester="2026 S2", weeks=12)
    db.add(s)
    db.commit()
    return s


@pytest.fixture
def client(session_factory, storage):
    def _db():
        with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_session_factory] = lambda: session_factory
    app.dependency_overrides[get_storage] = lambda: storage
    yield TestClient(app)
    app.dependency_overrides.clear()
