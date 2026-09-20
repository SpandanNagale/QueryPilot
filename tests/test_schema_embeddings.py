from __future__ import annotations

import json

from app.config import settings
from app.core import schema_store


def test_build_or_refresh_index_writes_cache_file(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "schema_embeddings_cache_path", tmp_path / "embeddings.json")
    count = schema_store.build_or_refresh_index()
    assert count > 0
    cache = json.loads((tmp_path / "embeddings.json").read_text())
    assert set(cache.keys()) == set(schema_store.TABLE_PURPOSES.keys())
    for entry in cache.values():
        assert len(entry["vector"]) == settings.embedding_dimensions


def test_build_or_refresh_index_is_idempotent_with_warm_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "schema_embeddings_cache_path", tmp_path / "embeddings.json")
    schema_store.build_or_refresh_index()
    second_call_embedded_count = schema_store.build_or_refresh_index()
    assert second_call_embedded_count == 0


def test_retrieve_relevant_tables_ranks_by_similarity(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "schema_embeddings_cache_path", tmp_path / "embeddings.json")
    schema_store.build_or_refresh_index()
    tables = schema_store.retrieve_relevant_tables("how many products are low on stock?", top_k=2)
    names = [t.name for t in tables]
    assert "inventory" in names


def test_retrieve_relevant_tables_degrades_to_full_schema_on_embedding_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "schema_embeddings_cache_path", tmp_path / "embeddings.json")

    def _broken_embed(texts):
        raise RuntimeError("simulated embedding outage")

    monkeypatch.setattr(schema_store, "_embed_texts", _broken_embed)
    tables = schema_store.retrieve_relevant_tables("anything", top_k=2)
    assert len(tables) == len(schema_store.TABLE_PURPOSES)
