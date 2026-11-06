#!/usr/bin/env python3
# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Cassette record/replay helper for the CWR adapter gateway (spec/cwr/kernel.md section 7).

Python 3.12 standard library only. The on-disk format is specified in benchmark/cassettes/FORMAT.md.
This module is both a library (imported by benchmark/stubs/selftest.py and usable by drivers) and
a command line tool:

    cassette_tool.py hash ADAPTER PARAMS_JSON
    cassette_tool.py record --stub search=http://127.0.0.1:P --stub publish=http://127.0.0.1:Q \
                            --instance ID --requests REQ.jsonl --out CASSETTE.jsonl [--label L]
    cassette_tool.py replay --cassette CASSETTE.jsonl --requests REQ.jsonl
    cassette_tool.py verify CASSETTE.jsonl [...]
    cassette_tool.py digest DIR_OR_FILE [...]

A requests file is JSONL, one {"adapter": ..., "params": {...}, "world_time": N} object per line.

Replay never opens a socket: the replay path installs a guard that makes every socket connect
raise, and a cassette miss is reported as UNAVAILABLE, never forwarded to a network.

The reference normaliser here mirrors the gateway table in kernel.md section 7 for the refworld
adapters; the kernel's Rust gateway (task 2.3) is normative and must produce the same codes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import socket
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

CASSETTE_FORMAT = "cwr.cassette"
CASSETTE_SCHEMA_VERSION = 1
REQUEST_HASH_SCHEME = "sha256-cwr-canonical-json-v1"
SET_DIGEST_SCHEME = "sha256-cwr-cassette-set-v1"
ENTRY_KEYS = {"adapter", "request_hash", "seq", "params", "world_time", "response"}
HEADER_KEYS = {"format", "schema_version", "request_hash", "label"}
KEPT_HEADERS = ("content-type", "x-cwr-retry-after-ticks")
ADAPTER_RE = re.compile(r"[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*")

# Wire mapping shared with benchmark/stubs/stubcore.py.
CODE_FOR_STATUS = {
    400: "INVALID",
    404: "NOT_FOUND",
    409: "DUPLICATE_KEY",
    413: "LIMIT",
    422: "REJECTED",
    429: "RATE_LIMITED",
    503: "UNAVAILABLE",
}

# refworld adapter contracts as stated in spec/cwr/reference-world.md section 2.4 and
# spec/cwr/examples/refworld/adapters/*.kvx. retry_after_ratio converts service ticks to world ticks.
REFWORLD_ADAPTERS: dict[str, dict] = {
    "search.query": {
        "endpoint": "stub://search/v1/query",
        "returns": "list[SearchHit]",
        "timeout_ms": 3000,
        "idempotency": "none",
        "undeclared_status": "UNAVAILABLE",
        "retry_after_ratio": 1,
        "failures": ["INVALID", "LIMIT", "RATE_LIMITED", "UNAVAILABLE", "TIMEOUT"],
    },
    "publish.post": {
        "endpoint": "stub://publish/v1/post",
        "returns": "PostReceipt",
        "timeout_ms": 5000,
        "idempotency": "key",
        "undeclared_status": "UNAVAILABLE",
        "retry_after_ratio": 1,
        "failures": ["INVALID", "DUPLICATE_KEY", "RATE_LIMITED", "UNAVAILABLE", "TIMEOUT"],
    },
    "publish.status": {
        "endpoint": "stub://publish/v1/status",
        "returns": "PostStatus",
        "timeout_ms": 2000,
        "idempotency": "natural",
        "undeclared_status": "UNAVAILABLE",
        "retry_after_ratio": 1,
        "failures": ["NOT_FOUND", "UNAVAILABLE", "TIMEOUT"],
    },
    "publish.delete": {
        "endpoint": "stub://publish/v1/delete",
        "returns": "None",
        "timeout_ms": 2000,
        "idempotency": "natural",
        "undeclared_status": "UNAVAILABLE",
        "retry_after_ratio": 1,
        "failures": ["NOT_FOUND", "UNAVAILABLE", "TIMEOUT"],
    },
}

# Record shapes the stubs return (field name -> JSON type). manual/types.md must state the same.
SHAPES: dict[str, dict[str, type]] = {
    "SearchHit": {"rank": int, "title": str, "url": str, "snippet": str},
    "PostReceipt": {"post_id": str, "key": str, "accepted_at": int},
    "PostStatus": {"post_id": str, "key": str, "state": str, "accepted_at": int},
}


