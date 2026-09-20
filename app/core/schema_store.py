"""Schema retrieval via ChromaDB.

Each table's description (name, purpose, columns, sample values) is
embedded once and persisted; a user request retrieves only the top-k most
relevant tables instead of the full schema being dumped into every prompt.

Embeddings come from Ollama (nomic-embed-text), not ChromaDB's bundled
default embedder — the latter downloads a model from Hugging Face at
runtime, which would violate "fully local, no external API calls" the
moment this ran on a machine without that model cached.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import chromadb
import ollama
from chromadb import Documents, EmbeddingFunction, Embeddings
from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.config import settings
from app.core.db import read_only_engine

_embedding_client = ollama.Client(host=settings.ollama_host, timeout=settings.llm_request_timeout_seconds)

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


class OllamaEmbeddingFunction(EmbeddingFunction):
    def __call__(self, input: Documents) -> Embeddings:
        response = _embedding_client.embed(model=settings.embed_model, input=list(input))
        return response["embeddings"]


_COLLECTION_NAME = "querypilot_schema"
_client: chromadb.ClientAPI | None = None
_collection: chromadb.Collection | None = None


def _get_client() -> chromadb.ClientAPI:
    global _client
    if _client is None:
        settings.chroma_persist_dir.mkdir(parents=True, exist_ok=True)
        # anonymized_telemetry=False: this system is fully local by design,
        # chromadb's default telemetry would otherwise attempt outbound calls.
        _client = chromadb.PersistentClient(
            path=str(settings.chroma_persist_dir),
            settings=chromadb.Settings(anonymized_telemetry=False),
        )
    return _client


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


def build_or_refresh_index(force: bool = False) -> int:
    """(Re)embeds every table description into the persistent Chroma
    collection. Idempotent and cheap to call at startup: skipped if the
    collection already has one entry per live table, unless force=True."""
    global _collection

    tables = introspect_tables()
    client = _get_client()
    collection = client.get_or_create_collection(
        name=_COLLECTION_NAME, embedding_function=OllamaEmbeddingFunction()
    )

    if not force and collection.count() == len(tables):
        existing_ids = set(collection.get()["ids"])
        if existing_ids == set(tables.keys()):
            _collection = collection
            return 0

    ids = list(tables.keys())
    documents = [tables[name].as_embedding_document() for name in ids]
    collection.upsert(ids=ids, documents=documents)

    _collection = collection
    return len(ids)


def _get_collection() -> chromadb.Collection:
    global _collection
    if _collection is None:
        build_or_refresh_index()
    return _collection


def retrieve_relevant_tables(nl_query: str, top_k: int | None = None) -> list[TableInfo]:
    """Top-k most relevant tables for a natural-language request, each
    carrying live columns + sample rows for prompt injection."""
    top_k = settings.schema_retrieval_top_k if top_k is None else top_k
    collection = _get_collection()
    top_k = min(top_k, collection.count())

    results = collection.query(query_texts=[nl_query], n_results=top_k)
    retrieved_names = results["ids"][0]

    tables = introspect_tables(table_names=retrieved_names)
    return [tables[name] for name in retrieved_names if name in tables]


def format_schema_context(tables: list[TableInfo]) -> str:
    return "\n\n".join(t.as_prompt_block() for t in tables)


def full_schema_summary() -> str:
    """All tables, used for the GET /schema endpoint and the session-start
    schema tour — not for the generation prompt, which uses retrieval."""
    tables = introspect_tables()
    return "\n\n".join(t.as_prompt_block() for t in tables.values())
