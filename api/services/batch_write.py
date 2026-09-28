"""Completed-batch checks shared by ingestion, import and tuning."""
from __future__ import annotations

import math
import uuid
from datetime import datetime, timezone
from weaviate.classes.query import Filter


class BatchVerificationError(RuntimeError):
    """A completed read proved that stored records differ from expectations."""


def _properties(value):
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, dict):
        return {key: _properties(item) for key, item in value.items() if item is not None}
    if isinstance(value, list):
        return [_properties(item) for item in value]
    return value


def _record_properties(value: dict) -> dict:
    value = dict(value)
    # Weaviate returns DATE properties as datetime objects, including for a
    # package whose source spelling was Z or another equivalent UTC offset.
    if isinstance(value.get("created_at"), str):
        value["created_at"] = datetime.fromisoformat(value["created_at"].replace("Z", "+00:00"))
    return _properties(value)


def _valid_vector(vector) -> bool:
    return isinstance(vector, list) and bool(vector) and all(
        isinstance(n, (int, float)) and not isinstance(n, bool) and math.isfinite(n)
        for n in vector)


def verify(collection, records: list[dict], *, exact: bool) -> int:
    """Confirm identities, properties and vectors; count alone cannot prove this."""
    expected = {str(uuid.UUID(str(record["id"]))): record for record in records}
    seen = set()
    def stored_objects():
        if exact:
            yield from collection.iterator(include_vector=True)
        else:
            ids = list(expected)
            for start in range(0, len(ids), 100):
                selected = ids[start:start + 100]
                yield from collection.query.fetch_objects(
                    filters=Filter.by_id().contains_any(selected),
                    limit=len(selected), include_vector=True).objects

    for obj in stored_objects():
        key = str(obj.uuid)
        if key not in expected:
            if exact:
                raise BatchVerificationError(f"Unexpected stored object {key}")
            continue
        if key in seen:
            raise BatchVerificationError(f"Duplicate stored object {key}")
        record = expected[key]
        if _record_properties(obj.properties or {}) != _record_properties(record["properties"]):
            raise BatchVerificationError(f"Stored properties differ for {key}")
        vector = (obj.vector or {}).get("default")
        if not _valid_vector(vector):
            raise BatchVerificationError(f"Stored vector is missing or invalid for {key}")
        requested = record.get("vector")
        if requested is not None and (len(requested) != len(vector) or not all(
                math.isclose(a, b, rel_tol=1e-5, abs_tol=1e-6)
                for a, b in zip(requested, vector))):
            raise BatchVerificationError(f"Stored vector differs for {key}")
        seen.add(key)
    if seen != expected.keys():
        raise BatchVerificationError(f"Confirmed {len(seen)} of {len(expected)} expected objects")
    return len(seen)


def insert(collection, records, *, exact: bool = True, expected_count: int | None = None) -> int:
    """Enqueue once, wait for the final flush, then verify persisted records.

    Explicit UUIDs prevent a caller's attempted count from hiding duplicate IDs.
    Generated vectors are checked for validity; supplied vectors are compared
    with float32 storage tolerance. No progress is published before confirmation.
    """
    prepared = []
    identities = set()
    for item in records:
        record = dict(item)
        key = str(uuid.UUID(str(record["id"]))) if "id" in record else str(uuid.uuid4())
        if key in identities:
            raise ValueError(f"Duplicate chunk UUID {key}")
        identities.add(key)
        record["id"] = key
        if "vector" in record and not _valid_vector(record["vector"]):
            raise ValueError(f"Missing or invalid vector for {key}")
        _record_properties(record["properties"])
        prepared.append(record)
    if expected_count is not None and len(prepared) != expected_count:
        raise ValueError(f"Package contains {len(prepared)} objects but declares {expected_count}")
    with collection.batch.dynamic() as batch:
        for record in prepared:
            batch.add_object(properties=record["properties"], uuid=record["id"],
                             vector=record.get("vector"))
    # failed_objects belongs to the wrapper and is reset per dynamic() call.
    # It includes errors from the final flush, unlike an in-context check.
    failed = collection.batch.failed_objects
    if failed:
        raise RuntimeError(f"Weaviate rejected {len(failed)} batch object(s)")
    return verify(collection, prepared, exact=exact)
