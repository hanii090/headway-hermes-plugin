"""Small read-only dashboard API; no Desktop pane is installed."""
from __future__ import annotations

import json
from pathlib import Path

from . import plugin_main


def health():
    return {"ok": True, "plugin": "headway", "version": "0.1.0"}


def status(session: str = "default"):
    # Status does not fingerprint or mutate the workspace. It reads the same
    # session/root-scoped state used by middleware and slash commands.
    return plugin_main.status_for_session(session)


# Hermes discovers an APIRouter when FastAPI is present. Plain functions above
# keep this module importable in the no-Hermes unit-test environment.
try:
    from fastapi import APIRouter
    router = APIRouter()
    router.get("/health")(health)
    router.get("/status")(status)
except ImportError:  # pragma: no cover - depends on Hermes environment
    router = None
