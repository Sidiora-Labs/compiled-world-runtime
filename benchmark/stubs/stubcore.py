# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Shared core of the refworld stub servers (search_server.py, publish_server.py).

Python 3.12 standard library only. The stubs back the refworld adapters described in
spec/cwr/reference-world.md section 4 and the gateway table in spec/cwr/kernel.md section 7.

Determinism contract: given equal seed, equal schedule and an equal sequence of requests
(body, X-CWR-Instance and X-CWR-World-Time headers), a stub produces byte-identical responses.
No response depends on host time, host identity, process identity or thread scheduling. Rate
limits are evaluated over world_time ticks taken from the X-CWR-World-Time request header.

The wire protocol is documented in benchmark/stubs/README.md.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import http.server
import json
import re
import socket
import struct
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SCHEDULE_FORMAT = "cwr.stub.schedule"
SCHEDULE_SCHEMA_VERSION = 1

# The closed failure envelope (spec.kvx req.2 ac_3). The stubs never emit a code outside it.
ENVELOPE = (
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

# Wire mapping: HTTP status <-> envelope code. TIMEOUT has no status: it is a delayed response.
STATUS_FOR_CODE = {
    "INVALID": 400,
    "NOT_FOUND": 404,
    "DUPLICATE_KEY": 409,
    "LIMIT": 413,
    "REJECTED": 422,
    "RATE_LIMITED": 429,
    "UNAVAILABLE": 503,
}
CODE_FOR_STATUS = {status: code for code, status in STATUS_FOR_CODE.items()}

# Injectable faults and the envelope code the gateway is expected to produce for each.
FAULT_WIRE_CODE = {
    "UNAVAILABLE": "UNAVAILABLE",  # HTTP 503, no side effect
    "TIMEOUT": "TIMEOUT",  # side effect completed, response delayed by delay_ms
    "RATE_LIMITED": "RATE_LIMITED",  # HTTP 429 with retry_after ticks, no side effect
    "REJECTED": "REJECTED",  # HTTP 422 with reason, no side effect
    "MALFORMED": "INVALID",  # HTTP 200 whose body breaks the declared shape, no side effect
    "RESET": "UNAVAILABLE",  # connection reset without a response, no side effect
}
FAULT_KEYS = {"adapter", "instance", "index", "ticks", "fault", "delay_ms", "retry_after", "reason"}

INSTANCE_RE = re.compile(r"[A-Za-z0-9._:-]{1,128}")


def canonical_json(value: Any) -> bytes:
    """Canonical encoding used for every response body and every digest."""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def strict_json(text: str) -> Any:
    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate key {key!r}")
            result[key] = value
        return result

    def invalid_constant(value):
        raise ValueError(f"non-JSON numeric constant {value}")

    return json.loads(text, object_pairs_hook=unique_pairs, parse_constant=invalid_constant)


class ScheduleError(ValueError):
    """A schedule file or schedule document that the stub refuses to load."""


def parse_kvx(text: str) -> dict:
    """Strict KVX: comments, [section] headers, key = <one JSON literal> lines."""
    sections: dict = {}
    current = None
    for number, original in enumerate(text.splitlines(), 1):
        line = original.strip()
        if not line or line.startswith("#"):
            continue
        section = re.fullmatch(r"\[([A-Za-z0-9_.-]+)\]", line)
        if section:
            name = section.group(1)
            if name in sections:
                raise ScheduleError(f"line {number}: duplicate section [{name}]")
            sections[name] = {}
            current = sections[name]
            continue
        assignment = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_-]*)\s*=\s*(.+)", line)
        if not assignment or current is None:
            raise ScheduleError(f"line {number}: unsupported or out-of-section syntax")
        key, value = assignment.groups()
        if key in current:
            raise ScheduleError(f"line {number}: duplicate key {key}")
        try:
            current[key] = strict_json(value)
        except ValueError as error:
            raise ScheduleError(
                f"line {number}: value must be a single JSON literal: {error}"
            ) from error
    return sections


def schedule_document_from_kvx(text: str) -> dict:
    sections = parse_kvx(text)
    if "schedule" not in sections:
        raise ScheduleError("KVX schedule needs a [schedule] section")
    document = dict(sections["schedule"])
    if "faults" in document:
        raise ScheduleError("KVX schedule declares faults as [fault.<name>] sections, not a key")
    faults = []
    for name, body in sections.items():
        if name == "schedule":
            continue
        if not name.startswith("fault.") or len(name) == len("fault."):
            raise ScheduleError(f"unknown section [{name}]")
        faults.append(dict(body, name=name[len("fault.") :]))
    document["faults"] = faults
    return document