# ---------------------------------------------------------------------------------------------
# Canonical encoding and hashes


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def request_hash(adapter: str, params: dict) -> str:
    """Canonical request hash: sha256 over canonical JSON of {"adapter": ..., "params": ...}."""
    return (
        "sha256:"
        + hashlib.sha256(canonical_json({"adapter": adapter, "params": params})).hexdigest()
    )


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def set_digest(paths: Iterable[str | Path]) -> str:
    """Digest of a cassette set: sha256 over sorted "<relpath>\\t<file sha256>\\n" lines of every
    *.jsonl file under the given directories (or the given files, keyed by file name)."""
    lines = []
    for raw in paths:
        root = Path(raw)
        if root.is_dir():
            for path in sorted(root.rglob("*.jsonl")):
                lines.append(f"{path.relative_to(root).as_posix()}\t{file_sha256(path)}\n")
        else:
            lines.append(f"{root.name}\t{file_sha256(root)}\n")
    lines.sort()
    return "sha256:" + hashlib.sha256("".join(lines).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------------------------
# Cassette model


class CassetteError(ValueError):
    pass


class Cassette:
    """One cassette = the adapter traffic of one Instance, in call order."""

    def __init__(self, label: str | None = None):
        self.label = label
        self.entries: list[dict] = []
        self._ordinals: dict[tuple[str, str], int] = {}

    def header(self) -> dict:
        return {
            "format": CASSETTE_FORMAT,
            "schema_version": CASSETTE_SCHEMA_VERSION,
            "request_hash": REQUEST_HASH_SCHEME,
            "label": self.label,
        }

    def append(self, adapter: str, params: dict, response: dict, world_time: int | None) -> dict:
        digest = request_hash(adapter, params)
        seq = self._ordinals.get((adapter, digest), 0)
        self._ordinals[(adapter, digest)] = seq + 1
        entry = {
            "adapter": adapter,
            "request_hash": digest,
            "seq": seq,
            "params": params,
            "world_time": world_time,
            "response": response,
        }
        self.entries.append(entry)
        return entry

    def dumps(self) -> bytes:
        return b"".join(canonical_json(item) + b"\n" for item in [self.header(), *self.entries])

    def dump(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.dumps())

    @classmethod
    def load(cls, path: str | Path) -> Cassette:
        problems = verify_bytes(Path(path).read_bytes())
        if problems:
            raise CassetteError(f"{path}: {problems[0]}")
        lines = Path(path).read_bytes().decode("utf-8").splitlines()
        header = json.loads(lines[0])
        cassette = cls(header["label"])
        for line in lines[1:]:
            entry = json.loads(line)
            cassette.append(
                entry["adapter"], entry["params"], entry["response"], entry["world_time"]
            )
        return cassette


def _valid_response(response: Any) -> str | None:
    if not isinstance(response, dict) or "kind" not in response:
        return "response must be an object with kind"
    kind = response["kind"]
    if kind == "http":
        if set(response) != {"kind", "status", "headers", "body"}:
            return "http response keys must be kind, status, headers, body"
        if not isinstance(response["status"], int) or isinstance(response["status"], bool):
            return "http status must be an integer"
        headers = response["headers"]
        if not isinstance(headers, dict) or not all(
            isinstance(k, str) and isinstance(v, str) and k in KEPT_HEADERS
            for k, v in headers.items()
        ):
            return f"headers must map a subset of {KEPT_HEADERS} to strings"
        if not isinstance(response["body"], str):
            return "http body must be a string"
    elif kind == "timeout":
        if set(response) != {"kind"}:
            return "timeout response has only kind"
    elif kind == "transport_error":
        if set(response) != {"kind", "error"} or not isinstance(response["error"], str):
            return "transport_error response keys must be kind and error (string)"
    else:
        return f"unknown response kind {kind!r}"
    return None


def verify_bytes(data: bytes) -> list[str]:
    """Return a list of problems; empty means the cassette is well formed and canonical."""
    problems: list[str] = []
    if not data.endswith(b"\n"):
        return ["file must end with a newline"]
    try:
        lines = data.decode("utf-8").split("\n")[:-1]
    except UnicodeDecodeError:
        return ["file is not UTF-8"]
    if not lines:
        return ["empty cassette"]
    ordinals: dict[tuple[str, str], int] = {}
    last_time = -1
    for number, line in enumerate(lines, 1):
        try:
            value = json.loads(line)
        except ValueError as error:
            problems.append(f"line {number}: not JSON: {error}")
            continue
        if canonical_json(value) != line.encode("utf-8"):
            problems.append(f"line {number}: not in canonical encoding")
        if number == 1:
            if (
                not isinstance(value, dict)
                or set(value) != HEADER_KEYS
                or value["format"] != CASSETTE_FORMAT
                or value["schema_version"] != CASSETTE_SCHEMA_VERSION
                or value["request_hash"] != REQUEST_HASH_SCHEME
                or not (value["label"] is None or isinstance(value["label"], str))
            ):
                problems.append("line 1: bad header")
            continue
        if not isinstance(value, dict) or set(value) != ENTRY_KEYS:
            problems.append(f"line {number}: entry keys must be {sorted(ENTRY_KEYS)}")
            continue
        adapter, params = value["adapter"], value["params"]
        if not isinstance(adapter, str) or not ADAPTER_RE.fullmatch(adapter):
            problems.append(f"line {number}: adapter must be a qualified name")
            continue
        if not isinstance(params, dict):
            problems.append(f"line {number}: params must be an object")
            continue
        digest = request_hash(adapter, params)
        if value["request_hash"] != digest:
            problems.append(f"line {number}: request_hash does not match params")
        expected = ordinals.get((adapter, digest), 0)
        ordinals[(adapter, digest)] = expected + 1
        if value["seq"] != expected:
            problems.append(f"line {number}: seq {value['seq']} but expected {expected}")
        world_time = value["world_time"]
        if world_time is not None:
            if not isinstance(world_time, int) or isinstance(world_time, bool) or world_time < 0:
                problems.append(f"line {number}: world_time must be a non-negative integer or null")
            elif world_time < last_time:
                problems.append(f"line {number}: world_time decreased")
            else:
                last_time = world_time
        reason = _valid_response(value["response"])
        if reason:
            problems.append(f"line {number}: {reason}")
    return problems


# ---------------------------------------------------------------------------------------------
# Live calls (record mode) and replay


def resolve_endpoint(endpoint: str, stubs: dict[str, str]) -> str:
    """stub://publish/v1/post + {"publish": "http://127.0.0.1:8001"} -> http://127.0.0.1:8001/v1/post"""
    match = re.fullmatch(r"stub://([a-z_][a-z0-9_]*)(/.*)", endpoint)
    if not match:
        raise CassetteError(f"unsupported endpoint {endpoint!r}")
    server, path = match.groups()
    if server not in stubs:
        raise CassetteError(f"no base URL configured for stub server {server!r}")
    return stubs[server].rstrip("/") + path


def call_stub(
    url: str,
    params: dict,
    instance: str,
    world_time: int,
    timeout_ms: int,
    adapter: str | None = None,
) -> dict:
    """One HTTP call to a stub, returned in cassette response form. Never retries."""
    import http.client
    import urllib.parse

    parts = urllib.parse.urlsplit(url)
    body = canonical_json(params)
    headers = {
        "Content-Type": "application/json",
        "X-CWR-Instance": instance,
        "X-CWR-World-Time": str(world_time),
    }
    if adapter:
        headers["X-CWR-Adapter"] = adapter
    connection = http.client.HTTPConnection(parts.hostname, parts.port, timeout=timeout_ms / 1000.0)
    try:
        connection.request("POST", parts.path, body=body, headers=headers)
        reply = connection.getresponse()
        data = reply.read()
        kept = {
            name: reply.getheader(name)
            for name in KEPT_HEADERS
            if reply.getheader(name) is not None
        }
        return {
            "kind": "http",
            "status": reply.status,
            "headers": kept,
            "body": data.decode("utf-8", errors="replace"),
        }
    except TimeoutError:
        return {"kind": "timeout"}
    except (OSError, http.client.HTTPException) as error:
        return {"kind": "transport_error", "error": type(error).__name__}
    finally:
        connection.close()


class Recorder:
    """Record mode: forward each call to the stub and append the raw response to the cassette."""

    def __init__(
        self,
        cassette: Cassette,
        stubs: dict[str, str],
        instance: str,
        contracts: dict[str, dict] | None = None,
        timeouts_ms: dict[str, int] | None = None,
    ):
        self.cassette = cassette
        self.stubs = stubs
        self.instance = instance
        self.contracts = contracts or REFWORLD_ADAPTERS
        self.timeouts_ms = timeouts_ms or {}

    def call(self, adapter: str, params: dict, world_time: int) -> dict:
        contract = self.contracts[adapter]
        url = resolve_endpoint(contract["endpoint"], self.stubs)
        timeout_ms = self.timeouts_ms.get(adapter, contract["timeout_ms"])
        response = call_stub(url, params, self.instance, world_time, timeout_ms, adapter)
        self.cassette.append(adapter, params, response, world_time)
        return response


class Replayer:
    """Replay mode: serve entries by (adapter, request hash, sequence). None means a miss."""

    def __init__(self, cassette: Cassette):
        self.table: dict[tuple[str, str, int], dict] = {
            (e["adapter"], e["request_hash"], e["seq"]): e["response"] for e in cassette.entries
        }
        self.ordinals: dict[tuple[str, str], int] = {}

    def serve(self, adapter: str, params: dict) -> tuple[dict | None, str, int]:
        digest = request_hash(adapter, params)
        seq = self.ordinals.get((adapter, digest), 0)
        self.ordinals[(adapter, digest)] = seq + 1
        return self.table.get((adapter, digest, seq)), digest, seq


class NetworkForbidden(RuntimeError):  # noqa: N818 - name is fixed by cassettes/FORMAT.md section 4
    pass


def forbid_network() -> None:
    """Make every socket connect in this process raise. Used by replay; irreversible by design."""

    def refuse(*_args: Any, **_kwargs: Any):
        raise NetworkForbidden("replay mode opens no socket")

    socket.socket.connect = refuse  # type: ignore[method-assign]
    socket.socket.connect_ex = refuse  # type: ignore[method-assign]
    socket.create_connection = refuse  # type: ignore[assignment]


# ---------------------------------------------------------------------------------------------
# Reference normaliser (kernel.md section 7 gateway table)


def _shape_ok(returns: str, value: Any) -> bool:
    if returns == "None":
        return value is None
    match = re.fullmatch(r"list\[(\w+)\]", returns)
    if match:
        return isinstance(value, list) and all(_shape_ok(match.group(1), item) for item in value)
    fields = SHAPES.get(returns)
    if fields is None or not isinstance(value, dict) or set(value) != set(fields):
        return False
    return all(
        isinstance(value[name], kind) and not isinstance(value[name], bool)
        for name, kind in fields.items()
    )


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str) and value.strip().isdigit():
        number = int(value.strip())
        return number if number > 0 else None
    return None


