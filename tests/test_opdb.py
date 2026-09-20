from __future__ import annotations

from app.core.models import AuditEvent, BudgetEntry
from app.core.opdb import Base, get_session


def test_local_sqlite_fallback_creates_tables_and_round_trips(tmp_path, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "database_url", f"sqlite:///{tmp_path / 'op.db'}")
    from sqlalchemy import create_engine

    test_engine = create_engine(settings.resolved_database_url)
    Base.metadata.create_all(test_engine)

    from sqlalchemy.orm import sessionmaker

    TestSession = sessionmaker(bind=test_engine)
    with TestSession() as session:
        session.add(AuditEvent(session_id="11111111-1111-4111-8111-111111111111", event="test", payload={"a": 1}))
        session.commit()

    with TestSession() as session:
        rows = session.query(AuditEvent).all()
    assert len(rows) == 1
    assert rows[0].payload == {"a": 1}


def test_resolved_database_url_falls_back_to_local_sqlite(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "database_url", "")
    assert settings.resolved_database_url.startswith("sqlite:///")


def test_resolved_database_url_normalizes_bare_postgresql_scheme_to_psycopg3(monkeypatch):
    """Neon hands out bare postgresql:// URLs, but SQLAlchemy 2.0 maps that
    scheme to psycopg2, which isn't installed (requirements.txt installs
    psycopg[binary], i.e. psycopg3). Must be rewritten or create_engine()
    crashes at import time."""
    from app.config import settings

    monkeypatch.setattr(settings, "database_url", "postgresql://user:pass@host/db")
    assert settings.resolved_database_url == "postgresql+psycopg://user:pass@host/db"


def test_resolved_database_url_leaves_already_correct_dialect_unchanged(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "database_url", "postgresql+psycopg://user:pass@host/db")
    assert settings.resolved_database_url == "postgresql+psycopg://user:pass@host/db"


def test_resolved_database_url_leaves_sqlite_unchanged(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "database_url", "sqlite:///some/path.db")
    assert settings.resolved_database_url == "sqlite:///some/path.db"
