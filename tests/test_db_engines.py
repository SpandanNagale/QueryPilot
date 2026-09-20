from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from sqlalchemy import text

from app.config import settings
from app.core.db import _set_sqlite_pragma, get_engines


def test_get_engines_is_cached_per_path(tmp_path):
    db_path = tmp_path / "a.db"
    shutil.copyfile(settings.template_db_path, db_path)
    ro1, rw1 = get_engines(str(db_path))
    ro2, rw2 = get_engines(str(db_path))
    assert ro1 is ro2
    assert rw1 is rw2


def test_get_engines_different_paths_are_isolated(tmp_path):
    path_a = tmp_path / "a.db"
    path_b = tmp_path / "b.db"
    shutil.copyfile(settings.template_db_path, path_a)
    shutil.copyfile(settings.template_db_path, path_b)
    ro_a, rw_a = get_engines(str(path_a))
    ro_b, rw_b = get_engines(str(path_b))

    with rw_a.connect() as conn:
        with conn.begin():
            conn.execute(text("DELETE FROM orders WHERE id = 1"))

    with ro_a.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM orders WHERE id = 1")).scalar_one() == 0
    with ro_b.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM orders WHERE id = 1")).scalar_one() == 1


def test_read_only_engine_from_get_engines_still_refuses_writes(tmp_path):
    db_path = tmp_path / "c.db"
    shutil.copyfile(settings.template_db_path, db_path)
    ro, _ = get_engines(str(db_path))
    with ro.connect() as conn:
        with pytest.raises(Exception):
            conn.execute(text("DELETE FROM orders WHERE id = 1"))


def test_sqlite_pragma_listener_noops_for_non_sqlite3_connection():
    """_set_sqlite_pragma is registered on the SQLAlchemy Engine *class*, so
    it fires for every engine in the process, including app/core/opdb.py's
    Postgres engine in production. A non-sqlite3 DBAPI connection (e.g. a
    psycopg connection) must be ignored, not handed a SQLite-only PRAGMA
    statement that would raise a Postgres syntax error."""

    class _FakeCursor:
        def execute(self, *a, **kw):
            raise AssertionError("PRAGMA should never be executed against a non-sqlite3 connection")

        def close(self):
            pass

    class _FakeNonSqliteConnection:  # stands in for e.g. a psycopg connection
        def cursor(self):
            raise AssertionError("cursor() should never be called for a non-sqlite3 connection")

    # Should not raise, and must not touch the fake connection at all.
    _set_sqlite_pragma(_FakeNonSqliteConnection(), None)
