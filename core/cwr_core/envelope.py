# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""The closed eleven-code CWR failure envelope as data, with a validator for envelope JSON.

Source of truth: `spec/cwr/world-source.md` section 6 and the `cwr-contract` crate
(`crates/cwr-contract/src/envelope.rs`). The envelope value is a JSON object with exactly the keys
`code`, `primitive`, `precondition`, `limit` and `detail`, plus `retry_after` if and only if the code
is `RATE_LIMITED`. A code outside `CODES` is never valid; adding one is a specification change.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

__all__ = [
    "BY_NAME",
    "CODES",
    "LIMIT_CODES",
    "RETRY_AFTER_CODES",
    "Code",
    "EnvelopeError",
    "loads",
    "validate",
]


@dataclass(frozen=True)
class Code:
    """One envelope code: its name, whether it carries `retry_after`, and its manual guidance."""

    name: str
    retry_after: bool
    guidance: str


CODES: tuple[Code, ...] = (
    Code("NOT_FOUND", False, "Query the visible existence primitive first."),
    Code("LOCKED", False, "Unlock, or wait for the holder."),
    Code("CONFLICT", False, "Re-check the named conditions and retry once."),
    Code("INVALID", False, "Fix the call; retrying unchanged cannot succeed."),
    Code("LIMIT", False, "Split the work across turns or reduce the size."),
    Code("UNAVAILABLE", False, "The world does not retry; the script may, on a later turn."),
    Code("RATE_LIMITED", True, "Wait retry_after ticks before calling the same adapter."),
    Code(
        "TIMEOUT",
        False,
        "The effect may or may not have happened; use the adapter's status primitive if it has one.",
    ),
    Code(
        "REJECTED",
        False,
        "Change the content; detail carries the service's reason as content, not guaranteed.",
    ),
    Code("DUPLICATE_KEY", False, "Reuse the original receipt or choose a new key."),
    Code(
        "INVARIANT_VIOLATION",
        False,
        "Not an ordinary outcome. The ledger carries the trace and the Build is considered defective.",
    ),
)
BY_NAME: dict[str, Code] = {code.name: code for code in CODES}
RETRY_AFTER_CODES: frozenset[str] = frozenset(code.name for code in CODES if code.retry_after)
LIMIT_CODES: frozenset[str] = frozenset({"LIMIT"})

REQUIRED_KEYS = ("code", "primitive", "precondition", "limit", "detail")
U32_MAX = 2**32 - 1
U64_MAX = 2**64 - 1


class EnvelopeError(ValueError):
    """Envelope text that is not strict JSON or not a valid envelope; `errors` lists every reason."""

    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _optional_str(value: Any) -> bool:
    return value is None or isinstance(value, str)


def validate(value: Any) -> list[str]:
    """Return every reason `value` is not a valid envelope object; an empty list means valid."""
    if not isinstance(value, dict):
        return ["envelope must be a JSON object"]
    errors: list[str] = []
    errors += [f"missing key {key}" for key in REQUIRED_KEYS if key not in value]
    allowed = {*REQUIRED_KEYS, "retry_after"}
    errors += [f"unknown key {key}" for key in sorted(set(value) - allowed, key=str)]

    raw_code = value.get("code")
    # An unhashable or non-string code is reported once and treated as unknown below.
    code = raw_code if isinstance(raw_code, str) else None
    if "code" in value and code not in BY_NAME:
        errors.append(f"{raw_code!r} is not one of the eleven envelope codes")
    if not _optional_str(value.get("primitive")):
        errors.append("primitive must be a string or null")

    precondition = value.get("precondition")
    if precondition is not None and not (_is_int(precondition) and 1 <= precondition <= U32_MAX):
        errors.append("precondition must be null or a one-based integer index")

    if "retry_after" in value:
        ticks = value["retry_after"]
        if code not in RETRY_AFTER_CODES:
            errors.append(f"retry_after is allowed only on RATE_LIMITED, not on {code}")
        elif not (_is_int(ticks) and 0 <= ticks <= U64_MAX):
            errors.append("retry_after must be a non-negative integer number of ticks")
    elif code in RETRY_AFTER_CODES:
        errors.append("RATE_LIMITED requires retry_after in ticks")

    limit = value.get("limit")
    if not _optional_str(limit):
        errors.append("limit must be a string or null")
    elif limit is not None and code in BY_NAME and code not in LIMIT_CODES:
        errors.append(f"limit is allowed only on LIMIT, not on {code}")

    if not _optional_str(value.get("detail")):
        errors.append("detail must be a string or null")
    return errors


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, item in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key {key}")
        result[key] = item
    return result


def _invalid_constant(name: str) -> Any:
    raise ValueError(f"non-JSON numeric constant {name}")


def loads(text: str) -> dict[str, Any]:
    """Parse envelope JSON strictly and validate it; raise EnvelopeError listing every reason."""
    try:
        value = json.loads(text, object_pairs_hook=_unique_pairs, parse_constant=_invalid_constant)
    except ValueError as error:
        raise EnvelopeError([f"not strict JSON: {error}"]) from error
    errors = validate(value)
    if errors:
        raise EnvelopeError(errors)
    return value
