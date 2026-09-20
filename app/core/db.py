"""SQLAlchemy engine setup.

Two engines are exposed on purpose:

- ``read_write_engine`` — full read/write access, used only by the seed
  script and the transactional executor (Phase 5).
- ``read_only_engine`` — opens the SQLite file with SQLite's own
  ``mode=ro`` URI flag, so a bug in the validator cannot result in a write.
  This is enforced by SQLite itself, not just application logic.

Swapping SQLite for Postgres later only means changing these two
``create_engine`` calls (e.g. a read-only role/connection string) — nothing
else in the app should know or care which database backend is in use.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.pool import NullPool

from app.config import settings


def _rw_uri(db_path: str) -> str:
    return f"file:{db_path}"


def _ro_uri(db_path: str) -> str:
    return f"file:{db_path}?mode=ro"


@lru_cache(maxsize=256)
def get_engines(db_path: str) -> tuple[Engine, Engine]:
    """One (read_only, read_write) engine pair per distinct SQLite file path,
    cached for the process lifetime. Every session's sandbox copy gets its
    own pair the first time it's requested; the template's pair is built the
    same way, through the same cache, so there's exactly one code path for
    "give me the engines for this file" regardless of which file it is."""
    db_path = str(db_path)
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    def _rw_creator() -> sqlite3.Connection:
        return sqlite3.connect(_rw_uri(db_path), uri=True, check_same_thread=False)

    def _ro_creator() -> sqlite3.Connection:
        return sqlite3.connect(_ro_uri(db_path), uri=True, check_same_thread=False)

    rw = create_engine("sqlite://", creator=_rw_creator, poolclass=NullPool, future=True)
    ro = create_engine("sqlite://", creator=_ro_creator, poolclass=NullPool, future=True)
    return ro, rw


read_only_engine, read_write_engine = get_engines(str(settings.template_db_path))


@event.listens_for(Engine, "connect")
def _set_sqlite_pragma(dbapi_connection, connection_record) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


@dataclass
class ForeignKeyRef:
    child_table: str
    child_column: str
    on_delete: str  # "CASCADE" | "NO ACTION" | "RESTRICT" | "SET NULL" | "SET DEFAULT"


def get_referencing_tables(parent_table: str, engine: Engine = read_only_engine) -> list[ForeignKeyRef]:
    """Every foreign key in the live schema that points at parent_table, with
    its ON DELETE action. Used to preview cascading deletes and to block
    (with a clear reason) deletes that would otherwise fail with a raw
    FOREIGN KEY constraint IntegrityError."""
    refs: list[ForeignKeyRef] = []
    with engine.connect() as conn:
        table_names = [
            row[0]
            for row in conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
            )
        ]
        for table in table_names:
            for fk in conn.execute(text(f'PRAGMA foreign_key_list("{table}")')).fetchall():
                # columns: id, seq, table(referenced), from(child col), to(parent col), on_update, on_delete, match
                referenced_table, child_column, on_delete = fk[2], fk[3], fk[6]
                if referenced_table == parent_table:
                    refs.append(ForeignKeyRef(child_table=table, child_column=child_column, on_delete=on_delete))
    return refs
