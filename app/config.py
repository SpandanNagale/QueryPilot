"""Centralized configuration for QueryPilot.

All limits, model names, and connection strings live here. Nothing in the
rest of the app should hardcode a model name, file path, or safety limit.
"""
from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="QP_", extra="ignore")

    # --- Database ---
    database_path: Path = PROJECT_ROOT / "data" / "querypilot.db"

    # --- Safety / guardrails ---
    read_only_mode: bool = False          # when true, manipulation path is fully disabled
    row_impact_ceiling: int = 50          # block writes affecting more rows than this
    auto_limit: int = 200                 # LIMIT injected into SELECTs lacking one
    max_generation_retries: int = 2       # validator failure -> regenerate, this many times
    max_self_correction_attempts: int = 3 # total attempts including the first, on execution error
    confirmation_timeout_seconds: int = 60

    # --- LLM (Ollama, fully local) ---
    ollama_host: str = "http://localhost:11434"
    sql_gen_model: str = "qwen2.5-coder:14b"
    chat_model: str = "qwen3:14b"
    embed_model: str = "nomic-embed-text"
    llm_request_timeout_seconds: int = 60

    # --- Schema retrieval ---
    chroma_persist_dir: Path = PROJECT_ROOT / "data" / "chroma"
    schema_retrieval_top_k: int = 3
    sample_rows_per_table: int = 3

    # --- Logging ---
    audit_log_path: Path = PROJECT_ROOT / "logs" / "audit.jsonl"

    # --- LangGraph checkpointing ---
    checkpoint_db_path: Path = PROJECT_ROOT / "data" / "checkpoints.sqlite"

    @property
    def read_write_uri(self) -> str:
        return f"file:{self.database_path}"

    @property
    def read_only_uri(self) -> str:
        return f"file:{self.database_path}?mode=ro"


settings = Settings()
