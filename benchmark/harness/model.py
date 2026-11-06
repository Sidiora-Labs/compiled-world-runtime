# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Model gateway client for the CWR experiment harness (task 5.1 model adapter, requirement 12.3).

Speaks the Messages JSON format over HTTPS with the standard library only. The endpoint
(MESSAGES_URL), the model identity (MODEL, identical for every arm of a campaign) and the gateway
keys (CLUSTER_1_GATEWAY_API_KEY to CLUSTER_5_GATEWAY_API_KEY) are read from the process environment
at runtime. A credentials file is read only when an operator passes its path explicitly and the file
lies outside the repository; nothing in the tree is ever read for credentials.

Transport guarantees:

  - The endpoint must be https. Plain http is accepted only for a loopback test server and only
    when the caller sets allow_insecure_loopback.
  - Redirects are never followed: any 3xx response fails the request, so a key is never sent to a
    second location.
  - Each request carries the key once, in the x-api-key header.

Retries and rate limiting: HTTP 429, 5xx and transport errors are retried with capped exponential
backoff and full jitter (the wait is drawn uniformly from [0, min(cap, base * 2^attempt)]; a numeric
retry-after raises it up to the cap), moving to the next key on every retry. A key refused with 401
or 403 is quarantined for the client's lifetime; the request fails once every key is quarantined. Any
other status fails immediately. Each client starts its round-robin at a different slot (a
process-wide counter), slot selection is thread-safe, and `inflight_slot` bounds concurrent
requests across every process on the host. Timing never enters a ledger, so jitter costs no
determinism.

Secrecy: key values never leave this module. They are held in a masked wrapper and are not part of
any repr, log line, exception message or return value. Every failure surfaces as a ModelError with a
fixed category (config, http_status, auth, redirect, transport, parse) and a short reason built
from the status code, an exception class name or a server error type identifier; server bodies,
exception messages, the endpoint URL and its host are never included, and the reason is still
redacted against every key, the URL and the host. Callers learn only the key *slot* number used.

No sampling parameter is sent unless the caller passes one in `params`; whatever is sent is returned in
ModelReply.request_params so the runner can record the sampling settings in the attempt record.

ReferenceStubModel is the deterministic offline model for harness smoke runs: it opens no socket and
replays a task's reference solution from benchmark/solutions, so a run exercises the real kernel without
a model. Its identity is distinct from any registered model, so its runs are never mistaken for a
campaign.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import importlib.util
import ipaddress
import itertools
import json
import os
import random
import re
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Generator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
URL_NAME = "MESSAGES_URL"
MODEL_NAME = "MODEL"
KEY_NAMES = tuple(f"CLUSTER_{n}_GATEWAY_API_KEY" for n in range(1, 6))
ANTHROPIC_VERSION = "2023-06-01"
RETRY_STATUSES = frozenset({429}) | frozenset(range(500, 600))
AUTH_STATUSES = frozenset({401, 403})
INFLIGHT_ENV = "CWR_GATEWAY_MAX_INFLIGHT"
INFLIGHT_DEFAULT = 48
CATEGORIES = ("config", "http_status", "auth", "redirect", "transport", "parse")
_ERROR_TYPE = re.compile(r"[a-z][a-z0-9_]{0,47}")
# Start slots of successive clients in this process, so concurrent workers spread over the keys.
_CLIENT_STARTS = itertools.count()
_CLIENT_STARTS_LOCK = threading.Lock()


@contextlib.contextmanager
def inflight_slot() -> Generator[None]:
    """Hold one of N machine-wide gateway request slots while one HTTP request is in flight.

    N is $CWR_GATEWAY_MAX_INFLIGHT (default 48; 0 disables the bound). Slots are flock-ed files under
    the system temp directory, so every campaign process on the host (tasks 5.2 and 5.3 run
    concurrently) shares one bound on concurrent gateway requests. Backoff sleeps hold no slot.
    """
    try:
        limit = int(os.environ.get(INFLIGHT_ENV, INFLIGHT_DEFAULT))
    except ValueError:
        raise ModelError("config", f"{INFLIGHT_ENV} is not an integer") from None
    if limit <= 0:
        yield
        return
    root = Path(tempfile.gettempdir()) / "cwr-gateway-slots"
    root.mkdir(exist_ok=True)
    start = (os.getpid() * 31 + threading.get_ident()) % limit
    while True:
        for offset in range(limit):
            fd = os.open(root / f"slot-{(start + offset) % limit}", os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(fd)
                continue
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)
            return
        time.sleep(0.2)


