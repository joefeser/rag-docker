from __future__ import annotations
import asyncio
import json
import logging
import copy
import os
import tempfile
import threading

import httpx
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from config import settings
from services import ollama_client as ollama
from services import weaviate_client as wc

GS_SYSTEM = (
    "You are creating evaluation data for a RAG system. Given a text chunk, generate one question "
    "that can be answered from this chunk, the correct answer based only on this chunk, and a ground "
    "truth answer (same as the answer). Return a JSON object with keys: question, answer, ground_truth. "
    "Do not include any text outside the JSON object."
)
GS_RETRY_SUFFIX = (
    " Your previous response was not valid JSON. Return ONLY the JSON object with keys: "
    "question, answer, ground_truth. No markdown, no explanation."
)

log = logging.getLogger(__name__)


class GoldStandardError(Exception):
    """Carries an API error code so the router does not have to guess."""

    def __init__(self, code: str, message: str, status: int):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status

_sessions: dict[str, dict] = {}
_tasks: set[asyncio.Task] = set()
_state_lock = threading.RLock()
_diagnostics: dict[str, dict] = {}


def _sessions_dir() -> Path:
    p = Path(settings.upload_dir) / "goldstandard_sessions"
    created = not p.exists()
    p.mkdir(parents=True, exist_ok=True)
    if created:
        _sync_directory(p.parent)
    return p


def _session_path(session_id: str) -> Path:
    return _sessions_dir() / f"{session_id}.json"


def _record_issue(path: Path, code: str) -> None:
    changed = _diagnostics.get(str(path), {}).get("code") != code
    messages = {
        "SESSION_STORAGE_UNAVAILABLE": "Session storage could not be inspected. Existing files are preserved; inspect local storage and restart after recovery.",
        "SESSION_READ_FAILED": "Session file could not be loaded. Original bytes are preserved; restore a valid copy and restart the API.",
        "SESSION_INTERRUPTED_WRITE": "An interrupted write left an unpublished temporary snapshot. The final JSON file remains authoritative; inspect the temporary file before removing it.",
        "SESSION_WRITE_FAILED": "Update failed before replacement; the previous snapshot remains authoritative. Check local storage before retrying.",
        "SESSION_DURABILITY_UNCERTAIN": "Replacement occurred but directory durability could not be confirmed. Refresh the session and inspect local storage before retrying.",
    }
    _diagnostics[str(path)] = {"filename": path.name, "code": code, "message": messages[code]}
    if changed:
        log.error("Session persistence issue %s for %s", code, path.name)


def _sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _save_session_sync(session: dict) -> None:
    """Publish one immutable snapshot under the single-process writer lock.

    Before replace, errors leave both the prior disk snapshot and cache intact.
    After replace, cache reflects disk even if directory fsync reports an
    uncertain durability outcome. Neither failure is acknowledged as success.
    """
    with _state_lock:
        snapshot = copy.deepcopy(session)
        try:
            path = _session_path(snapshot["session_id"])
        except OSError as exc:
            _record_issue(Path(settings.upload_dir) / "goldstandard_sessions" / (str(snapshot["session_id"]) + ".json"), "SESSION_WRITE_FAILED")
            raise GoldStandardError("SESSION_WRITE_FAILED", "Session directory could not be prepared. The previous snapshot is unchanged.", 503) from exc
        temporary = None
        replaced = False
        try:
            payload = json.dumps(snapshot, indent=2, allow_nan=False)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                    prefix="." + path.stem + "-", suffix=".tmp", delete=False) as output:
                temporary = Path(output.name)
                output.write(payload)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
            replaced = True
            _sessions[snapshot["session_id"]] = snapshot
            _sync_directory(path.parent)
            _diagnostics.pop(str(path), None)
        except (OSError, ValueError, TypeError) as exc:
            code = "SESSION_DURABILITY_UNCERTAIN" if replaced else "SESSION_WRITE_FAILED"
            _record_issue(path, code)
            message = ("Session replacement occurred but durability could not be confirmed. Refresh the session and inspect diagnostics before retrying."
                       if replaced else "Session update could not be persisted. The previous snapshot is unchanged.")
            raise GoldStandardError(code, message, 503) from exc
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    log.exception("Could not remove owned session temporary file %s", temporary.name)


async def _save_session(session: dict) -> None:
    await asyncio.to_thread(store_session, session)


