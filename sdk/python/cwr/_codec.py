# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""CWR SDK codec: the value codec, the per-call result envelope and the turn result envelope.

Spec: spec/cwr/model-surface.md section 4, spec/cwr/kernel.md sections 5 and 8, spec/cwr/world-source.md
section 6. This file is shipped byte-for-byte inside every Build's ``sdk/cwr/`` by the compiler (task 1.3);
it is part of the Build hash.

Value codec (pinned here, decisions.md "Decisions required during implementation"):

* ``None``, ``bool``, ``int`` and ``str`` are JSON literals. Finite ``float`` values pass as JSON numbers
  (no contract type is a float; the kernel refuses them as INVALID).
* ``bytes`` and ``bytearray`` become ``{"$bytes": "<standard base64>"}``.
* ``list`` and ``tuple`` become JSON arrays.
* ``dict`` with ``str`` keys becomes a JSON object; a key that begins with ``$`` is escaped by one more
  leading ``$`` (``"$x"`` travels as ``"$$x"``), so a single-``$`` key is always a codec tag.
* Anything else (other key types, sets, non-finite floats, objects) becomes ``{"$invalid": "<type name>"}``.
  The SDK never refuses a call on its own: the value still crosses the RPC boundary, the kernel refuses
  it as INVALID at step 1 and the refusal is in the ledger.

The serialised form is canonical JSON: keys sorted by code point (equal to UTF-8 byte order), separators
``,`` and ``:`` with no whitespace, non-ASCII characters written as UTF-8. For values without bytes this
is byte-identical to the kernel's ``canonical_json``, which ``params_hash`` hashes.

Per-call result envelope, the response of one RPC (one primitive call):

``{"status": "ok", "value": <codec value>, "content_guaranteed": <bool>}`` or
``{"status": "error", "error": <failure>, "content_guaranteed": <bool>}``, where a failure is
``{"code", "primitive", "precondition_index", "retry_after", "limit", "detail"}``. ``retry_after`` is an
integer number of ticks for RATE_LIMITED and null otherwise; ``limit`` names the limit for LIMIT.
``content_guaranteed`` is false exactly for adapter-backed (external) primitives (clause 2.5).

