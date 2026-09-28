from __future__ import annotations
from typing import Any, Optional
from pydantic import BaseModel, Field, field_validator, model_validator


# ── Collections ──────────────────────────────────────────────────────────────

class HnswConfig(BaseModel):
    efConstruction: int = 128
    maxConnections: int = 64
    ef: int = 64


class CreateCollectionRequest(BaseModel):
    name: str
    index_type: str = "hnsw"
    distance_metric: str = "cosine"
    hnsw_config: HnswConfig = HnswConfig()


class CollectionInfo(BaseModel):
    name: str
    object_count: int
    index_type: str
    distance_metric: str
    created_at: Optional[str]


class CollectionsResponse(BaseModel):
    collections: list[CollectionInfo]


# ── Ingest ────────────────────────────────────────────────────────────────────

class IngestUploadResponse(BaseModel):
    job_id: str
    status: str
    files_queued: int
    collection: str


class JobStatusResponse(BaseModel):
    job_id: str
    status: str
    files_total: int
    files_completed: int
    files_failed: int
    chunks_stored: int
    errors: list[str]
    # Files the caller sent that could not be parsed at all; they never
    # became part of files_total, so without this they vanish silently.
    skipped: list[str] = []


class IngestConfig(BaseModel):
    chunking_strategy: str = "overlap"
    chunk_size: int = 1000
    chunk_overlap: int = 200
    similarity_threshold: Optional[float] = None
    min_chunk_size: int = 100


class IngestConfigResponse(BaseModel):
    collection: str
    chunking_strategy: str
    chunk_size: int
    chunk_overlap: int
    similarity_threshold: Optional[float]
    min_chunk_size: int
    is_default: bool


# ── Retrieval config ──────────────────────────────────────────────────────────

