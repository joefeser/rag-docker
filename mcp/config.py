"""Environment-driven settings. Defaults work unmodified in the standard deployment."""
from __future__ import annotations

import os


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


# The api service serves its routes at the ROOT (/health, /collections). The
# /api prefix belongs to the nginx proxy, which this server bypasses entirely by
# talking to the service over the compose network.
RAG_API_BASE_URL = os.getenv("RAG_API_BASE_URL", "http://api:8000").rstrip("/")

INGEST_ROOT = os.getenv("RAG_MCP_INGEST_ROOT", "/host")
INGEST_WAIT_SECONDS = _int("RAG_MCP_INGEST_WAIT_SECONDS", 120)
HTTP_TIMEOUT = _float("RAG_MCP_HTTP_TIMEOUT", 300.0)
LOG_LEVEL = os.getenv("RAG_MCP_LOG_LEVEL", "INFO")