class ModelError(RuntimeError):
    """A gateway request failed.

    `category` is one of CATEGORIES and `reason` a short text built only from status codes,
    exception class names and server error type identifiers. Neither ever holds a key value, the
    endpoint URL or its host, a server body or an exception message.
    """

    def __init__(self, category: str, reason: str, status: int | None = None, attempts: int = 0):
        if category not in CATEGORIES:
            category = "transport"
        super().__init__(f"{category}: {reason}")
        self.category = category
        self.reason = reason
        self.status = status
        self.attempts = attempts


class _Secret:
    __slots__ = ("_value",)

    def __init__(self, value: str):
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "<secret>"

    __str__ = __repr__


@dataclass(frozen=True)
class ModelReply:
    """One completed model turn: concatenated text blocks, reported identity and usage."""

    text: str
    model: str
    stop_reason: str | None
    input_tokens: int | None
    output_tokens: int | None
    usage: dict[str, Any]
    key_slot: int
    attempts: int
    request_params: dict[str, Any] = field(default_factory=dict)
    message_id: str | None = None


def read_env_file(path: Path | str) -> dict[str, str]:
    """Parse KEY=VALUE lines of an explicitly named credentials file outside the repository.

    Refuses (ModelError, category config) a file inside the repository or one that cannot be read.
    Values are returned, never printed.
    """
    resolved = Path(path).expanduser().resolve()
    if resolved == REPO or REPO in resolved.parents:
        raise ModelError(
            "config", "a credentials file inside the repository is refused; export the variables"
        )
    try:
        text = resolved.read_text(encoding="utf-8")
    except OSError as error:
        raise ModelError(
            "config", f"credentials file unreadable ({type(error).__name__})"
        ) from None
    values: dict[str, str] = {}
    for raw in text.split("\n"):
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def _loopback(host: str | None) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host or "").is_loopback
    except ValueError:
        return False


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: the 3xx response surfaces as an HTTPError and fails the request."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        del req, fp, code, msg, headers, newurl


def _default_opener() -> Callable[..., Any]:
    """urlopen of a private opener without the redirect handler's following behaviour."""
    return urllib.request.build_opener(_NoRedirect()).open


