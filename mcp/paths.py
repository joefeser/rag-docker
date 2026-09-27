"""Ingest inbox path resolution.

The inbox is the only host filesystem this server can see, and it is mounted
read-only. Every path a caller supplies is resolved against it and must stay
inside it.
"""
from __future__ import annotations

import os
from pathlib import Path

from mcp.server.mcpserver.exceptions import ToolError

import config


class InboxPathError(ToolError):
    """Raised with a message written for the caller, naming the offending path.

    Subclasses ToolError so the SDK surfaces the text; a plain ValueError would
    be masked and the caller would never learn which path was rejected or why.
    """


def _contract() -> str:
    return (
        "Paths must be relative to the ingest inbox, which is mounted read-only "
        "at the container path configured by RAG_MCP_INGEST_ROOT (default /host) "
        "and maps to ./ingest-inbox in the project directory. Place the file "
        "there and pass a path relative to it, e.g. 'report.pdf' or 'docs/q3.pdf'."
    )


def resolve_inbox_path(rel: str) -> Path:
    if not rel or not rel.strip():
        raise InboxPathError(f"An empty path was given. {_contract()}")

    if os.path.isabs(rel):
        raise InboxPathError(f"'{rel}' is an absolute path. {_contract()}")

    root = Path(config.INGEST_ROOT).resolve()

    # Canonicalise BEFORE comparing. Resolving first is what makes this safe
    # against both '..' traversal and symlinks pointing outside the inbox;
    # comparing the unresolved path would let either escape.
    candidate = (root / rel).resolve()

    if candidate != root and root not in candidate.parents:
        raise InboxPathError(
            f"'{rel}' resolves outside the ingest inbox. {_contract()}"
        )

    if not candidate.exists():
        raise InboxPathError(f"'{rel}' was not found in the ingest inbox. {_contract()}")

    if not candidate.is_file():
        raise InboxPathError(f"'{rel}' is not a regular file. {_contract()}")

    return candidate
