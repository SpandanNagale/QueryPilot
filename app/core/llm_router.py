# app/core/llm_router.py
"""Loads config/model_groups.yaml and returns the ordered Deployment chain
for an alias ("sql_gen" or "chat"). Node code never sees a provider or model
name -- only an alias, resolved here. QP_LLM_PROFILE=local swaps in the
single ollama_local deployment from the config's `local:` section instead of
the deployed chain, for offline dev."""
from __future__ import annotations

import threading
import time
from typing import List

import yaml

from app.config import settings
from app.core.llm_errors import LLMConfigError
from app.core import llm_providers
from app.core.llm_providers import Deployment

_ALIASES = ("sql_gen", "chat")

_config_lock = threading.Lock()
_config_cache: dict | None = None

_deployment_lock = threading.Lock()
_deployment_cache: dict[str, List[Deployment]] = {}


def _load_config() -> dict:
    global _config_cache
    with _config_lock:
        if _config_cache is None:
            with open(settings.model_groups_path) as f:
                _config_cache = yaml.safe_load(f) or {}
        return _config_cache


def clear_deployment_cache() -> None:
    """Test-only: force the next get_deployments() call to rebuild from disk."""
    global _config_cache
    with _config_lock:
        _config_cache = None
    with _deployment_lock:
        _deployment_cache.clear()


def _group_for_alias(alias: str) -> list[dict]:
    config = _load_config()
    group = config.get("local", {}).get(alias) if settings.llm_profile == "local" else config.get(alias)
    if not group:
        raise LLMConfigError(f"No model_groups entry for alias={alias!r} (profile={settings.llm_profile!r}).")
    return group


def get_deployments(alias: str) -> List[Deployment]:
    with _deployment_lock:
        cache_key = f"{settings.llm_profile}:{alias}"
        if cache_key in _deployment_cache:
            return _deployment_cache[cache_key]

        group = _group_for_alias(alias)
        deployments = [llm_providers.build_deployment(entry["provider"], entry["model"]) for entry in group]
        _deployment_cache[cache_key] = deployments
        return deployments


def check_providers() -> dict[str, list[dict]]:
    """Pings every deployment of every alias with one short call. Computed
    fresh each call -- no caching, so it reflects the current cooldown-free
    reality. GET /health/providers is the only caller; keep pings cheap."""
    results: dict[str, list[dict]] = {}
    for alias in _ALIASES:
        entries: list[dict] = []
        try:
            deployments = get_deployments(alias)
        except LLMConfigError as e:
            results[alias] = [{"provider": None, "model": None, "status": "error", "detail": str(e)}]
            continue

        for deployment in deployments:
            started = time.monotonic()
            try:
                deployment.call(messages=[{"role": "user", "content": "Reply with the single word: pong"}], temperature=0.0)
                entries.append(
                    {
                        "provider": deployment.provider_key,
                        "model": deployment.model,
                        "status": "ok",
                        "detail": "",
                        "latency_ms": (time.monotonic() - started) * 1000,
                    }
                )
            except Exception as e:  # noqa: BLE001 - health probe reports any failure reason
                entries.append(
                    {"provider": deployment.provider_key, "model": deployment.model, "status": "error", "detail": str(e)}
                )
        results[alias] = entries
    return results
