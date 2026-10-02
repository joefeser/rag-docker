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
from models.schemas import SessionResponse
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
_scan_lock = threading.Lock()
_store_revision = 0
_SESSION_ID = re.compile(r"gs_[0-9a-f]{8}")


def validate_session_id(session_id: str) -> None:
    """Imported identities use the same grammar as locally generated ones."""
    if not isinstance(session_id, str) or not _SESSION_ID.fullmatch(session_id):
        raise ValueError("Invalid evaluation session ID.")


def validate_session(session: dict) -> None:
    """Validate without normalising or dropping historical validity metadata."""
    SessionResponse.model_validate(session, strict=True)
    validate_session_id(session["session_id"])


def _session_storage_root() -> Path:
    upload = Path(settings.upload_dir).resolve()
    p = upload / "goldstandard_sessions"
    if p.is_symlink() or (p.exists() and not p.is_dir()):
        raise ValueError("Evaluation session storage is not a regular directory.")
    root = p.resolve()
    if root.parent != upload:
        raise ValueError("Evaluation session storage is outside the upload directory.")
    return root


def _sessions_dir() -> Path:
    p = _session_storage_root()
    created = not p.exists()
    p.mkdir(parents=True, exist_ok=True)
    if created:
        _sync_directory(p.parent)
    return p


def _session_path(session_id: str) -> Path:
    validate_session_id(session_id)
    # Preflight must be read-only; the writer creates the directory only after
    # every imported session has been checked.
    root = _session_storage_root()
    candidate = root / f"{session_id}.json"
    if candidate.is_symlink() or (candidate.exists() and not candidate.is_file()):
        raise ValueError("Evaluation session destination is not a regular file.")
    target = candidate.resolve()
    # Grammar prevents metadata-derived paths; containment also refuses an
    # existing file symlink which would redirect a valid identity's write.
    if target.parent != root:
        raise ValueError("Evaluation session destination is outside session storage.")
    return target


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
    global _store_revision
    with _state_lock:
        snapshot = copy.deepcopy(session)
        try:
            path = _session_path(snapshot["session_id"])
            _sessions_dir()
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
            _store_revision += 1
            _sync_directory(path.parent)
            if snapshot.get("persistence_error"):
                _record_issue(path, snapshot["persistence_error"]["code"])
            else:
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


def _scan_sessions() -> None:
    """Inspect disk without blocking writers, then publish only a current scan."""
    global _store_revision
    with _scan_lock:
        with _state_lock:
            revision = _store_revision
        storage_label = Path(settings.upload_dir) / "goldstandard_sessions"
        loaded = {}
        issues = {}
        try:
            root = _session_storage_root()
            with os.scandir(root) as entries:
                paths = [Path(entry.path) for entry in entries]
        except FileNotFoundError:
            paths = []
        except (OSError, ValueError, RuntimeError):
            with _state_lock:
                if revision == _store_revision:
                    _record_issue(storage_label, "SESSION_STORAGE_UNAVAILABLE")
            return
        for path in paths:
            if re.fullmatch(r"\.gs_[0-9a-f]{8}-.+\.tmp", path.name):
                issues[path] = "SESSION_INTERRUPTED_WRITE"
            elif path.name.endswith(".json"):
                try:
                    if path.is_symlink() or not path.is_file():
                        raise ValueError("Evaluation session must be a regular file.")
                    data = json.loads(path.read_text())
                    validate_session(data)
                    if path != _session_path(data["session_id"]):
                        raise ValueError("Evaluation session filename does not match its identity.")
                    loaded[data["session_id"]] = data
                    retained_error = data.get("persistence_error")
                    if isinstance(retained_error, dict) and retained_error.get("code") in ("SESSION_WRITE_FAILED", "SESSION_DURABILITY_UNCERTAIN"):
                        issues[path] = retained_error["code"]
                except (OSError, ValueError, TypeError):
                    issues[path] = "SESSION_READ_FAILED"
        with _state_lock:
            # A concurrent durable commit wins over an older inspection, including
            # its cache and write-failure diagnostics. The next refresh rescans.
            if revision != _store_revision:
                return
            _diagnostics.pop(str(storage_label), None)
            present = {str(path) for path in paths}
            for key, issue in list(_diagnostics.items()):
                if issue["code"] in ("SESSION_READ_FAILED", "SESSION_INTERRUPTED_WRITE") and Path(key).parent == root and (key not in present or Path(key) not in issues):
                    _diagnostics.pop(key, None)
            for sid, data in loaded.items():
                if sid not in _sessions:
                    _sessions[sid] = data
                    _store_revision += 1
            for path, code in issues.items():
                # Preserve the independent failed-write policy at the same path.
                if _diagnostics.get(str(path), {}).get("code") not in ("SESSION_WRITE_FAILED", "SESSION_DURABILITY_UNCERTAIN"):
                    _record_issue(path, code)


def load_sessions_from_disk() -> None:
    _scan_sessions()


