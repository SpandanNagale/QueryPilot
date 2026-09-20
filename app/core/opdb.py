"""Operational database: audit events and budget entries. Points at Neon
Postgres in production (QP_DATABASE_URL) and a local SQLite file otherwise,
so local development needs no cloud account. Deliberately separate from
app/core/db.py's per-session query-data engines - this one holds QueryPilot's
own bookkeeping data, never the demo dataset the agent queries."""
from __future__ import annotations

import json
from contextlib import contextmanager
from functools import partial

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings

# default=str mirrors the old JSONL log_event's json.dumps(record, default=str)
# safety net: a non-JSON-primitive value in an audit payload (a date, a
# Decimal, ...) gets coerced to a string instead of raising, since the ORM's
# JSON column type otherwise calls plain json.dumps with no default=.
engine = create_engine(
    settings.resolved_database_url,
    pool_pre_ping=True,
    future=True,
    json_serializer=partial(json.dumps, default=str),
)
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
