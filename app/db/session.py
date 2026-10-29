from collections.abc import Iterator
from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings


def sqlalchemy_url(raw: str) -> str:
    """Point plain postgres:// URLs (as Supabase gives them) at the psycopg 3 driver."""
    for prefix in ("postgresql://", "postgres://"):
        if raw.startswith(prefix):
            return "postgresql+psycopg://" + raw[len(prefix) :]
    return raw


@lru_cache
def get_engine() -> Engine:
    url = sqlalchemy_url(get_settings().database_url.get_secret_value())
    # hide_parameters keeps row values out of error messages; echo stays off.
    return create_engine(url, pool_pre_ping=True, hide_parameters=True)


@lru_cache
def _session_factory() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), expire_on_commit=False)


def get_db() -> Iterator[Session]:
    """FastAPI dependency: one session per request."""
    db = _session_factory()()
    try:
        yield db
    finally:
        db.close()


def get_session_factory() -> sessionmaker[Session]:
    """FastAPI dependency for work that outlives the request's session (a streamed answer
    is written by a worker thread)."""
    return _session_factory()