def load_schedule_file(path: str | Path) -> dict:
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".kvx":
        return schedule_document_from_kvx(text)
    if path.suffix == ".json":
        try:
            return strict_json(text)
        except ValueError as error:
            raise ScheduleError(f"{path}: {error}") from error
    raise ScheduleError(f"{path}: schedule files must end in .json or .kvx")


@dataclass(frozen=True)
class Fault:
    name: str | None
    adapter: str  # qualified adapter name or "<server>.*"
    instance: str | None  # None matches every Instance
    index: frozenset | None
    ticks: tuple | None  # [lo, hi) in world_time ticks
    fault: str
    delay_ms: int | None
    retry_after: int | None
    reason: str | None

    def matches(self, adapter: str, instance: str, index: int, world_time: int) -> bool:
        if self.adapter.endswith(".*"):
            if not adapter.startswith(self.adapter[:-1]):
                return False
        elif self.adapter != adapter:
            return False
        if self.instance is not None and self.instance != instance:
            return False
        if self.index is not None:
            return index in self.index
        lo, hi = self.ticks
        return lo <= world_time < hi


@dataclass(frozen=True)
class Endpoint:
    adapter: str  # qualified adapter name, e.g. "publish.post"
    path: str  # HTTP path, e.g. "/v1/post"
    declared: frozenset  # declared failures from the adapter contract
    timeout_ms: int  # contract timeout_ms; default TIMEOUT delay is timeout_ms + 1000
    handler: Callable[[Any, dict, int], tuple[int, Any]]


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def compile_schedule(
    document: Any, server: str, endpoints: dict[str, Endpoint]
) -> tuple[list[Fault], str]:
    """Validate a schedule document for one server. Returns (faults, digest).

    Entries naming the other server's adapters are skipped, so one schedule may drive both stubs.
    A fault whose wire code is not declared by the targeted adapter is refused unless the
    schedule sets allow_undeclared = true (used only to test the gateway's undeclared mapping).
    """
    if not isinstance(document, dict):
        raise ScheduleError("schedule must be an object")
    allowed_top = {"format", "schema_version", "allow_undeclared", "faults", "note"}
    unknown = set(document) - allowed_top
    if unknown:
        raise ScheduleError(f"unknown schedule keys {sorted(unknown)}")
    if document.get("format") != SCHEDULE_FORMAT:
        raise ScheduleError(f"format must be {SCHEDULE_FORMAT!r}")
    if document.get("schema_version") != SCHEDULE_SCHEMA_VERSION:
        raise ScheduleError(f"schema_version must be {SCHEDULE_SCHEMA_VERSION}")
    allow_undeclared = document.get("allow_undeclared", False)
    if not isinstance(allow_undeclared, bool):
        raise ScheduleError("allow_undeclared must be a boolean")
    raw_faults = document.get("faults", [])
    if not isinstance(raw_faults, list):
        raise ScheduleError("faults must be a list")
    known_servers = {"search", "publish"}
    by_adapter = {endpoint.adapter: endpoint for endpoint in endpoints.values()}
    faults: list[Fault] = []
    for position, raw in enumerate(raw_faults):
        where = f"faults[{position}]"
        if not isinstance(raw, dict):
            raise ScheduleError(f"{where} must be an object")
        unknown = set(raw) - FAULT_KEYS - {"name"}
        if unknown:
            raise ScheduleError(f"{where}: unknown keys {sorted(unknown)}")
        adapter = raw.get("adapter")
        if not isinstance(adapter, str) or "." not in adapter:
            raise ScheduleError(
                f"{where}: adapter must be a qualified adapter name or '<server>.*'"
            )
        owner = adapter.split(".", 1)[0]
        if owner not in known_servers:
            raise ScheduleError(f"{where}: unknown adapter {adapter!r}")
        if owner != server:
            continue
        if adapter.endswith(".*"):
            targets = list(by_adapter.values())
        elif adapter in by_adapter:
            targets = [by_adapter[adapter]]
        else:
            raise ScheduleError(f"{where}: unknown adapter {adapter!r}")
        kind = raw.get("fault")
        if kind not in FAULT_WIRE_CODE:
            raise ScheduleError(f"{where}: fault must be one of {sorted(FAULT_WIRE_CODE)}")
        wire_code = FAULT_WIRE_CODE[kind]
        if not allow_undeclared:
            for target in targets:
                if wire_code not in target.declared:
                    raise ScheduleError(
                        f"{where}: fault {kind} yields {wire_code}, which {target.adapter} does not declare;"
                        " set allow_undeclared = true only to test the gateway's undeclared-status mapping"
                    )
        instance = raw.get("instance")
        if instance is not None and (
            not isinstance(instance, str) or not INSTANCE_RE.fullmatch(instance)
        ):
            raise ScheduleError(f"{where}: instance must be an Instance id string")
        has_index, has_ticks = "index" in raw, "ticks" in raw
        if has_index == has_ticks:
            raise ScheduleError(f"{where}: give exactly one of index or ticks")
        index = ticks = None
        if has_index:
            value = raw["index"]
            values = [value] if _is_int(value) else value
            if (
                not isinstance(values, list)
                or not values
                or not all(_is_int(v) and v >= 0 for v in values)
            ):
                raise ScheduleError(
                    f"{where}: index must be a non-negative integer or a nonempty list of them"
                )
            index = frozenset(values)
        else:
            value = raw["ticks"]
            if (
                not isinstance(value, list)
                or len(value) != 2
                or not all(_is_int(v) and v >= 0 for v in value)
                or value[0] >= value[1]
            ):
                raise ScheduleError(f"{where}: ticks must be [lo, hi] with 0 <= lo < hi")
            ticks = (value[0], value[1])
        delay_ms = raw.get("delay_ms")
        retry_after = raw.get("retry_after")
        reason = raw.get("reason")
        if delay_ms is not None and (kind != "TIMEOUT" or not _is_int(delay_ms) or delay_ms <= 0):
            raise ScheduleError(
                f"{where}: delay_ms is a positive integer and only valid for TIMEOUT"
            )
        if retry_after is not None and (
            kind != "RATE_LIMITED" or not _is_int(retry_after) or retry_after <= 0
        ):
            raise ScheduleError(
                f"{where}: retry_after is a positive integer and only valid for RATE_LIMITED"
            )
        if reason is not None and (kind != "REJECTED" or not isinstance(reason, str)):
            raise ScheduleError(f"{where}: reason is a string and only valid for REJECTED")
        if kind == "TIMEOUT" and delay_ms is None:
            delay_ms = max(target.timeout_ms for target in targets) + 1000
        if kind == "RATE_LIMITED" and retry_after is None:
            retry_after = 10
        if kind == "REJECTED" and reason is None:
            reason = "content refused by stub schedule"
        name = raw.get("name")
        if name is not None and not isinstance(name, str):
            raise ScheduleError(f"{where}: name must be a string")
        faults.append(
            Fault(name, adapter, instance, index, ticks, kind, delay_ms, retry_after, reason)
        )
    return faults, sha256_hex(canonical_json(document))


