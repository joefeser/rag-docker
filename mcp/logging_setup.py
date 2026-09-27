"""Logging that can never corrupt the JSON-RPC stream.

stdio transport means stdout carries protocol frames and nothing else. A single
stray write to stdout -- a print(), or a library defaulting there -- desynchronises
the stream and presents to the user as an unexplained hang. Everything therefore
goes to stderr, pinned explicitly rather than left to defaults.

Kept in its own module so importing it cannot pull in the SDK: it must run
before anything else has a chance to log.
"""
from __future__ import annotations

import logging
import sys


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
    root = logging.getLogger()
    root.handlers[:] = [handler]
    try:
        root.setLevel(level.upper())
    except ValueError:
        root.setLevel(logging.INFO)
        root.warning("Unknown log level %r; falling back to INFO", level)
