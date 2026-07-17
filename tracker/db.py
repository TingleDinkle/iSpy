"""Database engine and session factory."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from .config import settings

engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,   # survive dropped connections in long-running jobs
    pool_size=5,
    max_overflow=5,
)

SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def load_active(model, store: str | None = None) -> list:
    """Fetch all is_active rows of a registry model (App, RankingWatch,
    Developer, MarketSegment), optionally filtered by store. Instances are
    returned detached with attributes loaded — the pattern every job repeats."""
    from sqlalchemy import select  # local import to avoid cycles at import time

    query = select(model).where(model.is_active.is_(True)).order_by(model.id)
    if store is not None:
        query = query.where(model.store == store)
    with SessionLocal() as session:
        return list(session.execute(query).scalars())


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope: commit on success, rollback on error."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
