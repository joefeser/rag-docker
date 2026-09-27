from __future__ import annotations

from fastapi import APIRouter

from models.schemas import QueryRequest, QueryResponse
from services import metrics
from services import rag_pipeline
from services import weaviate_client as wc
from utils import api_error

router = APIRouter(prefix="/query")


@router.post("", response_model=QueryResponse)
async def run_query(body: QueryRequest):
    if not await wc.collection_exists(body.collection):
        return api_error(404, "COLLECTION_NOT_FOUND", f"Collection '{body.collection}' not found.")

    result = await rag_pipeline.run_query(
        question=body.question,
        collection=body.collection,
        retrieval_mode=body.retrieval_mode,
        top_k=body.top_k,
        alpha=body.alpha,
        include_citations=body.include_citations,
        response_format=body.response_format,
    )

    await metrics.record(
        collection=body.collection,
        retrieval_mode=body.retrieval_mode,
        retrieval_ms=result["retrieval_latency_ms"],
        llm_ms=result["llm_latency_ms"],
        chunks_retrieved=result["chunks_retrieved"],
    )

    return QueryResponse(**result)