def _scan_sessions_locked() -> None:
    try:
        root = _sessions_dir()
        with os.scandir(root) as entries:
            paths = [Path(entry.path) for entry in entries]
    except (OSError, ValueError, RuntimeError):
        _record_issue(Path(settings.upload_dir) / "goldstandard_sessions", "SESSION_STORAGE_UNAVAILABLE")
        return
    _diagnostics.pop(str(root), None)
    for temporary in paths:
        if re.fullmatch(r"\.gs_[0-9a-f]{8}-.+\.tmp", temporary.name):
            _record_issue(temporary, "SESSION_INTERRUPTED_WRITE")
    for path in paths:
        if not path.name.endswith(".json"):
            continue
        try:
            data = json.loads(path.read_text())
            if not isinstance(data, dict) or data.get("session_id") != path.stem or not isinstance(data.get("pairs"), list) or not all(isinstance(pair, dict) for pair in data["pairs"]):
                raise ValueError("Invalid retained session structure")
            from models.schemas import SessionResponse
            SessionResponse.model_validate(data)
            _sessions.setdefault(data["session_id"], data)
            # Write failures remain visible until a successful write, even if
            # the previous valid disk snapshot is readable.
            if _diagnostics.get(str(path), {}).get("code") == "SESSION_READ_FAILED":
                _diagnostics.pop(str(path), None)
        except (OSError, ValueError, TypeError):
            _record_issue(path, "SESSION_READ_FAILED")


def load_sessions_from_disk() -> None:
    with _state_lock:
        _scan_sessions_locked()


def session_diagnostics() -> list[dict]:
    with _state_lock:
        _scan_sessions_locked()
        return copy.deepcopy([_diagnostics[key] for key in sorted(_diagnostics)])


def sessions_for(collection: str) -> list[dict]:
    with _state_lock:
        _scan_sessions_locked()
        found = []
        for sid, session in _sessions.items():
            if session.get("collection") != collection:
                continue
            try:
                from models.schemas import SessionResponse
                SessionResponse.model_validate(session)
                if sid != session.get("session_id"):
                    raise ValueError("Cached session key does not match identity")
            except (ValueError, TypeError):
                _record_issue(Path(str(sid) + ".json"), "SESSION_READ_FAILED")
                continue
            found.append(session)
        return copy.deepcopy(found)


def store_session(session: dict) -> None:
    """Explicit whole-session storage; publishes cache only after replacement."""
    _save_session_sync(session)


def _update_session_sync(session_id: str, change):
    with _state_lock:
        current = _sessions.get(session_id)
        if current is None:
            return None
        snapshot = copy.deepcopy(current)
        result = change(snapshot)
        if snapshot != current:
            _save_session_sync(snapshot)
        return copy.deepcopy(result)


def _flag_sessions(collection: str, flag: str, reason: str) -> int:
    with _state_lock:
        now = datetime.now(timezone.utc).isoformat()
        marked = 0
        for session in sessions_for(collection):
            def change(current):
                current[flag] = True
                current[f"{flag}_reason"] = reason
                current[f"{flag}_at"] = now
            _update_session_sync(session["session_id"], change)
            marked += 1
        return marked


def mark_stale(collection: str, reason: str) -> int:
    return _flag_sessions(collection, "stale", reason)


def mark_orphaned(collection: str, reason: str) -> int:
    return _flag_sessions(collection, "orphaned", reason)


def get_session(session_id: str) -> dict | None:
    with _state_lock:
        return copy.deepcopy(_sessions.get(session_id))


def _parse_gs_json(text: str) -> dict:
    """Pull the JSON object out of a model reply.

    The model reliably returns a correct object and then keeps talking --
    "Extra data: line 6 column 1" was the single most common generation
    failure, costing pairs on nearly every session. `raw_decode` reads the
    leading value and ignores whatever follows, so trailing commentary is no
    longer fatal. A reply that opens with prose is still handled, by starting
    at the first brace.
    """
    text = text.strip()
    text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
    text = re.sub(r"\n?```$", "", text)
    text = text.strip()

    decoder = json.JSONDecoder()
    positions = [i for i, ch in enumerate(text) if ch == "{"]
    if not positions:
        raise ValueError("model reply contained no JSON object")
    # Anchoring on the first brace is not enough: a reply that explains itself
    # first ("return an object like { this }") puts a brace before the real
    # payload. Try each candidate and keep the first that decodes.
    last_error: Exception | None = None
    for start in positions:
        try:
            result, _ = decoder.raw_decode(text[start:])
        except ValueError as exc:
            last_error = exc
            continue
        if isinstance(result, dict):
            return result
        last_error = ValueError(f"Expected JSON object, got {type(result).__name__}")
    raise last_error or ValueError("model reply contained no usable JSON object")