EMPTY_SCHEDULE = {
    "format": SCHEDULE_FORMAT,
    "schema_version": SCHEDULE_SCHEMA_VERSION,
    "faults": [],
}


class WindowLimiter:
    """Sliding window over world_time ticks: at most `limit` accepted events in (t - window, t]."""

    def __init__(self, limit: int, window: int):
        self.limit = limit
        self.window = window
        self.events: list[int] = []

    def retry_after(self, world_time: int) -> int | None:
        """None when an event at world_time is allowed, else ticks until the window reopens."""
        live = sorted(tick for tick in self.events if tick > world_time - self.window)
        if len(live) < self.limit:
            return None
        return max(1, live[-self.limit] + self.window - world_time)

    def accept(self, world_time: int) -> None:
        self.events.append(world_time)
        self.events = [tick for tick in self.events if tick > world_time - self.window]


def failure(code: str, detail: str, **extra: Any) -> tuple[int, dict]:
    assert code in STATUS_FOR_CODE, code
    body = {"code": code, "detail": detail}
    body.update(extra)
    return STATUS_FOR_CODE[code], body


def protocol_failure(endpoint: Endpoint, detail: str) -> tuple[int, dict]:
    """A request the stub cannot process at all (only a gateway defect sends one).

    Declared codes only (reference-world.md section 4): INVALID where the adapter declares it, else
    UNAVAILABLE, which every refworld adapter declares. Never a status the gateway would have to map
    through undeclared_status.
    """
    return failure("INVALID" if "INVALID" in endpoint.declared else "UNAVAILABLE", detail)


def success(result: Any) -> tuple[int, dict]:
    return 200, {"result": result}


@dataclass
class Reply:
    status: int | None
    body: bytes
    headers: dict = field(default_factory=dict)
    delay_ms: int = 0
    reset: bool = False


