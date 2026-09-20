"""Session-wide test isolation.

Two real files were getting written to by a plain `pytest -q` run:

1. `data/schema_embeddings.json` - every `TestClient(app)`-based test
   triggers FastAPI's lifespan, which calls `build_or_refresh_index()`
   against the real `settings.schema_embeddings_cache_path`.
2. `data/template.db` - the golden-master template every session sandbox is
   copied from. `tests/test_guardrails.py`'s `scratch_order` fixture writes
   into `read_write_engine`, which is bound to `settings.template_db_path`;
   a crash mid-fixture (Ctrl-C, `pytest -x`) leaves junk rows behind with no
   automatic repair.

Redirected rather than backed-up/restored: this fixture copies template.db
to a temp file once per session and repoints settings at the copies, so no
test ever touches the real files at all - simpler and immune to an
interrupted run leaving things dirty.

This has to happen in pytest_configure(), which pytest runs before it
collects (imports) any test module - not in a normal fixture, which only
runs at test setup time, well after collection has already imported
app.core.db elsewhere. app/core/db.py builds its read_only_engine /
read_write_engine globals once, at its own import time, from
settings.template_db_path - so the settings patch has to land before that
first import for those globals (and every module-level default-argument
binding built from them - app/core/validator.py, app/core/executor.py,
app/core/schema_store.py, app/main.py) to pick up the redirected path
instead of the real one.
"""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path


def pytest_configure(config) -> None:
    from app.config import settings

    tmp_dir = Path(tempfile.mkdtemp(prefix="qp_pytest_"))

    settings.schema_embeddings_cache_path = tmp_dir / "schema_embeddings.json"

    if settings.template_db_path.exists():
        tmp_template = tmp_dir / "template.db"
        shutil.copyfile(settings.template_db_path, tmp_template)
        settings.template_db_path = tmp_template
