"""The primary retrieval-augmented generation tool."""
from __future__ import annotations

from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

import ragclient
from mcpapp import server

_MODES = ("hnsw", "flat", "hybrid")


@server.tool(
    name="rag_query",
    description=(
        "Ask a question against a collection using retrieval-augmented "
        "generation. Returns an answer grounded in the stored documents, with "
        "citations. Collection names are matched with only the first letter "
        "normalised; the rest is case-sensitive. Generation is CPU-bound and "
        "typically takes 10-40 seconds."
    ),
    annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=False),
)
async def rag_query(
    question: str,
    collection: str,
    retrieval_mode: str = "hnsw",
    top_k: int = 5,
    alpha: float = 0.75,
    include_citations: bool = True,
    response_format: str = "end_user",
) -> dict:
    # Three canonical modes. The API has an unnamed fallback branch for any
    # other string, but neither the API nor the UI names it, so an unrecognised
    # mode here is a mistake rather than a choice.
    if retrieval_mode not in _MODES:
        raise ToolError(f"retrieval_mode must be one of {_MODES}, got {retrieval_mode!r}")
    if response_format not in ("end_user", "engineer"):
        raise ToolError("response_format must be 'end_user' or 'engineer'.")
    if not 1 <= top_k <= 50:
        raise ToolError("top_k must be between 1 and 50.")
    if not 0.0 <= alpha <= 1.0:
        raise ToolError("alpha must be between 0 and 1.")

    return await ragclient.post(
        "/query",
        json={
            "question": question,
            "collection": collection,
            "retrieval_mode": retrieval_mode,
            "top_k": top_k,
            "alpha": alpha,
            "include_citations": include_citations,
            "response_format": response_format,
        },
    )
