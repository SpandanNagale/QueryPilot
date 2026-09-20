"""Schema retrieval via Gemini embeddings + numpy cosine similarity.

Each table's description (name, purpose, columns) is embedded once via
Gemini's embedding API and cached to disk, keyed by a hash of the document
text so a schema/description change invalidates just that table's cached
vector. A user request embeds the query text at request time and ranks
tables by cosine similarity - no vector database, no ONNX runtime, no
background telemetry client: at 6 tables, a linear numpy scan is simply
faster to build and run than standing up a real vector index.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from functools import lru_cache

import numpy as np
from google import genai
from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.config import provider_credentials, settings
from app.core.db import read_only_engine

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _get_embedding_client() -> genai.Client:
    """Built lazily on first real use, not at import time - importing this
    module (or app.main) with no GEMINI_API_KEY set must not raise, so that
    QP_LLM_PROFILE=local (zero-cloud-setup dev) still works and the
    degrade-to-full-schema path in retrieve_relevant_tables actually gets a
    chance to run instead of failing before its own try/except."""
    return genai.Client(api_key=provider_credentials.gemini_api_key)

# Static business-purpose descriptions. Column names/types and sample rows
# are appended live from the database, so this only needs to say *why* a
# table exists, not what's in it mechanically.
TABLE_PURPOSES: dict[str, str] = {
    "products": (
        "Catalog of cold-drink products manufactured and sold: cola, juice, water, "
        "and energy drinks, with unit price and bottle size."
    ),
    "retailers": (
        "Wholesale customers (general stores, supermarkets, hotels) across towns in "
        "Ratnagiri district that buy products, each with a credit limit."
    ),
    "orders": (
        "Purchase orders placed by retailers: order/delivery dates, status "
        "(pending/delivered/cancelled), and total amount. The central fact table."
    ),
    "order_items": (
        "Line items within an order: which product, what quantity, and the unit "
        "price charged for that order. Joins orders to products."
    ),
    "payments": (
        "Payments received against orders (cash/upi/credit). An order can be "
        "unpaid, partially paid, or fully paid — absence of a payment row means unpaid."
    ),
    "inventory": (
        "Current stock level per product and when it was last restocked. Used to "
        "answer stockout / low-stock questions."
    ),
}


_ENUM_MAX_DISTINCT = 10  # columns with at most this many distinct values are shown in full


@dataclass
class TableInfo:
    name: str
    purpose: str
    columns: list[tuple[str, str]] = field(default_factory=list)   # (name, type)
    sample_rows: list[dict] = field(default_factory=list)
    enum_columns: dict[str, list[str]] = field(default_factory=dict)  # low-cardinality TEXT columns -> all values

    def as_prompt_block(self) -> str:
        col_lines = ", ".join(f"{name} {ctype}" for name, ctype in self.columns)
        lines = [f"TABLE {self.name} ({col_lines})", f"  purpose: {self.purpose}"]
        for col, values in self.enum_columns.items():
            lines.append(f"  {col} is always one of: {values}")
        for row in self.sample_rows:
            lines.append(f"  sample: {row}")
        return "\n".join(lines)

    def as_embedding_document(self) -> str:
        col_names = ", ".join(name for name, _ in self.columns)
        return f"{self.name}: {self.purpose} Columns: {col_names}."


def introspect_tables(
    table_names: list[str] | None = None, engine: Engine = read_only_engine
) -> dict[str, TableInfo]:
    """Live introspection: columns from PRAGMA, sample rows and low-cardinality
    TEXT column values (e.g. status/category enums) straight from the DB.
    Never hand-maintained, so it can't drift from the real schema.

    Pass table_names to introspect only a subset (e.g. the tables retrieval
    picked) instead of every table. Pass engine to introspect a session's
    sandbox database instead of the template.
    """
    tables: dict[str, TableInfo] = {}
    with engine.connect() as conn:
        if table_names is None:
            table_names = [
                row[0]
                for row in conn.execute(
                    text("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
                )
            ]
        for name in table_names:
            columns = [
                (row[1], row[2]) for row in conn.execute(text(f'PRAGMA table_info("{name}")')).fetchall()
            ]
            sample_rows = [
                dict(row._mapping)
                for row in conn.execute(
                    text(f'SELECT * FROM "{name}" LIMIT :n'), {"n": settings.sample_rows_per_table}
                )
            ]
            enum_columns: dict[str, list[str]] = {}
            for col_name, col_type in columns:
                if col_type.upper() != "TEXT":
                    continue
                distinct = [
                    row[0]
                    for row in conn.execute(
                        text(f'SELECT DISTINCT "{col_name}" FROM "{name}" LIMIT :n'),
                        {"n": _ENUM_MAX_DISTINCT + 1},
                    )
                ]
                if 1 < len(distinct) <= _ENUM_MAX_DISTINCT:
                    enum_columns[col_name] = sorted(distinct)
            tables[name] = TableInfo(
                name=name,
                purpose=TABLE_PURPOSES.get(name, ""),
                columns=columns,
                sample_rows=sample_rows,
                enum_columns=enum_columns,
            )
    return tables


def _embed_texts(texts: list[str]) -> list[list[float]]:
    result = _get_embedding_client().models.embed_content(
        model=settings.embedding_model,
        contents=texts,
        config={"output_dimensionality": settings.embedding_dimensions},
    )
    return [list(e.values) for e in result.embeddings]


def _doc_hash(document: str) -> str:
    return hashlib.sha256(document.encode("utf-8")).hexdigest()


def _load_cache() -> dict[str, dict]:
    if not settings.schema_embeddings_cache_path.exists():
        return {}
    try:
        return json.loads(settings.schema_embeddings_cache_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_cache(cache: dict[str, dict]) -> None:
    settings.schema_embeddings_cache_path.parent.mkdir(parents=True, exist_ok=True)
    settings.schema_embeddings_cache_path.write_text(json.dumps(cache), encoding="utf-8")


def build_or_refresh_index(force: bool = False) -> int:
    """(Re)embeds every table description whose cached hash doesn't match its
    current as_embedding_document() text. Idempotent and cheap to call at
    startup: with a warm cache and no schema changes, this makes zero
    embedding calls."""
    tables = introspect_tables()
    cache = {} if force else _load_cache()

    to_embed: list[str] = []
    to_embed_names: list[str] = []
    for name, table in tables.items():
        document = table.as_embedding_document()
        digest = _doc_hash(document)
        if cache.get(name, {}).get("hash") != digest:
            to_embed.append(document)
            to_embed_names.append(name)

    if to_embed:
        vectors = _embed_texts(to_embed)
        for name, document, vector in zip(to_embed_names, to_embed, vectors):
            cache[name] = {"hash": _doc_hash(document), "vector": vector}

    # Drop cache entries for tables that no longer exist.
    for stale_name in list(cache.keys()):
        if stale_name not in tables:
            del cache[stale_name]

    _save_cache(cache)
    return len(to_embed)


def retrieve_relevant_tables(nl_query: str, top_k: int | None = None) -> list[TableInfo]:
    """Top-k most relevant tables for a natural-language request, ranked by
    cosine similarity against the cached per-table embeddings. Falls back to
    every table (full schema) on any embedding failure - with 6 tables the
    full schema is small enough that this degradation is nearly free, and
    correctness matters far more than an optimally-targeted prompt."""
    top_k = settings.schema_retrieval_top_k if top_k is None else top_k
    tables = introspect_tables()

    try:
        cache = _load_cache()
        missing = [name for name in tables if name not in cache]
        if missing:
            build_or_refresh_index()
            cache = _load_cache()

        query_vector = np.array(_embed_texts([nl_query])[0])
        names = list(tables.keys())
        matrix = np.array([cache[name]["vector"] for name in names])
        query_norm = query_vector / np.linalg.norm(query_vector)
        matrix_norm = matrix / np.linalg.norm(matrix, axis=1, keepdims=True)
        scores = matrix_norm @ query_norm

        ranked = [name for _, name in sorted(zip(scores, names), reverse=True)]
        top_names = ranked[:top_k]
        return [tables[name] for name in top_names]
    except Exception:  # noqa: BLE001 - any embedding/cache failure degrades to the full schema, never fails the request
        logger.warning("Schema embedding retrieval failed; falling back to full schema.", exc_info=True)
        return list(tables.values())


def format_schema_context(tables: list[TableInfo]) -> str:
    return "\n\n".join(t.as_prompt_block() for t in tables)


def full_schema_summary() -> str:
    """All tables, used for the GET /schema endpoint and the session-start
    schema tour — not for the generation prompt, which uses retrieval."""
    tables = introspect_tables()
    return "\n\n".join(t.as_prompt_block() for t in tables.values())