def normalise(
    adapter: str, response: dict | None, contracts: dict[str, dict] | None = None
) -> dict:
    """Map a raw stub response (or None for a cassette miss) to an outcome.

    Returns {"status": "ok"|"error", "code", "retry_after", "shape", "value", "declared"} where
    declared is false when the code had to fall back to undeclared_status.
    """
    contract = (contracts or REFWORLD_ADAPTERS)[adapter]
    declared = set(contract["failures"])
    fallback = contract.get("undeclared_status", "UNAVAILABLE")

    def error(code: str, retry_after: int | None = None, value: Any = None) -> dict:
        is_declared = code in declared
        if not is_declared:
            code, retry_after = fallback, None
        return {
            "status": "error",
            "code": code,
            "retry_after": retry_after,
            "shape": contract["returns"],
            "value": value,
            "declared": is_declared,
        }

    if response is None:
        return error("UNAVAILABLE", value="cassette miss")
    if response["kind"] == "timeout":
        if "TIMEOUT" not in declared:
            # The gateway produces TIMEOUT itself; an adapter without it is a Build defect that the
            # compiler refuses (CWR-F002), never patched to the fallback (gateway/normalise.rs).
            return {
                "status": "error",
                "code": "INVARIANT_VIOLATION",
                "retry_after": None,
                "shape": contract["returns"],
                "value": None,
                "declared": False,
            }
        return error("TIMEOUT")
    if response["kind"] == "transport_error":
        return error("UNAVAILABLE")
    try:
        body = json.loads(response["body"])
    except ValueError:
        body = None
    status = response["status"]
    if 200 <= status < 300:
        if (
            isinstance(body, dict)
            and set(body) == {"result"}
            and _shape_ok(contract["returns"], body["result"])
        ):
            return {
                "status": "ok",
                "code": None,
                "retry_after": None,
                "shape": contract["returns"],
                "value": body["result"],
                "declared": True,
            }
        return error("INVALID")
    code = CODE_FOR_STATUS.get(status)
    if code is None:
        return error(fallback)
    if not isinstance(body, dict) or body.get("code") != code:
        return error("INVALID")
    if code == "RATE_LIMITED":
        # Same precedence as the gateway: the X-CWR-Retry-After-Ticks header, then the body field;
        # ticks = ceil(service value * retry_after_ratio), at least 1.
        retry_after = None
        for candidate in (
            response.get("headers", {}).get("x-cwr-retry-after-ticks"),
            body.get("retry_after"),
        ):
            retry_after = _positive_int(candidate)
            if retry_after is not None:
                break
        if retry_after is None:
            return error("INVALID")
        return error(
            code, retry_after=max(1, math.ceil(retry_after * contract.get("retry_after_ratio", 1)))
        )
    if code == "DUPLICATE_KEY":
        return error(code, value=body.get("receipt"))
    return error(code)


