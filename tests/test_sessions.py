from __future__ import annotations

import time
import uuid

from sqlalchemy import text

from app.config import settings
from app.core import sessions
from app.core.db import get_engines


def _new_id() -> str:
    return str(uuid.uuid4())


def test_valid_uuid4_accepted():
    assert sessions.is_valid_session_id(str(uuid.uuid4()))


def test_traversal_and_malformed_ids_rejected():
    for bad in ["../../etc/passwd", "not-a-uuid", "", "12345", str(uuid.uuid1())]:
        assert not sessions.is_valid_session_id(bad)


def test_first_access_copies_template(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "session_dir", tmp_path)
    session_id = _new_id()
    path = sessions.get_session_db_path(session_id)
    assert path.exists()
    with get_engines(str(path))[0].connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM orders")).scalar_one() > 0


def test_two_sessions_are_isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "session_dir", tmp_path)
    id_a, id_b = _new_id(), _new_id()
    path_a = sessions.get_session_db_path(id_a)
    path_b = sessions.get_session_db_path(id_b)

    ro_a, rw_a = get_engines(str(path_a))
    ro_b, _ = get_engines(str(path_b))

    with rw_a.connect() as conn:
        with conn.begin():
            conn.execute(text("DELETE FROM orders WHERE id = 1"))

    assert conn_count(ro_a, "orders", "id = 1") == 0
    assert conn_count(ro_b, "orders", "id = 1") == 1


def conn_count(engine, table, where) -> int:
    with engine.connect() as conn:
        return conn.execute(text(f"SELECT COUNT(*) FROM {table} WHERE {where}")).scalar_one()


def test_reset_restores_original_data(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "session_dir", tmp_path)
    session_id = _new_id()
    path = sessions.get_session_db_path(session_id)
    rw, _ = get_engines(str(path))[1], None
    ro, rw = get_engines(str(path))

    with rw.connect() as conn:
        with conn.begin():
            conn.execute(text("DELETE FROM orders WHERE id = 1"))
    assert conn_count(ro, "orders", "id = 1") == 0

    sessions.reset_session(session_id)
    ro2, _ = get_engines(str(path))  # same path, same cached engine pair
    assert conn_count(ro2, "orders", "id = 1") == 1


def test_sweep_removes_idle_sessions_past_ttl(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "session_dir", tmp_path)
    monkeypatch.setattr(settings, "session_ttl_minutes", 0)  # anything untouched for >0 minutes expires
    session_id = _new_id()
    path = sessions.get_session_db_path(session_id)
    old_time = time.time() - 3600
    import os

    os.utime(path, (old_time, old_time))

    removed = sessions.sweep_expired_sessions()
    assert removed == 1
    assert not path.exists()


def test_sweep_evicts_oldest_when_over_max_sessions(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "session_dir", tmp_path)
    monkeypatch.setattr(settings, "max_sessions", 2)
    monkeypatch.setattr(settings, "session_ttl_minutes", 999)  # nothing expires by age in this test
    import os

    paths = []
    for i in range(3):
        path = sessions.get_session_db_path(_new_id())
        os.utime(path, (time.time() + i, time.time() + i))  # strictly increasing mtimes
        paths.append(path)

    removed = sessions.sweep_expired_sessions()
    assert removed == 1
    assert not paths[0].exists()  # oldest evicted
    assert paths[1].exists() and paths[2].exists()