class StubCore:
    """Deterministic request processing, independent of HTTP plumbing."""

    server_name = "stub"

    def __init__(self, seed: int, schedule: dict | None = None):
        self.seed = seed
        self.lock = threading.Lock()
        self.endpoints: dict[str, Endpoint] = {e.path: e for e in self.build_endpoints()}
        self.set_schedule(schedule if schedule is not None else EMPTY_SCHEDULE)
        self.reset()

    # Subclasses provide these.
    def build_endpoints(self) -> list[Endpoint]:
        raise NotImplementedError

    def new_instance_state(self) -> Any:
        raise NotImplementedError

    def set_schedule(self, document: dict) -> str:
        faults, digest = compile_schedule(document, self.server_name, self.endpoints)
        self.faults = faults
        self.schedule_digest = digest
        return digest

    def reset(self) -> None:
        self.instances: dict[str, Any] = {}
        self.counters: dict[tuple[str, str], int] = {}
        self.log: list[dict] = []

    def state_for(self, instance: str) -> Any:
        if instance not in self.instances:
            self.instances[instance] = self.new_instance_state()
        return self.instances[instance]

    def describe(self) -> dict:
        return {
            "server": self.server_name,
            "seed": self.seed,
            "schedule_sha256": self.schedule_digest,
            "endpoints": [
                {
                    "adapter": e.adapter,
                    "path": e.path,
                    "declared": sorted(e.declared),
                    "timeout_ms": e.timeout_ms,
                }
                for e in sorted(self.endpoints.values(), key=lambda e: e.path)
            ],
        }

    def handle(self, path: str, headers: dict, raw_body: bytes) -> Reply:
        endpoint = self.endpoints.get(path)
        with self.lock:
            entry = {
                "n": len(self.log),
                "path": path,
                "adapter": endpoint.adapter if endpoint else None,
                "instance": None,
                "index": None,
                "world_time": None,
                "request_sha256": sha256_hex(raw_body),
                "status": None,
                "fault": None,
            }
            self.log.append(entry)
            if endpoint is None:
                status, body = failure("NOT_FOUND", f"no stub endpoint {path}")
                entry["status"] = status
                return Reply(status, canonical_json(body))
            instance = headers.get("x-cwr-instance", "")
            world_time_text = headers.get("x-cwr-world-time", "")
            if not INSTANCE_RE.fullmatch(instance) or not re.fullmatch(
                r"0|[1-9][0-9]{0,17}", world_time_text
            ):
                # Only a gateway defect reaches this branch; the kernel always sends both headers.
                status, body = protocol_failure(
                    endpoint, "missing or malformed X-CWR-Instance / X-CWR-World-Time header"
                )
                entry["status"] = status
                return Reply(status, canonical_json(body))
            world_time = int(world_time_text)
            key = (instance, endpoint.adapter)
            index = self.counters.get(key, 0)
            self.counters[key] = index + 1
            entry.update(instance=instance, index=index, world_time=world_time)
            fault = next(
                (
                    f
                    for f in self.faults
                    if f.matches(endpoint.adapter, instance, index, world_time)
                ),
                None,
            )
            entry["fault"] = fault.fault if fault else None
            reply = self._apply(endpoint, fault, instance, world_time, raw_body)
            entry["status"] = reply.status
            return reply

    def _apply(
        self,
        endpoint: Endpoint,
        fault: Fault | None,
        instance: str,
        world_time: int,
        raw_body: bytes,
    ) -> Reply:
        if fault is not None and fault.fault != "TIMEOUT":
            if fault.fault == "RESET":
                return Reply(None, b"", reset=True)
            if fault.fault == "MALFORMED":
                return Reply(
                    200, canonical_json({"malformed": True, "detail": "stub schedule MALFORMED"})
                )
            if fault.fault == "UNAVAILABLE":
                status, body = failure("UNAVAILABLE", "stub schedule UNAVAILABLE")
            elif fault.fault == "RATE_LIMITED":
                status, body = failure(
                    "RATE_LIMITED", "stub schedule RATE_LIMITED", retry_after=fault.retry_after
                )
            else:
                status, body = failure("REJECTED", fault.reason)
            return self._reply(status, body)
        try:
            params = strict_json(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            params = None
        if not isinstance(params, dict):
            status, body = protocol_failure(
                endpoint, "request body must be a JSON object of adapter parameters"
            )
        else:
            status, body = endpoint.handler(self.state_for(instance), params, world_time)
        reply = self._reply(status, body)
        if fault is not None:  # TIMEOUT: the side effect above has happened; the answer comes late.
            reply.delay_ms = fault.delay_ms
        return reply

    @staticmethod
    def _reply(status: int, body: dict) -> Reply:
        headers = {}
        if status == 429:
            headers["X-CWR-Retry-After-Ticks"] = str(body["retry_after"])
        return Reply(status, canonical_json(body), headers)


def make_handler(core: StubCore, verbose: bool):
    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = f"cwr-stub-{core.server_name}/1"
        sys_version = ""
        protocol_version = "HTTP/1.0"

        def log_message(self, format: str, *args: Any) -> None:
            if verbose:
                sys.stderr.write(f"[{core.server_name}] {format % args}\n")

        def _read_body(self) -> bytes:
            length = int(self.headers.get("Content-Length") or 0)
            return self.rfile.read(length) if length > 0 else b""

        def _send(self, status: int, body: bytes, headers: dict | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def _admin(self, method: str) -> bool:
            if not self.path.startswith("/_stub/"):
                return False
            if method == "GET" and self.path == "/_stub/health":
                self._send(200, canonical_json(dict(core.describe(), status="ok")))
            elif method == "GET" and self.path == "/_stub/log":
                with core.lock:
                    body = canonical_json({"requests": core.log})
                self._send(200, body)
            elif method == "POST" and self.path == "/_stub/reset":
                self._read_body()
                with core.lock:
                    core.reset()
                self._send(200, canonical_json({"reset": True}))
            elif method == "PUT" and self.path == "/_stub/schedule":
                raw = self._read_body()
                try:
                    document = strict_json(raw.decode("utf-8"))
                    with core.lock:
                        digest = core.set_schedule(document)
                except (UnicodeDecodeError, ValueError) as error:
                    self._send(
                        400, canonical_json({"error": "schedule refused", "detail": str(error)})
                    )
                else:
                    self._send(200, canonical_json({"schedule_sha256": digest}))
            else:
                self._send(404, canonical_json({"error": "unknown admin route"}))
            return True

        def do_GET(self) -> None:
            if not self._admin("GET"):
                self._send(405, canonical_json({"error": "adapter endpoints accept POST only"}))

        def do_PUT(self) -> None:
            if not self._admin("PUT"):
                self._send(405, canonical_json({"error": "adapter endpoints accept POST only"}))

        def do_POST(self) -> None:
            if self._admin("POST"):
                return
            raw = self._read_body()
            headers = {name.lower(): value for name, value in self.headers.items()}
            reply = core.handle(self.path, headers, raw)
            if reply.reset:
                with contextlib.suppress(OSError):
                    self.connection.setsockopt(
                        socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0)
                    )
                self.close_connection = True
                return
            if reply.delay_ms:
                time.sleep(
                    reply.delay_ms / 1000.0
                )  # host-time stall; the side effect already happened
            # The gateway may already have given up (TIMEOUT); nothing else to do then.
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                self._send(reply.status, reply.body, reply.headers)

    return Handler


class StubHTTPServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main(core_class: type[StubCore], argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=f"refworld {core_class.server_name} stub server")
    parser.add_argument("--host", default="127.0.0.1", help="bind address (default 127.0.0.1)")
    parser.add_argument(
        "--port", type=int, default=0, help="TCP port; 0 picks an ephemeral port (default)"
    )
    parser.add_argument("--seed", type=int, default=0, help="content seed (default 0)")
    parser.add_argument("--schedule", help="fault schedule file (.json or .kvx)")
    parser.add_argument("--port-file", help="write the bound port to this file once listening")
    parser.add_argument("--verbose", action="store_true", help="log every HTTP request to stderr")
    parser.add_argument(
        "--check-schedule",
        action="store_true",
        help="validate --schedule for this server, print its digest and exit",
    )
    args = parser.parse_args(argv)
    try:
        schedule = load_schedule_file(args.schedule) if args.schedule else EMPTY_SCHEDULE
        core = core_class(args.seed, schedule)
    except (OSError, ScheduleError) as error:
        print(
            json.dumps({"event": "error", "server": core_class.server_name, "detail": str(error)}),
            flush=True,
        )
        return 2
    if args.check_schedule:
        print(
            json.dumps(
                {
                    "event": "schedule_ok",
                    "server": core.server_name,
                    "schedule_sha256": core.schedule_digest,
                    "faults": len(core.faults),
                }
            ),
            flush=True,
        )
        return 0
    httpd = StubHTTPServer((args.host, args.port), make_handler(core, args.verbose))
    port = httpd.server_address[1]
    if args.port_file:
        Path(args.port_file).write_text(f"{port}\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "event": "ready",
                "server": core.server_name,
                "host": args.host,
                "port": port,
                "seed": core.seed,
                "schedule_sha256": core.schedule_digest,
            }
        ),
        flush=True,
    )
    try:
        httpd.serve_forever(poll_interval=0.1)
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0