Turn result envelope: ``status``, ``returned``, ``error``, ``trace``, ``world``, ``resources`` as in
model-surface.md section 4. Each trace item additionally carries ``content_guaranteed`` so the
content-not-guaranteed label is present in every envelope that reports an adapter call (clause 2.5).
"""

from __future__ import annotations

import base64
import json
import math

__all__ = [
    "CODES",
    "TURN_KEYS",
    "CodecError",
    "call_error",
    "call_ok",
    "check_call_result",
    "check_failure",
    "check_turn_envelope",
    "decode",
    "dumps",
    "dumps_bytes",
    "encode",
    "failure",
    "loads",
    "loads_bytes",
    "trace_item",
    "turn_envelope",
]

# The closed failure envelope (clause 2.3), in the order of world-source.md section 6.
CODES = (
    "NOT_FOUND",
    "LOCKED",
    "CONFLICT",
    "INVALID",
    "LIMIT",
    "UNAVAILABLE",
    "RATE_LIMITED",
    "TIMEOUT",
    "REJECTED",
    "DUPLICATE_KEY",
    "INVARIANT_VIOLATION",
)

FAILURE_KEYS = ("code", "primitive", "precondition_index", "retry_after", "limit", "detail")
TURN_KEYS = ("status", "returned", "error", "trace", "world", "resources")
TRACE_KEYS = ("primitive", "status", "code", "world_time", "content_guaranteed")
WORLD_KEYS = ("version", "build", "time", "turn")
RESOURCE_KEYS = ("cpu_ms", "wall_ms", "primitive_calls", "adapter_calls", "remaining")


class CodecError(ValueError):
    """A wire value or envelope that does not follow this codec."""


# ----------------------------------------------------------------------------- values


def encode(value):
    """Python value to its JSON-compatible codec form."""
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return value if math.isfinite(value) else {"$invalid": "float"}
    if isinstance(value, (bytes, bytearray)):
        return {"$bytes": base64.b64encode(bytes(value)).decode("ascii")}
    if isinstance(value, (list, tuple)):
        return [encode(v) for v in value]
    if isinstance(value, dict):
        if not all(isinstance(k, str) for k in value):
            return {"$invalid": "dict"}
        return {("$" + k if k.startswith("$") else k): encode(v) for k, v in value.items()}
    return {"$invalid": type(value).__name__}


def decode(value):
    """Codec form to the Python value. Raises CodecError on an unknown tag."""
    if isinstance(value, list):
        return [decode(v) for v in value]
    if isinstance(value, dict):
        tags = [k for k in value if k.startswith("$") and not k.startswith("$$")]
        if tags:
            if len(value) != 1:
                raise CodecError(f"codec tag {tags[0]!r} must be the only key of its object")
            tag = tags[0]
            if tag == "$bytes" and isinstance(value[tag], str):
                try:
                    return base64.b64decode(value[tag].encode("ascii"), validate=True)
                except (ValueError, UnicodeEncodeError) as e:
                    raise CodecError(f"bad $bytes payload: {e}") from None
            raise CodecError(f"unknown codec tag {tag!r}")
        return {(k[1:] if k.startswith("$$") else k): decode(v) for k, v in value.items()}
    return value


def dumps(value) -> str:
    """Canonical JSON text of a Python value."""
    return json.dumps(
        encode(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def loads(text):
    """Python value of canonical JSON text (str or UTF-8 bytes).

    Raises ``ValueError`` (``json.JSONDecodeError``, ``UnicodeDecodeError``) on text that is not JSON
    and CodecError on an unknown or malformed codec tag.
    """
    if isinstance(text, (bytes, bytearray)):
        text = bytes(text).decode("utf-8")
    return decode(json.loads(text))


def dumps_bytes(value) -> bytes:
    """UTF-8 bytes of ``dumps(value)``: the payload of one RPC frame."""
    return dumps(value).encode("utf-8")


def loads_bytes(data: bytes):
    """Python value of one RPC frame payload; raises as ``loads``."""
    return loads(data)


# ----------------------------------------------------------------------------- envelopes


def _int_or_none(v, what):
    """Raises CodecError unless ``v`` is None or an integer that is not a bool."""
    if v is not None and (isinstance(v, bool) or not isinstance(v, int)):
        raise CodecError(f"{what} must be an integer or null, got {v!r}")


def failure(code, primitive, precondition_index=None, retry_after=None, limit=None, detail=""):
    """A failure value with the fixed key set; validates the envelope rules."""
    f = {
        "code": code,
        "primitive": primitive,
        "precondition_index": precondition_index,
        "retry_after": retry_after,
        "limit": limit,
        "detail": detail,
    }
    check_failure(f)
    return f


def check_failure(f) -> None:
    """Raises CodecError unless ``f`` is a failure value.

    A failure value has exactly the failure keys, a code from the closed envelope, a string
    primitive, a one-based precondition_index or null, retry_after exactly for RATE_LIMITED, limit
    only for LIMIT, and a string detail.
    """
    if not isinstance(f, dict) or tuple(sorted(f)) != tuple(sorted(FAILURE_KEYS)):
        raise CodecError(f"failure must have exactly the keys {FAILURE_KEYS}, got {f!r}")
    if f["code"] not in CODES:
        raise CodecError(f"code {f['code']!r} is outside the closed failure envelope")
    if not isinstance(f["primitive"], str):
        raise CodecError("failure primitive must be a string")
    _int_or_none(f["precondition_index"], "precondition_index")
    if f["precondition_index"] is not None and f["precondition_index"] < 1:
        raise CodecError("precondition_index is one-based")
    _int_or_none(f["retry_after"], "retry_after")
    if f["code"] == "RATE_LIMITED":
        if f["retry_after"] is None or f["retry_after"] < 0:
            raise CodecError("RATE_LIMITED carries retry_after in ticks")
    elif f["retry_after"] is not None:
        raise CodecError("retry_after is present only for RATE_LIMITED")
    if f["limit"] is not None and (f["code"] != "LIMIT" or not isinstance(f["limit"], str)):
        raise CodecError("limit is a limit name and only for LIMIT")
    if not isinstance(f["detail"], str):
        raise CodecError("detail must be a string")


def call_ok(value, content_guaranteed=True):
    """Per-call result envelope for an ok call, in codec form."""
    return {"status": "ok", "value": encode(value), "content_guaranteed": bool(content_guaranteed)}


def call_error(fail, content_guaranteed=True):
    """Per-call result envelope for a refused call; raises CodecError if ``fail`` is not a failure."""
    check_failure(fail)
    return {"status": "error", "error": fail, "content_guaranteed": bool(content_guaranteed)}


def check_call_result(r) -> None:
    """Validates a per-call result envelope in codec form."""
    if not isinstance(r, dict) or not isinstance(r.get("content_guaranteed"), bool):
        raise CodecError(f"call result must be an object with a boolean content_guaranteed: {r!r}")
    if r.get("status") == "ok":
        if set(r) != {"status", "value", "content_guaranteed"}:
            raise CodecError(f"ok call result keys: {sorted(r)}")
    elif r.get("status") == "error":
        if set(r) != {"status", "error", "content_guaranteed"}:
            raise CodecError(f"error call result keys: {sorted(r)}")
        check_failure(r["error"])
    else:
        raise CodecError(f"call result status must be ok or error: {r.get('status')!r}")


def trace_item(primitive, status, code, world_time, content_guaranteed=True):
    """One trace item of the turn result envelope, validated; raises CodecError on a bad item."""
    item = {
        "primitive": primitive,
        "status": status,
        "code": code,
        "world_time": world_time,
        "content_guaranteed": bool(content_guaranteed),
    }
    _check_trace_item(item)
    return item


def _check_trace_item(t) -> None:
    """Raises CodecError unless ``t`` is a trace item with exactly the trace keys."""
    if not isinstance(t, dict) or set(t) != set(TRACE_KEYS):
        raise CodecError(f"trace item must have exactly the keys {TRACE_KEYS}: {t!r}")
    if t["status"] not in ("ok", "error"):
        raise CodecError("trace status must be ok or error")
    if (t["status"] == "ok") != (t["code"] is None):
        raise CodecError("trace code is null exactly when status is ok")
    if t["code"] is not None and t["code"] not in CODES:
        raise CodecError(f"trace code {t['code']!r} is outside the envelope")
    _int_or_none(t["world_time"], "world_time")
    if not isinstance(t["content_guaranteed"], bool):
        raise CodecError("content_guaranteed must be a boolean")


def turn_envelope(status, returned, error, trace, world, resources):
    """The turn result envelope of model-surface.md section 4, in codec form, validated."""
    env = {
        "status": status,
        "returned": encode(returned),
        "error": error,
        "trace": list(trace),
        "world": world,
        "resources": resources,
    }
    check_turn_envelope(env)
    return env


def check_turn_envelope(env) -> None:
    """Raises CodecError unless ``env`` is a turn result envelope in codec form.

    The envelope has exactly the turn keys, a status of ok, error or partial, a failure exactly when
    the status is not ok, valid trace items, the world and resources key sets, and a decodable
    returned value.
    """
    if not isinstance(env, dict) or set(env) != set(TURN_KEYS):
        raise CodecError(f"turn envelope must have exactly the keys {TURN_KEYS}")
    if env["status"] not in ("ok", "error", "partial"):
        raise CodecError("turn status must be ok, error or partial")
    if (env["status"] == "ok") != (env["error"] is None):
        raise CodecError("error is null exactly when status is ok")
    if env["error"] is not None:
        check_failure(env["error"])
    if not isinstance(env["trace"], list):
        raise CodecError("trace must be a list")
    for t in env["trace"]:
        _check_trace_item(t)
    w = env["world"]
    if not isinstance(w, dict) or set(w) != set(WORLD_KEYS):
        raise CodecError(f"world must have exactly the keys {WORLD_KEYS}")
    r = env["resources"]
    if not isinstance(r, dict) or set(r) != set(RESOURCE_KEYS):
        raise CodecError(f"resources must have exactly the keys {RESOURCE_KEYS}")
    decode(env["returned"])