# ---------------------------------------------------------------------------------------------
# Command line


def _read_requests(path: str) -> list[dict]:
    requests = []
    for number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        item = json.loads(line)
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("adapter"), str)
            or not isinstance(item.get("params"), dict)
        ):
            raise CassetteError(f"{path}:{number}: need adapter and params")
        requests.append(item)
    return requests


def _pairs(values: list[str], what: str) -> dict[str, str]:
    result = {}
    for value in values or []:
        name, sep, rest = value.partition("=")
        if not sep or not name or not rest:
            raise CassetteError(f"{what} must be NAME=VALUE, got {value!r}")
        result[name] = rest
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CWR cassette record/replay helper")
    sub = parser.add_subparsers(dest="command", required=True)
    p_hash = sub.add_parser("hash", help="print the canonical request hash")
    p_hash.add_argument("adapter")
    p_hash.add_argument("params", help="JSON object of parameters")
    p_rec = sub.add_parser("record", help="forward requests to live stubs and write a cassette")
    p_rec.add_argument(
        "--stub",
        action="append",
        required=True,
        help="SERVER=BASE_URL, e.g. search=http://127.0.0.1:8001",
    )
    p_rec.add_argument("--instance", required=True)
    p_rec.add_argument("--requests", required=True)
    p_rec.add_argument("--out", required=True)
    p_rec.add_argument("--label")
    p_rec.add_argument(
        "--timeout-ms", action="append", help="ADAPTER=MS override of the contract timeout"
    )
    p_rep = sub.add_parser(
        "replay", help="serve requests from a cassette with the network disabled"
    )
    p_rep.add_argument("--cassette", required=True)
    p_rep.add_argument("--requests", required=True)
    p_ver = sub.add_parser(
        "verify", help="check cassette files for format, hashes, sequences and canonical form"
    )
    p_ver.add_argument("files", nargs="+")
    p_dig = sub.add_parser("digest", help="print the cassette set digest")
    p_dig.add_argument("paths", nargs="+")
    args = parser.parse_args(argv)

    try:
        if args.command == "hash":
            print(request_hash(args.adapter, json.loads(args.params)))
        elif args.command == "record":
            stubs = _pairs(args.stub, "--stub")
            timeouts = {k: int(v) for k, v in _pairs(args.timeout_ms, "--timeout-ms").items()}
            cassette = Cassette(args.label)
            recorder = Recorder(cassette, stubs, args.instance, timeouts_ms=timeouts)
            for item in _read_requests(args.requests):
                recorder.call(item["adapter"], item["params"], int(item.get("world_time", 0)))
            cassette.dump(args.out)
            print(
                json.dumps(
                    {
                        "out": args.out,
                        "entries": len(cassette.entries),
                        "file_sha256": file_sha256(Path(args.out)),
                    }
                )
            )
        elif args.command == "replay":
            cassette = Cassette.load(args.cassette)
            requests = _read_requests(args.requests)
            forbid_network()
            replayer = Replayer(cassette)
            misses = 0
            for item in requests:
                response, digest, seq = replayer.serve(item["adapter"], item["params"])
                misses += response is None
                print(
                    canonical_json(
                        {
                            "adapter": item["adapter"],
                            "request_hash": digest,
                            "seq": seq,
                            "hit": response is not None,
                            "outcome": normalise(item["adapter"], response),
                        }
                    ).decode("utf-8")
                )
            return 1 if misses else 0
        elif args.command == "verify":
            failed = False
            for name in args.files:
                problems = verify_bytes(Path(name).read_bytes())
                for problem in problems:
                    print(f"{name}: {problem}")
                failed |= bool(problems)
                if not problems:
                    print(f"{name}: ok")
            return 1 if failed else 0
        elif args.command == "digest":
            print(set_digest(args.paths))
    except (CassetteError, OSError, ValueError, KeyError) as error:
        print(f"cassette_tool: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
