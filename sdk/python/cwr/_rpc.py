# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""CWR SDK RPC client: one primitive call is one request and one response over one unix socket.

Spec: spec/cwr/kernel.md sections 3 and 4 (the SDK's only side door is one unix socket; every public SDK
function performs exactly one RPC call). This file is shipped byte-for-byte inside every Build's
``sdk/cwr/`` by the compiler (task 1.3); it is part of the Build hash.

Framing (pinned here): every message is a frame of a 4-byte unsigned big-endian payload length followed by
that many bytes of UTF-8 canonical JSON (``cwr._codec.dumps``). Frames larger than ``MAX_FRAME`` are refused.

* Request: ``{"v": 1, "id": <n>, "primitive": "<module.name>", "params": {<name>: <codec value>}}`` where
  ``id`` counts from 1 within the process and parameter names are the contract's names (a Python keyword
  parameter such as ``from_`` travels as ``from``).
* Response: ``{"v": 1, "id": <n>, "result": <per-call result envelope>}`` (see ``cwr._codec``).

The socket is found, in order: a channel installed with ``install(sock)`` (the sandbox runner), an inherited
connected descriptor named by the environment variable ``CWR_RPC_FD``, or a socket path named by
``CWR_RPC_SOCKET``. There is no retry and no local refusal: a value the codec cannot represent still crosses
the boundary as ``{"$invalid": ...}`` and the kernel records the INVALID outcome.
"""

from __future__ import annotations

import json
import os
import socket
import struct

from . import _codec

__all__ = [
    "MAX_FRAME",
    "PROTOCOL",
    "RpcError",
    "call",
    "calls_made",
    "close",
    "install",
    "last_result",
]

PROTOCOL = 1
MAX_FRAME = 64 * 1024 * 1024
_HEADER = struct.Struct(">I")

_sock = None
_broken = None  # why the channel lost its framing; every later call refuses with this reason
_next_id = 1
_last = None
_count = 0


class RpcError(RuntimeError):
    """The RPC channel itself failed (no socket, closed connection, malformed frame). Not a world outcome."""


def install(sock) -> None:
    """Install a connected stream socket as the channel (used by the sandbox runner)."""
    global _sock, _broken
    _sock = sock
    _broken = None


def close() -> None:
    """Close the channel if one is open; the next call opens a new one from the environment."""
    global _sock, _broken
    if _sock is not None:
        try:
            _sock.close()
        finally:
            _sock = None
            _broken = None


def _channel():
    """The open channel, opening it from the environment on first use; raises RpcError if none."""
    global _sock
    if _broken is not None:
        raise RpcError(f"RPC channel unusable after an earlier defect: {_broken}")
    if _sock is not None:
        return _sock
    fd = os.environ.get("CWR_RPC_FD")
    if fd:
        try:
            _sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM, fileno=int(fd))
        except (ValueError, OSError) as e:
            raise RpcError(f"CWR_RPC_FD does not name a usable socket: {e}") from None
        return _sock
    path = os.environ.get("CWR_RPC_SOCKET")
    if path:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            s.connect(path)
        except OSError as e:
            s.close()
            raise RpcError(f"cannot connect to CWR_RPC_SOCKET: {e}") from None
        _sock = s
        return _sock
    raise RpcError(
        "no RPC channel: the SDK runs only inside a CWR turn (CWR_RPC_FD or CWR_RPC_SOCKET unset)"
    )


def _recv_exact(s, n: int) -> bytes:
    """Exactly ``n`` bytes from ``s``; raises RpcError if the peer closes first."""
    buf = bytearray()
    while len(buf) < n:
        chunk = s.recv(n - len(buf))
        if not chunk:
            raise RpcError("RPC channel closed by the kernel")
        buf.extend(chunk)
    return bytes(buf)


def send_frame(s, payload: bytes) -> None:
    """Send one frame (length prefix and payload); raises RpcError above ``MAX_FRAME``."""
    if len(payload) > MAX_FRAME:
        raise RpcError(f"frame of {len(payload)} bytes exceeds {MAX_FRAME}")
    s.sendall(_HEADER.pack(len(payload)) + payload)


def recv_frame(s) -> bytes:
    """Receive one frame's payload; raises RpcError above ``MAX_FRAME`` or on a closed channel."""
    (n,) = _HEADER.unpack(_recv_exact(s, _HEADER.size))
    if n > MAX_FRAME:
        raise RpcError(f"frame of {n} bytes exceeds {MAX_FRAME}")
    return _recv_exact(s, n)


def _raise(fail: dict):
    """Raise the Build's typed exception for a failure value."""
    from . import errors  # generated per Build from contracts.json

    raise errors.from_failure(fail)


def call(primitive: str, params: dict, content_guaranteed: bool = True):
    """Perform exactly one RPC for one primitive call; return the value or raise the typed exception.

    ``content_guaranteed`` is the Build's own label for the primitive (False for adapters); a response
    whose label differs is a channel defect, so the clause 2.5 label cannot be lost or forged in transit.
    """
    global _next_id, _last, _count, _broken
    s = _channel()
    rid = _next_id
    _next_id += 1
    request = {
        "v": PROTOCOL,
        "id": rid,
        "primitive": primitive,
        "params": _codec.encode(dict(params)),
    }
    text = json.dumps(request, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    payload = text.encode("utf-8")
    if len(payload) > MAX_FRAME:
        raise RpcError(f"frame of {len(payload)} bytes exceeds {MAX_FRAME}")
    # From the first byte sent until the whole response is read, a failure leaves the stream at an
    # unknown position: the channel is marked broken so that no later call reads a stale response.
    try:
        send_frame(s, payload)
        _count += 1
        raw = recv_frame(s)
    except RpcError as e:
        _broken = str(e)
        raise
    except OSError as e:
        _broken = f"RPC channel failed: {e}"
        raise RpcError(_broken) from None
    try:
        response = json.loads(raw.decode("utf-8"))
    except ValueError as e:
        raise RpcError(f"malformed RPC response: {e}") from None
    if not isinstance(response, dict) or response.get("id") != rid or response.get("v") != PROTOCOL:
        raise RpcError(f"RPC response does not answer request {rid}: {response!r}")
    result = response.get("result")
    try:
        _codec.check_call_result(result)
    except _codec.CodecError as e:
        raise RpcError(f"malformed call result: {e}") from None
    if result["content_guaranteed"] is not bool(content_guaranteed):
        raise RpcError(
            f"{primitive}: response labels content_guaranteed={result['content_guaranteed']}, "
            f"the Build says {bool(content_guaranteed)}"
        )
    _last = result
    if result["status"] == "ok":
        return _codec.decode(result["value"])
    _raise(result["error"])


def last_result():
    """The per-call result envelope of the most recent call, including its content_guaranteed label."""
    return _last


def calls_made() -> int:
    """Number of RPCs this process has sent."""
    return _count