async def _chat_once(system: str, user: str) -> str:
    """One chat call, retried once if the transport fails.

    Ollama serialises requests per model, so generating while someone is
    querying can push a call past the client timeout. `httpx.ReadTimeout`
    carries an empty message, which is why these used to be recorded as an
    empty string. A transient timeout should cost a retry, not a pair.
    """
    try:
        return await ollama.chat(system, user)
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        log.warning("Ollama call failed (%s); retrying once", type(exc).__name__)
        return await ollama.chat(system, user)


# One initial attempt plus two reprompts. The model's failure mode is malformed
# JSON (a missing comma, an unterminated string), which is independent between
# attempts, so a second reprompt converts most remaining failures into pairs.
# Each attempt costs an LLM call, so the budget is small and fixed.
_GENERATION_ATTEMPTS = 3


async def _generate_pair(chunk: dict) -> dict:
    user_msg = f"Chunk:\n{chunk['content']}"
    data = None
    last_error: Exception | None = None
    for attempt in range(_GENERATION_ATTEMPTS):
        system = GS_SYSTEM if attempt == 0 else GS_SYSTEM + GS_RETRY_SUFFIX
        raw = await _chat_once(system, user_msg)
        try:
            data = _parse_gs_json(raw)
            break
        except Exception as exc:                      # noqa: BLE001
            last_error = exc
            log.info("Pair generation attempt %d/%d did not yield valid JSON: %s",
                     attempt + 1, _GENERATION_ATTEMPTS, exc)
    if data is None:
        raise last_error or ValueError("no usable reply from the model")

    return {
        "pair_id": f"p_{uuid.uuid4().hex[:8]}",
        "question": data.get("question", ""),
        "answer": data.get("answer", ""),
        "contexts": [chunk["content"]],
        "ground_truth": data.get("ground_truth", data.get("answer", "")),
        "source_file": chunk.get("source_file", ""),
        "chunk_index": chunk.get("chunk_index", 0),
        "status": "pending",
    }


async def _run_generation(session_id: str, chunks: list[dict]) -> None:
    cancelled = False
    try:
        for chunk in chunks:
            try:
                pair = await _generate_pair(chunk)
            except asyncio.CancelledError:
                cancelled = True
                raise
            except Exception as exc:
                reason = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
                log.warning("Gold-standard pair generation failed: %s", reason, exc_info=True)
                def failed(current):
                    current["pairs_failed"] = current.get("pairs_failed", 0) + 1
                    current.setdefault("errors", []).append(reason)
                    current["pairs_attempted"] = current.get("pairs_attempted", 0) + 1
                await asyncio.to_thread(_update_session_sync, session_id, failed)
            else:
                def completed(current):
                    current["pairs"].append(pair)
                    current["pairs_completed"] += 1
                    current["pairs_attempted"] = current.get("pairs_attempted", 0) + 1
                await asyncio.to_thread(_update_session_sync, session_id, completed)
    except asyncio.CancelledError:
        cancelled = True
        raise
    finally:
        def finish(current):
            if current.get("status") == "generating":
                current["status"] = ("cancelled" if cancelled else
                    "failed" if not current["pairs"] and current.get("errors") else "completed")
        await asyncio.to_thread(_update_session_sync, session_id, finish)


async def start_generation(
    collection: str,
    sample_size: int,
    seed: int | None,
) -> dict:
    all_chunks = await wc.sample_chunks(collection, limit=sample_size)
    actual_size = len(all_chunks)

    session_id = f"gs_{uuid.uuid4().hex[:8]}"
    session = {
        "session_id": session_id,
        "collection": collection,
        "status": "generating",
        "pairs_total": actual_size,
        # `attempted` drives progress and always reaches `total`; `completed`
        # counts pairs that actually exist. Reporting one number for both made
        # a session with a failed pair read "3/3" while holding 2.
        "pairs_attempted": 0,
        "pairs_completed": 0,
        "pairs_failed": 0,
        "pairs": [],
    }
    await _save_session(session)

    task = asyncio.create_task(_run_generation(session_id, all_chunks))
    _tasks.add(task)

    def _on_task_done(t: asyncio.Task) -> None:
        _tasks.discard(t)
        exc = t.exception() if not t.cancelled() else None
        if exc is not None:
            def failed(current):
                current["status"] = "failed"
                current.setdefault("errors", []).append(type(exc).__name__)
            try:
                _update_session_sync(session_id, failed)
            except GoldStandardError:
                log.exception("Could not durably report failed generation for %s", session_id)


    task.add_done_callback(_on_task_done)

    return {
        "session_id": session_id,
        "status": "generating",
        "pairs_total": actual_size,
        "pairs_completed": 0,
    }


