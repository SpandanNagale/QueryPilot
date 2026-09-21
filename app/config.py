"""Centralized configuration for QueryPilot.

All limits, model names, and connection strings live here. Nothing in the
rest of the app should hardcode a model name, file path, or safety limit.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="QP_", extra="ignore")

    # --- Database ---
    template_db_path: Path = PROJECT_ROOT / "data" / "template.db"

    # --- Per-session sandbox ---
    session_dir: Path = Path(tempfile.gettempdir()) / "qp_sessions"
    session_ttl_minutes: int = 30
    max_sessions: int = 200

    # --- Safety / guardrails ---
    read_only_mode: bool = False          # when true, manipulation path is fully disabled
    row_impact_ceiling: int = 50          # block writes affecting more rows than this
    auto_limit: int = 200                 # LIMIT injected into SELECTs lacking one
    max_generation_retries: int = 2       # validator failure -> regenerate, this many times
    max_self_correction_attempts: int = 3 # total attempts including the first, on execution error
    confirmation_timeout_seconds: int = 60
    guardrail_max_input_chars: int = 2000 # input guardrail: reject raw user messages longer than this
    rate_limit_per_minute: int = 10       # per-IP sliding-window cap on /chat and /confirm requests
    daily_budget_usd: float = 0.0         # kill-switch: block /chat once today's (UTC) LLM spend hits this; 0.0 = no cap

    # --- LLM gateway ---
    llm_profile: str = "cloud"   # "cloud" (default, deployed chain) or "local" (dev-only, forces ollama_local)
    model_groups_path: Path = PROJECT_ROOT / "config" / "model_groups.yaml"
    ollama_host: str = "http://localhost:11434"   # local Ollama: used by QP_LLM_PROFILE=local
    llm_request_timeout_seconds: int = 60
    llm_cache_enabled: bool = True  # exact-match in-memory cache for intent_classifier/sql_generator; escape hatch, not required for normal operation

    # --- Schema retrieval ---
    schema_embeddings_cache_path: Path = PROJECT_ROOT / "data" / "schema_embeddings.json"
    embedding_model: str = "gemini-embedding-001"
    embedding_dimensions: int = 768
    schema_retrieval_top_k: int = 3
    sample_rows_per_table: int = 3

    # --- Audit access control ---
    admin_token: str = ""  # required in the X-Admin-Token header for GET /audit's full (all-sessions) log

    # --- LangGraph checkpointing ---
    checkpoint_db_path: Path = Path(tempfile.gettempdir()) / "qp_checkpoints" / "checkpoints.sqlite"

    # --- Operational storage (audit events, budget entries) ---
    # Empty means "use a local SQLite file" -- local dev needs zero cloud setup.
    database_url: str = ""

    # --- CORS ---
    cors_allowed_origins: str = ""  # comma-separated; empty means no cross-origin requests are allowed

    @property
    def resolved_database_url(self) -> str:
        # SQLAlchemy 2.0 maps bare "postgresql://" to the psycopg2 dialect,
        # but requirements.txt installs psycopg (v3) -- Neon's dashboard
        # hands out exactly the bare "postgresql://" format, so without this
        # rewrite create_engine() (and thus `alembic upgrade head`, the
        # Docker CMD's first step) crashes on a real Neon URL.
        url = self.database_url or f"sqlite:///{PROJECT_ROOT / 'data' / 'operational.db'}"
        return url.replace("postgresql://", "postgresql+psycopg://", 1)


settings = Settings()


class ProviderCredentials(BaseSettings):
    """Cloud LLM provider credentials. Deliberately NOT under the QP_ prefix
    -- these are the exact env var names the deployment docs and Render
    blueprint use (OLLAMA_API_KEY, GEMINI_API_KEY, etc.), matching the
    sibling Crewai-Research-pipeline repo's config/settings.py convention."""

    model_config = SettingsConfigDict(env_file=".env", env_prefix="", extra="ignore")

    ollama_api_key: str = ""
    ollama_cloud_host: str = "https://ollama.com"
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    gemini_api_key: str = ""
    groq_api_key: str = ""
    groq_base_url: str = "https://api.groq.com/openai/v1"


provider_credentials = ProviderCredentials()


class LangfuseSettings(BaseSettings):
    """Optional Langfuse tracing (Phase 4). Unprefixed, matching the sibling
    Crewai-Research-pipeline repo's config/settings.py -- and Langfuse's own
    documented env var names. Unset (the default) disables tracing entirely;
    see app/core/langfuse_sink.py::is_configured()."""

    model_config = SettingsConfigDict(env_file=".env", env_prefix="", extra="ignore")

    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = "https://cloud.langfuse.com"


langfuse_settings = LangfuseSettings()
