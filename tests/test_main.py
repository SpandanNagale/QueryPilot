# tests/test_main.py
from __future__ import annotations

import importlib

from starlette.middleware.cors import CORSMiddleware


def _cors_allow_credentials(origins_env: str) -> bool:
    """Reload app.main with a given QP_CORS_ALLOWED_ORIGINS value and return
    the allow_credentials the CORS middleware was actually configured with."""
    import app.config as config_module
    import app.main as main_module

    original = config_module.settings.cors_allowed_origins
    try:
        config_module.settings.cors_allowed_origins = origins_env
        importlib.reload(main_module)
        for mw in main_module.app.user_middleware:
            if mw.cls is CORSMiddleware:
                return mw.kwargs["allow_credentials"]
        raise AssertionError("CORSMiddleware not registered on app")
    finally:
        config_module.settings.cors_allowed_origins = original
        importlib.reload(main_module)


def test_empty_cors_origins_disables_credentials():
    assert _cors_allow_credentials("") is False


def test_wildcard_cors_origin_never_gets_credentials():
    # regression: allow_origins=["*"] + allow_credentials=True is forbidden
    assert _cors_allow_credentials("*") is False
