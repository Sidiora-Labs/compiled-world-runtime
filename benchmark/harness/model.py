# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Model gateway client for the CWR experiment harness (task 5.1 model adapter, requirement 12.3).

Speaks the Messages JSON format over HTTPS with the standard library only. The endpoint, the model
identity (identical for every arm of a campaign) and the gateway keys are read from the environment
at runtime. Keys rotate round-robin across requests and retries.

HTTP 429 and 5xx responses and
transport errors are retried with capped exponential backoff (deterministic, no jitter; a numeric
retry-after header raises the wait up to the cap), moving to the next key on every retry; 401 and 403
move to the next key and fail once every key has been refused; any other 4xx fails immediately.

Key values never leave this module: they are held in a masked wrapper, are not part of any repr,
log line, exception message or return value, and every server-supplied text placed in an exception
is redacted against all loaded keys. Callers learn only the key *slot* number used.

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
import hashlib
import importlib.util
import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Generator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
DEFAULT_ENV = REPO / ".env"
KEY_NAMES = tuple(f"CLUSTER_{n}_GATEWAY_API_KEY" for n in range(1, 6))
ANTHROPIC_VERSION = "2023-06-01"
RETRY_STATUSES = frozenset({429}) | frozenset(range(500, 600))
AUTH_STATUSES = frozenset({401, 403})


class ModelError(RuntimeError):
    """A gateway request failed. The message never contains a key value."""

    def __init__(self, message: str, status: int | None = None, attempts: int = 0):
        super().__init__(message)
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


def read_env(path: Path | str | None = None) -> dict[str, str]:
    """Parse KEY=VALUE lines of a dotenv file. Values are returned, never printed."""
    path = Path(path or os.environ.get("CWR_ENV_FILE") or DEFAULT_ENV)
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
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


class GatewayClient:
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
        opener: Callable[..., Any] = urllib.request.urlopen,
    ):
        keys = [k for k in keys if k]
        if not messages_url or not model or not keys:
            raise ModelError(
                "gateway configuration incomplete: need MESSAGES_URL, MODEL and at least one key"
            )
        self._url = messages_url
        self._keys = [_Secret(k) for k in keys]
        self.model = model
        self.timeout_s = timeout_s
        self.max_attempts = max_attempts
        self.backoff_base_s = backoff_base_s
        self.backoff_cap_s = backoff_cap_s
        self._sleep = sleep
        self._open = opener
        self._next = 0

    @classmethod
    def from_env(cls, env_path: Path | str | None = None, **kwargs: Any) -> GatewayClient:
        env = read_env(env_path)
        keys = [env.get(name, "") for name in KEY_NAMES]
        return cls(env.get("MESSAGES_URL", ""), env.get("MODEL", ""), keys, **kwargs)

    def __repr__(self) -> str:
        return f"GatewayClient(model={self.model!r}, keys={len(self._keys)})"

    @property
    def key_count(self) -> int:
        return len(self._keys)

    def _redact(self, text: str) -> str:
        for secret in self._keys:
            value = secret.reveal()
            if value:
                text = text.replace(value, "[redacted]")
        return text

    def _take_slot(self) -> int:
        slot = self._next % len(self._keys)
        self._next = (slot + 1) % len(self._keys)
        return slot

    def _backoff(self, attempt: int, retry_after: str | None) -> float:
        delay = min(self.backoff_cap_s, self.backoff_base_s * (2**attempt))
        if retry_after:
            with contextlib.suppress(ValueError):
                delay = max(delay, min(float(retry_after), self.backoff_cap_s))
        return delay

    def _request(self, slot: int, body: bytes) -> urllib.request.Request:
        key = self._keys[slot].reveal()
        return urllib.request.Request(
            self._url,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {key}",
                "x-api-key": key,
                "anthropic-version": ANTHROPIC_VERSION,
                "content-type": "application/json",
            },
        )

    def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        max_tokens: int,
        system: str | None = None,
        params: dict[str, Any] | None = None,
    ) -> ModelReply:
        """POST one Messages request and return the concatenated text blocks with usage."""
        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": messages,
        }
        if system is not None:
            payload["system"] = system
        for key, value in (params or {}).items():
            if key in payload:
                raise ModelError(f"params may not override {key}")
            payload[key] = value
        body = json.dumps(payload).encode("utf-8")
        refused: set[int] = set()
        last_error = "no attempt made"
        last_status: int | None = None
        for attempt in range(self.max_attempts):
            slot = self._take_slot()
            try:
                with self._open(self._request(slot, body), timeout=self.timeout_s) as response:
                    raw = response.read()
                return self._parse(raw, slot, attempt + 1, params or {})
            except urllib.error.HTTPError as error:
                status = error.code
                try:
                    detail = error.read().decode("utf-8", "replace")[:500]
                except Exception:
                    detail = ""
                retry_after = error.headers.get("retry-after") if error.headers else None
                last_status = status
                last_error = f"HTTP {status}: {self._redact(detail)}"
                if status in AUTH_STATUSES:
                    refused.add(slot)
                    if len(refused) >= len(self._keys):
                        raise ModelError(
                            f"every gateway key was refused; last {last_error}", status, attempt + 1
                        ) from None
                    continue
                if status not in RETRY_STATUSES:
                    raise ModelError(
                        f"gateway request failed with {last_error}", status, attempt + 1
                    ) from None
                if attempt + 1 < self.max_attempts:
                    self._sleep(self._backoff(attempt, retry_after))
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as error:
                last_status = None
                last_error = self._redact(
                    f"transport error: {type(error).__name__}: {getattr(error, 'reason', error)}"
                )
                if attempt + 1 < self.max_attempts:
                    self._sleep(self._backoff(attempt, None))
        raise ModelError(
            f"gateway request failed after {self.max_attempts} attempts; last {last_error}",
            last_status,
            self.max_attempts,
        )

    def _parse(self, raw: bytes, slot: int, attempts: int, params: dict[str, Any]) -> ModelReply:
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ModelError(f"gateway returned a non-JSON body: {error}", 200, attempts) from None
        if not isinstance(data, dict) or not isinstance(data.get("content"), list):
            raise ModelError(
                "gateway response is not a Messages object with a content list", 200, attempts
            )
        text = "".join(
            block.get("text", "")
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

    Tries single lines first (the canonical one-line rendering), then fenced json blocks.
    """
    candidates = list(reversed(context.splitlines()))
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
        "--env",
        default=None,
        help="credentials file (default: $CWR_ENV_FILE, else the repository default)",
    )
    args = parser.parse_args(argv)
    if not args.smoke:
        parser.print_help()
        return 2
    client = GatewayClient.from_env(args.env, max_attempts=4)
    try:
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
