from __future__ import annotations
import asyncio
import json
import re
from pathlib import Path
from fastapi import APIRouter, Form, UploadFile, File
from pydantic import BaseModel

from config import settings
from models.schemas import IngestConfigResponse, IngestUploadResponse, JobStatusResponse
from services import ingest_pipeline
from services import weaviate_client as wc
from utils import api_error

router = APIRouter(prefix="/ingest")

_CONFIGS_DIR: Path | None = None


class SaveIngestConfigBody(BaseModel):
    collection: str
    chunking_strategy: str = "overlap"
    chunk_size: int = 1000
    chunk_overlap: int = 200
    similarity_threshold: float | None = None
    min_chunk_size: int = 100


def _configs_dir() -> Path:
    global _CONFIGS_DIR
    if _CONFIGS_DIR is None:
        _CONFIGS_DIR = Path(settings.upload_dir) / "ingest_configs"
        _CONFIGS_DIR.mkdir(parents=True, exist_ok=True)
    return _CONFIGS_DIR


def _safe_name(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_]", "_", name)


def _load_config(collection: str) -> dict | None:
    p = _configs_dir() / f"{_safe_name(collection)}.json"
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            pass
    return None


@router.post("/upload", response_model=IngestUploadResponse, status_code=202)
async def ingest_upload(
    collection: str = Form(...),
    strategy: str = Form("overlap"),
    chunk_size: int = Form(1000),
    chunk_overlap: int = Form(200),
    similarity_threshold: float = Form(0.85),
    min_chunk_size: int = Form(100),
    files: list[UploadFile] = File(...),
):
    if not await wc.collection_exists(collection):
        return api_error(404, "COLLECTION_NOT_FOUND", f"Collection '{collection}' not found.")

    try:
        job_id = await ingest_pipeline.start_ingest_job(
            files=files,
            collection=collection,
            strategy=strategy,
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            similarity_threshold=similarity_threshold,
            min_chunk_size=min_chunk_size,
        )
    except ValueError as exc:
        return api_error(400, "NO_SUPPORTED_FILES", str(exc))

    return IngestUploadResponse(
        job_id=job_id,
        status="queued",
        files_queued=len(files),
        collection=collection,
    )


@router.get("/job/{job_id}", response_model=JobStatusResponse)
async def job_status(job_id: str):
    job = ingest_pipeline.get_job(job_id)
    if job is None:
        return api_error(404, "JOB_NOT_FOUND", f"Job '{job_id}' not found.")
    return JobStatusResponse(**job)


@router.get("/config/{collection}", response_model=IngestConfigResponse)
async def get_ingest_config(collection: str):
    cfg = await asyncio.to_thread(_load_config, collection)
    is_default = cfg is None
    if is_default:
        cfg = {
            "collection": collection,
            "chunking_strategy": "overlap",
            "chunk_size": 1000,
            "chunk_overlap": 200,
            "similarity_threshold": None,
            "min_chunk_size": 100,
        }
    return IngestConfigResponse(is_default=is_default, **cfg)


def _write_config(p: Path, data: str) -> None:
    p.write_text(data)


@router.post("/config", response_model=IngestConfigResponse, status_code=201)
async def save_ingest_config(body: SaveIngestConfigBody):
    cfg = body.model_dump()
    p = _configs_dir() / f"{_safe_name(body.collection)}.json"
    await asyncio.to_thread(_write_config, p, json.dumps(cfg, indent=2))
    return IngestConfigResponse(is_default=False, **cfg)