class GatewayClient:
    """Messages client with key rotation, quarantine of refused keys, jittered retries and redaction.

    Safe to share between threads: slot selection and quarantine are guarded by a lock.
    """

    def __init__(
        self,
        messages_url: str,
        model: str,
        keys: Sequence[str],
        *,
        timeout_s: float = 300.0,
        max_attempts: int = 8,
        backoff_base_s: float = 1.0,
        backoff_cap_s: float = 60.0,
        sleep: Callable[[float], None] = time.sleep,
        opener: Callable[..., Any] | None = None,
        jitter: Callable[[float], float] | None = None,
        start_slot: int | None = None,
        allow_insecure_loopback: bool = False,
    ):
        keys = [k for k in keys if k]
        if not messages_url or not model or not keys:
            raise ModelError(
                "config",
                f"gateway configuration incomplete: need {URL_NAME}, {MODEL_NAME} and at least one "
                "of " + ", ".join(KEY_NAMES),
            )
        parts = urllib.parse.urlsplit(messages_url)
        self._url = messages_url
        self._host = parts.hostname or ""
        self._keys = [_Secret(k) for k in keys]
        if parts.scheme != "https" and not (
            parts.scheme == "http" and allow_insecure_loopback and _loopback(parts.hostname)
        ):
            raise ModelError(
                "config",
                f"{URL_NAME} must use https (plain http only for a loopback test server)",
            )
        self.model = model
        self.timeout_s = timeout_s
        self.max_attempts = max_attempts
        self.backoff_base_s = backoff_base_s
        self.backoff_cap_s = backoff_cap_s
        self._sleep = sleep
        self._open = opener or _default_opener()
        rng = random.Random()
        self._jitter = jitter or (lambda delay: rng.uniform(0.0, delay))
        self._lock = threading.Lock()
        self._quarantined: set[int] = set()
        if start_slot is None:
            with _CLIENT_STARTS_LOCK:
                start_slot = next(_CLIENT_STARTS)
        self._next = start_slot % len(self._keys)

    @classmethod
    def from_env(
        cls,
        env_file: Path | str | None = None,
        *,
        environ: Mapping[str, str] | None = None,
        **kwargs: Any,
    ) -> GatewayClient:
        """A client configured from the process environment.

        `env_file`, when an operator passes one explicitly, must lie outside the repository; its
        values fill variables the environment does not set. Raises ModelError (config) naming the
        missing variables, never their values.
        """
        values = dict(os.environ if environ is None else environ)
        if env_file is not None:
            for key, value in read_env_file(env_file).items():
                values.setdefault(key, value)
        keys = [values.get(name, "") for name in KEY_NAMES]
        missing = [name for name in (URL_NAME, MODEL_NAME) if not values.get(name)]
        if not any(keys):
            missing.append("CLUSTER_<n>_GATEWAY_API_KEY")
        if missing:
            raise ModelError(
                "config", "gateway variables missing from the environment: " + ", ".join(missing)
            )
        return cls(values[URL_NAME], values[MODEL_NAME], keys, **kwargs)

    def __repr__(self) -> str:
        return f"GatewayClient(model={self.model!r}, keys={len(self._keys)})"

    @property
    def key_count(self) -> int:
        """Number of configured keys (quarantined ones included)."""
        return len(self._keys)

    @property
    def quarantined_slots(self) -> list[int]:
        """One-based slot numbers of keys refused with 401 or 403 during this client's lifetime."""
        with self._lock:
            return sorted(slot + 1 for slot in self._quarantined)

    def _redact(self, text: str) -> str:
        values = [secret.reveal() for secret in self._keys] + [self._url, self._host]
        for value in sorted((v for v in values if v), key=len, reverse=True):
            text = text.replace(value, "[redacted]")
        return text

    def _fail(self, category: str, reason: str, status: int | None, attempts: int) -> ModelError:
        return ModelError(category, self._redact(reason), status, attempts)

    def _take_slot(self) -> int | None:
        """The next key slot that is not quarantined, or None when every key is."""
        with self._lock:
            for _ in range(len(self._keys)):
                slot = self._next
                self._next = (slot + 1) % len(self._keys)
                if slot not in self._quarantined:
                    return slot
            return None

    def _quarantine(self, slot: int) -> None:
        with self._lock:
            self._quarantined.add(slot)

    def _backoff(self, attempt: int, retry_after: str | None) -> float:
        delay = self._jitter(min(self.backoff_cap_s, self.backoff_base_s * (2**attempt)))
        if retry_after:
            with contextlib.suppress(ValueError):
                delay = max(delay, min(float(retry_after), self.backoff_cap_s))
        return delay

    def _request(self, slot: int, body: bytes) -> urllib.request.Request:
        return urllib.request.Request(
            self._url,
            data=body,
            method="POST",
            headers={
                "x-api-key": self._keys[slot].reveal(),
                "anthropic-version": ANTHROPIC_VERSION,
                "content-type": "application/json",
            },
        )

    @staticmethod
    def _error_type(error: urllib.error.HTTPError) -> str | None:
        """The server's error type identifier (e.g. rate_limit_error), never its message."""
        try:
            data = json.loads(error.read(4096).decode("utf-8", "replace"))
        except Exception:
            return None
        node = data.get("error") if isinstance(data, dict) else None
        kind = node.get("type") if isinstance(node, dict) else None
        return kind if isinstance(kind, str) and _ERROR_TYPE.fullmatch(kind) else None

    def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        max_tokens: int,
        system: str | None = None,
        params: dict[str, Any] | None = None,
    ) -> ModelReply:
        """POST one Messages request and return the concatenated text blocks with usage.

        Raises ModelError only; no other exception escapes.
        """
        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": messages,
        }
        if system is not None:
            payload["system"] = system
        for key, value in (params or {}).items():
            if key in payload:
                raise ModelError("config", f"params may not override {key}")
            payload[key] = value
        try:
            body = json.dumps(payload).encode("utf-8")
        except (TypeError, ValueError) as error:
            raise ModelError("config", f"request is not JSON ({type(error).__name__})") from None
        last = ("transport", "no attempt made", None)
        for attempt in range(self.max_attempts):
            retry_after: str | None = None
            slot = self._take_slot()
            if slot is None:
                raise self._fail("auth", "every gateway key was refused", last[2], attempt)
            try:
                with (
                    inflight_slot(),
                    self._open(self._request(slot, body), timeout=self.timeout_s) as response,
                ):
                    raw = response.read()
                return self._parse(raw, slot, attempt + 1, params or {})
            except ModelError:
                raise
            except urllib.error.HTTPError as error:
                status = error.code
                kind = self._error_type(error)
                retry_after = error.headers.get("retry-after") if error.headers else None
                reason = f"HTTP {status}" + (f" {kind}" if kind else "")
                if 300 <= status < 400:
                    raise self._fail(
                        "redirect", f"{reason}: redirects are never followed", status, attempt + 1
                    ) from None
                if status in AUTH_STATUSES:
                    self._quarantine(slot)
                    last = ("auth", f"{reason}; key slot {slot + 1} quarantined", status)
                    continue
                if status not in RETRY_STATUSES:
                    raise self._fail("http_status", reason, status, attempt + 1) from None
                last = ("http_status", reason, status)
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as error:
                cause = getattr(error, "reason", None)
                name = type(cause).__name__ if isinstance(cause, BaseException) else ""
                last = ("transport", type(error).__name__ + (f" ({name})" if name else ""), None)
            except Exception as error:
                last = ("transport", f"unexpected {type(error).__name__}", None)
            if attempt + 1 < self.max_attempts:
                self._sleep(self._backoff(attempt, retry_after))
        category, reason, status = last
        raise self._fail(
            category,
            f"failed after {self.max_attempts} attempts; last {reason}",
            status,
            self.max_attempts,
        )

    def _parse(self, raw: bytes, slot: int, attempts: int, params: dict[str, Any]) -> ModelReply:
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ModelError(
                "parse", f"gateway returned a non-JSON body ({type(error).__name__})", 200, attempts
            ) from None
        if not isinstance(data, dict) or not isinstance(data.get("content"), list):
            raise ModelError(
                "parse",
                "gateway response is not a Messages object with a content list",
                200,
                attempts,
            )
        text = "".join(
            str(block.get("text", ""))
            for block in data["content"]
            if isinstance(block, dict) and block.get("type") == "text"
        )
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        return ModelReply(
            text=text,
            model=str(data.get("model", self.model)),
            stop_reason=data.get("stop_reason"),
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
            usage=usage,
            key_slot=slot + 1,
            attempts=attempts,
            request_params=dict(params),
            message_id=data.get("id"),
        )

    def complete_context(
        self, context: str, *, max_tokens: int, params: dict[str, Any] | None = None
    ) -> ModelReply:
        """One CWR turn: the composed context (model-surface.md section 1) as the single user message.

        There is no system prompt other than the manual, which is already the first component of the
        context, so no `system` field is sent.
        """
        return self.complete(
            [{"role": "user", "content": context}], max_tokens=max_tokens, params=params
        )