def _sessions_on_disk() -> list[dict]:
    """Return detached, validated files for export without changing the cache."""
    try:
        paths = sorted(_session_storage_root().glob("*.json"))
    except (OSError, ValueError, RuntimeError):
        log.exception("Cannot read evaluation session storage; existing files are unchanged")
        return []
    sessions = []
    for path in paths:
        try:
            if path.is_symlink() or not path.is_file():
                raise ValueError("Evaluation session must be a regular file.")
            data = json.loads(path.read_text())
            validate_session(data)
            if path != _session_path(data["session_id"]):
                raise ValueError("Evaluation session filename does not match its identity.")
        except (OSError, ValueError, RuntimeError) as exc:
            log.warning("Skipping invalid evaluation session %s; file is unchanged (%s)",
                        path.name, type(exc).__name__)
            continue
        sessions.append(data)
    return sessions


def session_diagnostics() -> list[dict]:
    _scan_sessions()
    with _state_lock:
        return copy.deepcopy([_diagnostics[key] for key in sorted(_diagnostics)])


def sessions_for(collection: str) -> list[dict]:
    _scan_sessions()
    with _state_lock:
        found = []
        for sid, session in _sessions.items():
            if session.get("collection") != collection:
                continue
            try:
                from models.schemas import SessionResponse
                validate_session(session)
                if sid != session.get("session_id"):
                    raise ValueError("Cached session key does not match identity")
                _session_path(sid)
            except (OSError, ValueError, RuntimeError) as exc:
                log.warning("Skipping invalid cached evaluation session %s (%s)",
                            sid, type(exc).__name__)
                _record_issue(Path(str(sid) + ".json"), "SESSION_READ_FAILED")
                continue
            found.append(session)
        return copy.deepcopy(found)


def store_session(session: dict) -> None:
    """Explicit whole-session storage; publishes cache only after replacement."""
    validate_session(session)
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
    sessions = sessions_for(collection)
    now = datetime.now(timezone.utc).isoformat()
    marked = 0
    for session in sessions:
        def change(current):
            current[flag] = True
            current[f"{flag}_reason"] = reason
            current[f"{flag}_at"] = now
        try:
            _update_session_sync(session["session_id"], change)
            marked += 1
        except (GoldStandardError, OSError, ValueError, RuntimeError):
            # The primary collection mutation already happened. Preserve the
            # failed-write diagnostic and continue other markers; never turn a
            # completed delete/rebuild into a fictitious primary failure.
            log.exception("Could not durably mark session %s %s", session["session_id"], flag)
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
        "question": str(data.get("question", "")),
        "answer": str(data.get("answer", "")),
        "contexts": [chunk["content"]],
        "ground_truth": str(data.get("ground_truth", data.get("answer", ""))),
        "source_file": chunk.get("source_file", ""),
        "chunk_index": int(chunk.get("chunk_index", 0)),
        "status": "pending",
    }


def _failed_generation(current: dict, exc: Exception) -> None:
    current["status"] = "failed"
    reason = (f"{exc.code}: {exc.message}" if isinstance(exc, GoldStandardError)
              else f"{type(exc).__name__}: {exc}")
    errors = current.setdefault("errors", [])
    if reason not in errors:
        errors.append(reason)
    if isinstance(exc, GoldStandardError):
        current["persistence_error"] = {"code": exc.code, "message": exc.message}


async def _record_generation_failure(session_id: str, exc: Exception) -> None:
    try:
        await asyncio.to_thread(_update_session_sync, session_id,
                                lambda current: _failed_generation(current, exc))
    except Exception:
        log.exception("Could not durably report failed generation for %s", session_id)


async def _run_generation(session_id: str, chunks: list[dict]) -> None:
    cancelled = False
    persistence_failure = None
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
    except GoldStandardError as exc:
        persistence_failure = exc
        raise
    except asyncio.CancelledError:
        cancelled = True
        raise
    finally:
        def finish(current):
            if persistence_failure is not None:
                _failed_generation(current, persistence_failure)
                return
            if current.get("status") == "generating":
                current["status"] = ("cancelled" if cancelled else
                    "failed" if not current["pairs"] and current.get("errors") else "completed")
        await asyncio.to_thread(_update_session_sync, session_id, finish)


async def start_generation(
    collection: str,
    sample_size: int,
    seed: int | None,
) -> dict:
    from models.schemas import GenerateRequest
    request = GenerateRequest(collection=collection, sample_size=sample_size, seed=seed)
    all_chunks = await wc.sample_chunks(collection, limit=request.sample_size, seed=request.seed)
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
            reporter = asyncio.create_task(_record_generation_failure(session_id, exc))
            _tasks.add(reporter)
            reporter.add_done_callback(_tasks.discard)

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


async def save_session(session_id: str, filename: str | None, allow_historical: bool = False) -> dict | None:
    session = _sessions.get(session_id)
    if session is None:
        return None

    from models.schemas import SessionValidity
    if not isinstance(allow_historical, bool):
        raise ValueError("allow_historical must be a boolean")
    validity = SessionValidity.model_validate(session).model_dump()
    historical = validity["stale"] or validity["orphaned"]
    if historical and not allow_historical:
        raise GoldStandardError(
            "HISTORICAL_SESSION",
            "This retained session is stale or orphaned and is not a current "
            "collection baseline. Inspect its validity metadata and explicitly "
            "set allow_historical=true to export historical pairs.", 409)

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
        "historical": historical,
        "session_validity": validity,
    }
