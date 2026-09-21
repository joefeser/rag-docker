"""Entrypoint for the RAG MCP server (stdio transport).

Order matters: logging is configured before the SDK or any tool module is
imported, so nothing can write to stdout before stderr is pinned.
"""
from __future__ import annotations

import config
from logging_setup import configure_logging

configure_logging(config.LOG_LEVEL)

import logging  # noqa: E402

from mcpapp import server  # noqa: E402

# Importing each module registers its tools against the shared server.
import tools.diagnostics  # noqa: F401,E402
import tools.collections  # noqa: F401,E402
import tools.ingest  # noqa: F401,E402
import tools.query  # noqa: F401,E402
import tools.goldstandard  # noqa: F401,E402

log = logging.getLogger("rag-mcp")


def main() -> None:
    log.info("RAG MCP server starting; API base %s", config.RAG_API_BASE_URL)
    log.info("Ingest inbox: %s", config.INGEST_ROOT)
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
