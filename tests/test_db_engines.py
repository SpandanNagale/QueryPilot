from __future__ import annotations

import shutil
from pathlib import Path

from sqlalchemy import text

from app.config import settings
from app.core.db import get_engines


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
        try:
            conn.execute(text("DELETE FROM orders WHERE id = 1"))
            assert False, "read-only engine must refuse writes"
        except Exception:
            pass