async def update_pair(session_id: str, pair_id: str, updates: dict) -> dict | None:
    def change(current):
        for pair in current["pairs"]:
            if pair["pair_id"] == pair_id:
                pair.update({key: value for key, value in updates.items() if value is not None})
                return pair
        return None
    return await asyncio.to_thread(_update_session_sync, session_id, change)


async def regenerate_pair(session_id: str, pair_id: str) -> dict | None:
    session = get_session(session_id)
    if session is None:
        return None
    if session.get("status") == "generating":
        # The generation loop is appending to session["pairs"] and saving it;
        # regenerating underneath that races with it and can lose a pair.
        raise GoldStandardError(
            "GENERATION_IN_PROGRESS",
            f"Session '{session_id}' is still generating. Wait for it to finish "
            "before regenerating a pair.", 409)
    for i, pair in enumerate(session["pairs"]):
        if pair["pair_id"] == pair_id:
            chunk = {
                "content": pair["contexts"][0],
                "source_file": pair["source_file"],
                "chunk_index": pair["chunk_index"],
            }
            try:
                new_pair = await _generate_pair(chunk)
            except Exception as exc:                  # noqa: BLE001
                # The model regularly returns unparseable JSON. Generation
                # records that and moves on; regeneration used to let it escape
                # as a bare 500. Some of these carry an empty str(), so the
                # type name is always included or the message says nothing.
                reason = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
                log.warning("Regeneration of pair %s failed: %s", pair_id, reason, exc_info=True)
                raise GoldStandardError(
                    "PAIR_GENERATION_FAILED",
                    f"The model did not return a usable question/answer pair "
                    f"({reason}). The existing pair is unchanged; try again.", 502) from exc
            new_pair["pair_id"] = pair_id
            def replace(current):
                for position, existing in enumerate(current["pairs"]):
                    if existing["pair_id"] == pair_id:
                        if existing != pair or current.get("status") == "generating":
                            raise GoldStandardError("PAIR_CHANGED_DURING_REGENERATION",
                                "The pair changed while regeneration was running. Its acknowledged edits are preserved; refresh before retrying.", 409)
                        current["pairs"][position] = new_pair
                        return new_pair
                return None
            return await asyncio.to_thread(_update_session_sync, session_id, replace)
    return None


# Published cache snapshots are immutable. Export captures one stable reference
# and never mutates or exposes it; later commits publish a different object.
def _save_export_sync(out_path: Path, ragas: list[dict]) -> None:
    out_path.write_text(json.dumps(ragas, indent=2))


async def save_session(session_id: str, filename: str | None) -> dict | None:
    session = _sessions.get(session_id)
    if session is None:
        return None

    approved = [p for p in session["pairs"] if p["status"] in ("approved", "edited")]
    excluded = len(session["pairs"]) - len(approved)

    collection = session.get("collection", "export")
    if not filename:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        safe = re.sub(r"[^a-zA-Z0-9_]", "", collection.replace(" ", "_"))
        filename = f"{safe}_{ts}.json"

    # Sanitize: only the basename; no path traversal
    filename = Path(filename).name
    out_dir = Path(settings.upload_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = (out_dir / filename).resolve()
    if not str(out_path).startswith(str(out_dir) + "/"):
        raise ValueError("Invalid filename.")

    ragas = [
        {
            "question": p["question"],
            "answer": p["answer"],
            "contexts": p["contexts"],
            "ground_truth": p["ground_truth"],
        }
        for p in approved
    ]
    await asyncio.to_thread(_save_export_sync, out_path, ragas)

    return {
        "filename": filename,
        "pairs_saved": len(approved),
        "pairs_excluded": excluded,
        "download_url": f"/api/goldstandard/download/{filename}",
    }
