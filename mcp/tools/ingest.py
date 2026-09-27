"""Document ingestion tools."""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

import config
import paths as inbox
import ragclient
from mcpapp import server

_TERMINAL = ("completed", "failed", "error")


@server.tool(
    name="rag_ingest_files",
    description=(
        "Ingest one or more documents into a collection. Paths are relative to "
        "the ingest inbox (./ingest-inbox in the project directory), which is "
        "the only filesystem this server can read. Waits up to wait_seconds for "
        "the job to finish; if it is still running, returns a job_id to poll "
        "with rag_ingest_status."
    ),
    annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False),
)
async def rag_ingest_files(
    collection: str,
    paths: list[str],
    wait_seconds: int | None = None,
) -> dict:
    if not paths:
        raise ToolError("At least one path is required, relative to the ingest inbox.")

    budget = config.INGEST_WAIT_SECONDS if wait_seconds is None else int(wait_seconds)
    if budget < 0 or budget > 600:
        raise ToolError("wait_seconds must be between 0 and 600.")

    resolved: list[Path] = [inbox.resolve_inbox_path(p) for p in paths]

    files = []
    handles = []
    try:
        for p in resolved:
            fh = open(p, "rb")
            handles.append(fh)
            files.append(("files", (p.name, fh, "application/octet-stream")))
        started = await ragclient.post(
            "/ingest/upload", data={"collection": collection}, files=files
        )
    finally:
        for fh in handles:
            fh.close()

    job_id = started.get("job_id")
    if not job_id:
        return started

    deadline = time.monotonic() + budget
    status = started
    while True:
        if time.monotonic() >= deadline:
            break
        status = await ragclient.get(f"/ingest/job/{job_id}")
        if status.get("status") in _TERMINAL:
            return status
        await asyncio.sleep(2)

    # Budget exhausted (or zero): hand back the handle rather than time out.
    if budget > 0:
        status = await ragclient.get(f"/ingest/job/{job_id}")
        if status.get("status") in _TERMINAL:
            return status
    return {
        **status,
        "job_id": job_id,
        "note": (
            f"Still running after {budget}s. Poll rag_ingest_status with "
            f"job_id '{job_id}'."
        ),
    }


@server.tool(
    name="rag_ingest_status",
    description="Check the progress of an ingest job started by rag_ingest_files.",
    annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True),
)
async def rag_ingest_status(job_id: str) -> dict:
    return await ragclient.get(f"/ingest/job/{job_id}")


@server.tool(
    name="rag_get_ingest_config",
    description="Read the saved chunking configuration for a collection.",
    annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True),
)
async def rag_get_ingest_config(collection: str) -> dict:
    return await ragclient.get(f"/ingest/config/{collection}")


@server.tool(
    name="rag_set_ingest_config",
    description=(
        "Save the default chunking configuration for a collection. Applies to "
        "future ingests, not to documents already stored."
    ),
    annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True),
)
async def rag_set_ingest_config(
    collection: str,
    chunking_strategy: str = "overlap",
    chunk_size: int = 1000,
    chunk_overlap: int = 200,
    similarity_threshold: float | None = None,
    min_chunk_size: int = 100,
) -> dict:
    body = {
        "collection": collection,
        "chunking_strategy": chunking_strategy,
        "chunk_size": chunk_size,
        "chunk_overlap": chunk_overlap,
        "min_chunk_size": min_chunk_size,
    }
    if similarity_threshold is not None:
        body["similarity_threshold"] = similarity_threshold
    return await ragclient.post("/ingest/config", json=body)
