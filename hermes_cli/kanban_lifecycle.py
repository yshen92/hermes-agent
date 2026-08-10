"""Bounded worker-to-dispatcher lifecycle result transport.

Restricted Kanban workers cannot open the board database.  Their only durable
board mutation is therefore a single complete/block result sent on the
dispatcher-created Unix datagram socket inherited as stdin. Linux
``SCM_CREDENTIALS`` binds each datagram to its real sender PID/UID, preventing
another same-UID worker from injecting a sibling result even if it can inspect
that sibling's processes. The dispatcher performs every database write itself
through the canonical Kanban finalizers.

This module deliberately contains no database access and no generic mutation
API.  The fixed restricted Unix identity configured outside Hermes is the hard
filesystem boundary; the marker and DB-layer guard are defense in depth.
"""
from __future__ import annotations

import json
import os
import socket
from typing import Any, Optional


RESTRICTED_WORKER_ENV = "HERMES_KANBAN_RESTRICTED_WORKER"
CONTEXT_ENV = "HERMES_KANBAN_CONTEXT"
MAX_RESULT_BYTES = 64 * 1024
MAX_CONTEXT_BYTES = 64 * 1024


def is_restricted_worker() -> bool:
    return os.environ.get(RESTRICTED_WORKER_ENV, "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def emit_result(action: str, payload: dict[str, Any]) -> None:
    """Emit one bounded result on the worker's inherited control socket.

    Identity is intentionally absent.  Task/run/profile/workspace/claim/PID
    authority comes exclusively from the dispatcher's spawn binding and live
    database state, never from worker-supplied strings.
    """
    if not is_restricted_worker():
        raise RuntimeError("restricted lifecycle result channel is not active")
    if action not in {"complete", "block"}:
        raise ValueError(f"unsupported lifecycle action: {action!r}")
    record = {"action": action, "payload": payload}
    encoded = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > MAX_RESULT_BYTES:
        raise ValueError(
            f"lifecycle result exceeds {MAX_RESULT_BYTES} byte limit"
        )
    try:
        channel = socket.socket(fileno=0)
        channel.send(encoded.encode("utf-8"))
        channel.detach()
    except OSError as exc:
        raise RuntimeError("restricted lifecycle control socket is unavailable") from exc


def parse_result(raw: bytes) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """Parse one complete datagram from the bounded lifecycle channel."""
    if not raw:
        return None, "missing lifecycle result"
    if len(raw) > MAX_RESULT_BYTES:
        return None, "lifecycle result exceeds size limit"
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, "malformed lifecycle result"
    if not isinstance(parsed, dict):
        return None, "lifecycle result must be an object"
    if set(parsed) != {"action", "payload"}:
        return None, "lifecycle result contains unsupported fields"
    if parsed.get("action") not in {"complete", "block"}:
        return None, "unsupported lifecycle action"
    if not isinstance(parsed.get("payload"), dict):
        return None, "lifecycle payload must be an object"
    return parsed, None


def bounded_context(value: str) -> str:
    """Return a UTF-8 context snapshot capped to a safe environment size."""
    raw = value.encode("utf-8")
    if len(raw) <= MAX_CONTEXT_BYTES:
        return value
    suffix = b"\n\n[worker context truncated by dispatcher]\n"
    kept = raw[: MAX_CONTEXT_BYTES - len(suffix)]
    return kept.decode("utf-8", errors="ignore") + suffix.decode("ascii")