STUB_MODEL = "cwr-stub/reference-player-1"
SOLUTIONS = REPO / "benchmark" / "solutions"
_ENVELOPE_KEYS = ("status", "world", "trace")


def envelope_from_context(context: str) -> dict[str, Any] | None:
    """The last result envelope rendered in a context: the last JSON object with status, world and trace.

    Tries single lines first (the canonical one-line rendering), then fenced json blocks. Lines end
    at LF only, so an envelope string holding U+0085, U+2028 or U+2029 stays on its line.
    """
    candidates = list(reversed(context.split("\n")))
    blocks = context.split("```json")
    candidates += [b.split("```", 1)[0] for b in reversed(blocks[1:])]
    for text in candidates:
        text = text.strip()
        if not text.startswith("{"):
            continue
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and all(k in value for k in _ENVELOPE_KEYS):
            return value
    return None


class ReferenceStubModel:
    """Deterministic offline model: plays a task's reference solution one turn per request.

    The solution module's `turns()` generator yields one script body per turn and receives the previous
    result envelope (benchmark/solutions protocol). Each body is returned inside one python fence. The
    envelope sent back is the one rendered in the context (`envelope_from_context`), so the stub sees
    only what a model sees. On an even seed the first reply is a probe that reads a path absent from
    every task, which fails with the declared NOT_FOUND; seeds therefore differ in ledgers and metrics.
    Once the solution is exhausted every further reply is an empty turn (`pass`). No socket is opened;
    token counts are None because nothing crosses a model boundary.
    """

    def __init__(
        self,
        turns: Callable[[], Generator[str, Any, Any]],
        *,
        seed: int,
        model: str = STUB_MODEL,
    ):
        self.model = model
        self.seed = seed
        self._factory = turns
        self._generator: Generator[str, Any, Any] | None = None
        self._probe_pending = seed % 2 == 0
        self._done = False
        self.requests = 0

    @classmethod
    def for_task(
        cls, task: str, *, seed: int, solutions: Path | str = SOLUTIONS
    ) -> ReferenceStubModel:
        path = Path(solutions) / f"{task}.py"
        name = "cwr_solution_" + hashlib.sha256(str(path).encode()).hexdigest()[:16]
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            raise ModelError(f"no reference solution for task {task} at {path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        if not callable(getattr(module, "turns", None)):
            raise ModelError(f"{path} defines no turns() generator")
        return cls(module.turns, seed=seed)

    def _next_body(self, envelope: dict[str, Any] | None) -> str:
        if self._probe_pending:
            self._probe_pending = False
            return f'import fs\nfs.read(path="/.cwr-stub-probe/seed-{self.seed}")\n'
        if self._done:
            return "pass\n"
        try:
            if self._generator is None:
                self._generator = self._factory()
                return next(self._generator)
            fallback = {"status": None, "world": {"time": 0, "turn": 0}, "trace": []}
            return self._generator.send(envelope or fallback)
        except StopIteration:
            self._done = True
            return "pass\n"

    def complete_context(
        self, context: str, *, max_tokens: int, params: dict[str, Any] | None = None
    ) -> ModelReply:
        """One deterministic reply: exactly one fenced python block holding the next script."""
        del max_tokens
        self.requests += 1
        body = self._next_body(envelope_from_context(context))
        return ModelReply(
            text=f"```python\n{body.rstrip(chr(10))}\n```\n",
            model=self.model,
            stop_reason="end_turn",
            input_tokens=None,
            output_tokens=None,
            usage={},
            key_slot=0,
            attempts=1,
            request_params=dict(params or {}),
            message_id=f"stub-{self.seed}-{self.requests}",
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="CWR gateway client smoke check (prints no secrets)."
    )
    parser.add_argument(
        "--smoke", action="store_true", help="send one short request with max_tokens 20"
    )
    parser.add_argument(
        "--env-file",
        default=None,
        help="credentials file outside the repository; fills variables the environment lacks",
    )
    args = parser.parse_args(argv)
    if not args.smoke:
        parser.print_help()
        return 2
    try:
        client = GatewayClient.from_env(args.env_file, max_attempts=4)
        reply = client.complete(
            [{"role": "user", "content": "Reply with the single word: ok"}], max_tokens=20
        )
    except ModelError as error:
        print(f"smoke FAILED: {error}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "ok": True,
                "model": reply.model,
                "stop_reason": reply.stop_reason,
                "input_tokens": reply.input_tokens,
                "output_tokens": reply.output_tokens,
                "key_slot": reply.key_slot,
                "attempts": reply.attempts,
                "text": reply.text,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