class SaveRetrievalConfigBody(BaseModel):
    collection: str
    retrieval_mode: str = "hnsw"
    top_k: int = 5
    alpha: float = 0.75
    ef: Optional[int] = None
    response_format: str = "end_user"

    @field_validator("retrieval_mode")
    @classmethod
    def _mode(cls, v: str) -> str:
        # "semantic" routes to Weaviate near_text in rag_pipeline.run_query;
        # it is offered on the Retrieval page, so the stored config must accept it.
        allowed = ("hnsw", "flat", "hybrid", "semantic")
        if v not in allowed:
            raise ValueError(f"retrieval_mode must be one of {allowed}, got {v!r}")
        return v

    @field_validator("response_format")
    @classmethod
    def _fmt(cls, v: str) -> str:
        allowed = ("end_user", "engineer")
        if v not in allowed:
            raise ValueError(f"response_format must be one of {allowed}, got {v!r}")
        return v

    @field_validator("top_k")
    @classmethod
    def _top_k(cls, v: int) -> int:
        if not 1 <= v <= 50:
            raise ValueError("top_k must be between 1 and 50")
        return v

    @field_validator("alpha")
    @classmethod
    def _alpha(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            raise ValueError("alpha must be between 0 and 1")
        return v


class RetrievalConfigResponse(BaseModel):
    collection: str
    retrieval_mode: str
    top_k: int
    alpha: float
    ef: Optional[int]
    response_format: str
    is_default: bool


class HelpResponse(BaseModel):
    topic: str
    markdown: str


# ── Export / Import ───────────────────────────────────────────────────────────

class ExportRequest(BaseModel):
    collection: str
    include_models: bool = False


class ExportStartResponse(BaseModel):
    job_id: str
    status: str
    collection: str


class ExportJobStatusResponse(BaseModel):
    job_id: str
    status: str
    collection: str
    chunks_written: int
    filename: Optional[str]
    size_bytes: Optional[int]
    source_document_count: Optional[int]
    fidelity: Optional[str]
    # What the package actually carries. A request for models that could not be
    # honoured comes back false, with the reason in `warnings`.
    models_bundled: Optional[bool]
    retrieve_script: Optional[bool]
    warnings: list[str]
    error: Optional[str]


class ImportRequest(BaseModel):
    filename: str
    # Required, no default (spec §6.4): silently picking a conflict policy could
    # delete a collection the caller did not mean to touch.
    on_conflict: str

    @field_validator("on_conflict")
    @classmethod
    def _on_conflict(cls, v: str) -> str:
        allowed = ("abort", "rename", "replace")
        if v not in allowed:
            raise ValueError(f"on_conflict must be one of {allowed}, got {v!r}")
        return v


class ImportStartResponse(BaseModel):
    job_id: str
    status: str
    filename: str


class ImportedSessionMapping(BaseModel):
    source_session_id: str
    session_id: str
    collection: str


class ImportJobStatusResponse(BaseModel):
    job_id: str
    status: str
    filename: str
    on_conflict: str
    # The name actually imported under; differs from original_collection when
    # on_conflict='rename' resolved a collision.
    collection: Optional[str]
    original_collection: Optional[str]
    chunks_written: int
    fidelity: Optional[str]
    renamed: bool
    notes: list[str]
    restored_sessions: list[ImportedSessionMapping] = Field(default_factory=list)
    error: Optional[str]
    error_code: Optional[str]
    error_detail: Optional[dict]


class PackageSummary(BaseModel):
    filename: str
    size_bytes: int
    # Null when the archive could not be read; the listing still shows the file
    # so the user is not left wondering where it went.
    collection: Optional[str]
    chunk_count: Optional[int]
    fidelity: Optional[str]
    created_at: Optional[str]
    readable: bool


class PackageListResponse(BaseModel):
    packages: list[PackageSummary]


# ── Tuning ────────────────────────────────────────────────────────────────────

CHUNKING_STRATEGIES = ("fixed", "overlap", "language", "context_aware", "semantic")


class _ChunkingFields(BaseModel):
    chunking_strategy: Optional[str] = None
    chunk_size: Optional[int] = None
    chunk_overlap: Optional[int] = None
    similarity_threshold: Optional[float] = None
    min_chunk_size: Optional[int] = None

    @field_validator("chunking_strategy")
    @classmethod
    def _strategy(cls, v):
        if v is not None and v not in CHUNKING_STRATEGIES:
            raise ValueError(f"chunking_strategy must be one of {CHUNKING_STRATEGIES}, got {v!r}")
        return v

    def has_chunking(self) -> bool:
        return any(getattr(self, f) is not None for f in
                   ("chunking_strategy", "chunk_size", "chunk_overlap",
                    "similarity_threshold", "min_chunk_size"))

    def chunking(self) -> dict:
        """Defaults match the ingest pipeline, so an omitted field means 'as before'."""
        return {
            "strategy": self.chunking_strategy or "overlap",
            "chunk_size": self.chunk_size if self.chunk_size is not None else 1000,
            "chunk_overlap": self.chunk_overlap if self.chunk_overlap is not None else 200,
            "similarity_threshold": (self.similarity_threshold
                                     if self.similarity_threshold is not None else 0.85),
            "min_chunk_size": self.min_chunk_size if self.min_chunk_size is not None else 100,
        }


class RechunkRequest(_ChunkingFields):
    collection: str


class ReembedRequest(_ChunkingFields):
    """Chunking fields are optional here, and only legal with `with-sources`."""
    collection: str


class ReindexRequest(BaseModel):
    collection: str
    index_type: Optional[str] = None
    distance_metric: Optional[str] = None

    @field_validator("index_type")
    @classmethod
    def _index(cls, v):
        if v is not None and v not in ("hnsw", "flat"):
            raise ValueError(f"index_type must be 'hnsw' or 'flat', got {v!r}")
        return v

    @field_validator("distance_metric")
    @classmethod
    def _distance(cls, v):
        allowed = ("cosine", "dot", "l2-squared")
        if v is not None and v not in allowed:
            raise ValueError(f"distance_metric must be one of {allowed}, got {v!r}")
        return v


class TuneStartResponse(BaseModel):
    job_id: str
    status: str
    collection: str
    operation: str


class TuneJobStatusResponse(BaseModel):
    job_id: str
    status: str
    collection: str
    operation: str
    chunks_total: int
    chunks_written: int
    notes: list[str]
    error: Optional[str]
    error_code: Optional[str]
    error_detail: Optional[dict]


class TuneOptionsResponse(BaseModel):
    collection: str
    fidelity: str
    source_document_count: int
    can_rechunk: bool
    can_reembed: bool
    can_reindex: bool
    note: str


# ── Query ─────────────────────────────────────────────────────────────────────

class QueryRequest(BaseModel):
    question: str
    collection: str
    retrieval_mode: str = "hnsw"
    top_k: int = 5
    alpha: float = 0.75
    include_citations: bool = False
    response_format: str = "end_user"


class Citation(BaseModel):
    source_file: str
    chunk_index: int
    score: float
    excerpt: str


class QueryResponse(BaseModel):
    answer: str
    citations: Optional[list[Citation]]
    retrieval_latency_ms: int
    llm_latency_ms: int
    chunks_retrieved: int


# ── Gold Standard ─────────────────────────────────────────────────────────────

class GenerateRequest(BaseModel):
    collection: str
    sample_size: int = 20
    seed: Optional[int] = None


class GenerateResponse(BaseModel):
    session_id: str
    status: str
    pairs_total: int
    # `attempted` reaches `total` even when a pair fails, so it drives progress;
    # `completed` is how many pairs actually exist.
    pairs_attempted: int = 0
    pairs_completed: int
    pairs_failed: int = 0


class GoldPair(BaseModel):
    pair_id: str
    question: str
    answer: str
    contexts: list[str]
    ground_truth: str
    source_file: str
    chunk_index: int
    status: str


class SessionImportProvenance(BaseModel):
    session_id: str = Field(pattern=r"^gs_[0-9a-f]{8}$")
    collection: str
    imported_at: str


class SessionResponse(BaseModel):
    session_id: str
    status: str
    pairs_total: int
    # `attempted` reaches `total` even when a pair fails, so it drives progress;
    # `completed` is how many pairs actually exist.
    pairs_attempted: int = 0
    pairs_completed: int
    pairs_failed: int = 0
    pairs: list[GoldPair]
    collection: str
    errors: list[str] = []
    imported_from: SessionImportProvenance | None = None


class PatchPairRequest(BaseModel):
    status: Optional[str] = None
    question: Optional[str] = None
    answer: Optional[str] = None
    ground_truth: Optional[str] = None

    @model_validator(mode="after")
    def _content_edit_needs_edited_status(self):
        """Rewriting a pair's content must be recorded as an edit.

        Without this a caller could change the question while the pair still
        reads "approved", and the export would ship content nobody approved
        under a status that says otherwise.
        """
        content = [f for f in ("question", "answer", "ground_truth")
                   if getattr(self, f) is not None]
        if content and self.status != "edited":
            raise ValueError(
                f"changing {', '.join(content)} requires status='edited'; "
                f"got status={self.status!r}")
        return self

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        allowed = {"approved", "edited", "rejected", "pending"}
        if v not in allowed:
            raise ValueError(f"status must be one of {allowed}")
        return v


class RegenerateRequest(BaseModel):
    session_id: str
    pair_id: str


class SaveRequest(BaseModel):
    session_id: str
    filename: Optional[str] = None


class SaveResponse(BaseModel):
    filename: str
    pairs_saved: int
    pairs_excluded: int
    download_url: str


# ── Metrics ───────────────────────────────────────────────────────────────────

class LatencyStats(BaseModel):
    p50: float
    p95: float
    p99: float
    mean: float
    count: int


class LatencyRecord(BaseModel):
    """One recorded query, for plotting latency over time."""
    timestamp: str
    collection: str
    retrieval_mode: str
    retrieval_ms: int
    llm_ms: int
    total_ms: int


class MetricsResponse(BaseModel):
    total_records: int
    retrieval_latency: LatencyStats
    llm_latency: LatencyStats
    total_latency: LatencyStats
    # Most recent records, oldest first so a chart reads left to right.
    # Bounded by the `limit` query parameter: the ring buffer holds up to 500
    # entries and the UI polls every 30s, so returning all of them every time
    # would be wasteful.
    history: list[LatencyRecord]


# ── Error ─────────────────────────────────────────────────────────────────────

class ErrorDetail(BaseModel):
    code: str
    message: str
    detail: Any = None


class ErrorResponse(BaseModel):
    error: ErrorDetail
