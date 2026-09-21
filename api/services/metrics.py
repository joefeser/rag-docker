from __future__ import annotations
import asyncio
import json
import statistics
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from config import settings

MAX_RECORDS = 500
_ring: deque[dict] = deque(maxlen=MAX_RECORDS)

_METRICS_FILE = None


def _metrics_path() -> Path:
    global _METRICS_FILE
    if _METRICS_FILE is None:
        p = Path(settings.upload_dir)
        p.mkdir(parents=True, exist_ok=True)
        _METRICS_FILE = p / "metrics.jsonl"
    return _METRICS_FILE


def load_from_disk() -> None:
    p = _metrics_path()
    if not p.exists():
        return
    lines = p.read_text().splitlines()
    for line in lines[-MAX_RECORDS:]:
        try:
            _ring.append(json.loads(line))
        except Exception:
            pass


def _append_sync(line: str) -> None:
    with open(_metrics_path(), "a") as f:
        f.write(line + "\n")


async def record(
    collection: str,
    retrieval_mode: str,
    retrieval_ms: int,
    llm_ms: int,
    chunks_retrieved: int,
) -> None:
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "collection": collection,
        "retrieval_mode": retrieval_mode,
        "retrieval_ms": retrieval_ms,
        "llm_ms": llm_ms,
        "total_ms": retrieval_ms + llm_ms,
        "chunks_retrieved": chunks_retrieved,
    }
    _ring.append(entry)
    await asyncio.to_thread(_append_sync, json.dumps(entry))


def _percentile(data: list[float], p: int) -> float:
    if not data:
        return 0.0
    data_sorted = sorted(data)
    k = (len(data_sorted) - 1) * p / 100
    lo, hi = int(k), min(int(k) + 1, len(data_sorted) - 1)
    frac = k - lo
    return round(data_sorted[lo] + frac * (data_sorted[hi] - data_sorted[lo]), 1)


def _stats(values: list[float]) -> dict:
    if not values:
        return {"p50": 0.0, "p95": 0.0, "p99": 0.0, "mean": 0.0, "count": 0}
    return {
        "p50": _percentile(values, 50),
        "p95": _percentile(values, 95),
        "p99": _percentile(values, 99),
        "mean": round(statistics.mean(values), 1),
        "count": len(values),
    }


def get_summary(
    collection: str | None = None,
    retrieval_mode: str | None = None,
    history_limit: int = 100,
) -> dict:
    records = list(_ring)
    if collection:
        records = [r for r in records if r.get("collection") == collection]
    if retrieval_mode:
        records = [r for r in records if r.get("retrieval_mode") == retrieval_mode]

    retrieval = [r["retrieval_ms"] for r in records]
    llm = [r["llm_ms"] for r in records]
    total = [r["total_ms"] for r in records]

    # Most recent `history_limit` records, kept in chronological order. The ring
    # buffer is already oldest-first, so the tail is the newest slice.
    recent = records[-history_limit:] if history_limit > 0 else []
    history = [
        {
            "timestamp": r.get("ts", ""),
            "collection": r.get("collection", ""),
            "retrieval_mode": r.get("retrieval_mode", ""),
            "retrieval_ms": r.get("retrieval_ms", 0),
            "llm_ms": r.get("llm_ms", 0),
            "total_ms": r.get("total_ms", 0),
        }
        for r in recent
    ]

    return {
        "total_records": len(records),
        "retrieval_latency": _stats(retrieval),
        "llm_latency": _stats(llm),
        "total_latency": _stats(total),
        "history": history,
    }
