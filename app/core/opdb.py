"""Operational database: audit events and budget entries. Points at Neon
Postgres in production (QP_DATABASE_URL) and a local SQLite file otherwise,
so local development needs no cloud account. Deliberately separate from
app/core/db.py's per-session query-data engines - this one holds QueryPilot's
own bookkeeping data, never the demo dataset the agent queries."""
from __future__ import annotations

from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings

engine = create_engine(settings.resolved_database_url, pool_pre_ping=True, future=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


@contextmanager
def get_session():
    session: Session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
