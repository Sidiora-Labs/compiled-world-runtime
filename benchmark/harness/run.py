# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""CWR experiment runner: one Instance per (hypothesis, arm, task, seed) (task 5.1 do_1).

The runner drives turns through two narrow interfaces and never touches an Instance any other way:

  KernelAdapter  creates the Instance of the arm's Build, returns the composed context (manual, notice,
                 ledger tail K, last envelope, in that order: model-surface.md section 1), submits the
                 model's message as one turn, upgrades for H4, and exports the ledger and state.
  ModelAdapter   one request per turn with the composed context; returns text and token counts.

CwrCliKernel binds the KernelAdapter to the real `cwr` CLI (kernel.md section 2): one Instance store
per run (`--home`), Builds resolved by hash from the operator's build roots, the adapter gateway in
replay mode against one cassette per (task, seed), or in live mode for the labelled robustness run.
The harness never invokes the compiler: a human operator compiles every Build with `cwr build`
(experimental ones with their single flag) and passes it in by hash; the runner refuses a Build whose
manifest flag is not exactly the arm's flag. Unit tests use an in-memory kernel.

Per Instance the runner writes, under benchmark/results (benchmark/results/live for live mode):

  h<N>/<arm>/<task>/<seed>/ledger.jsonl       exported ledger, byte-for-byte what the kernel wrote
  h<N>/<arm>/<task>/<seed>/final_state.kvx    exported final state (the predicate state view)
  h<N>/<arm>/<task>/<seed>/states/turn_NNNN.kvx   state after each turn (for turns to success)
  h<N>/<arm>/<task>/<seed>/attempt.kvx        attempt record (experiments.md section 6) with the
                                              harness's own ledger-only metrics in [metrics]

Stop rule (registered as convention C7 in metrics.py): the run stops after the first turn whose success
predicate holds over the exported state and ledger, at the turn limit, when the harness token budget is
exhausted, on a model error, or when the ledger records INVARIANT_VIOLATION (a Build defect).
The harness has no write path into build/ or xbuild/ and never invokes the compiler.
"""

from __future__ import annotations

import abc
import argparse
import base64
import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    __package__ = "benchmark.harness"

from . import metrics
from .arms import Arm, load_arms, resolve_build
from .ledgerio import (
    REPO,
    load_predicate,
    parse_kvx,
    read_kvx,
    read_ledger,
    state_view,
    write_kvx,
    write_ledger,
)
from .prereg import DigestMismatch, check_digests, load_prereg, missing_fields

RESULTS = REPO / "benchmark" / "results"
EXIT_STATUSES = (
    "success",
    "turn_limit",
    "budget",
    "model_error",
    "invariant_violation",
    "kernel_error",
)
GATEWAY_MODES = ("replay", "live")

# The task instruction is the one harness-authored text a model sees. The kernel composes the four
# context components (model-surface.md section 1); the runner appends task.md verbatim under this fixed
# heading, byte-identical for every arm, seed and turn (reference-world.md section 5: "the instruction
# given to the model, verbatim, identical across arms"). It is the harness's only prompt template.
TASK_TEMPLATE = "\n\n# Task\n\n{instruction}"
# Separators of the harness-side context assembly used only while the CLI has no `context` command.
FALLBACK_TAIL_HEADING = "\n\n# Ledger tail\n\n"
FALLBACK_ENVELOPE_HEADING = "\n\n# Last result envelope\n\n"
STATE_VIEW_FORMAT = "cwr.state-view"


# ---------------------------------------------------------------------------------------------
# Interfaces


class KernelAdapter(abc.ABC):
    """Everything the runner may ask of the kernel. Implementations must not expose kernel internals."""

    gateway_mode: str = "replay"

    @abc.abstractmethod
    def create_instance(
        self,
        *,
        build: str,
        seed: int,
        tail: int | str,
        arm: str,
        model: str,
        allow_experimental: bool,
        initial_state: Path | None,
    ) -> str: ...

    @abc.abstractmethod
    def context(self, instance: str) -> str: ...

    @abc.abstractmethod
    def turn(self, instance: str, message: str) -> dict[str, Any]:
        """Submit the model's whole message; the kernel extracts the fenced block. Returns the envelope."""

    @abc.abstractmethod
    def upgrade(self, instance: str, build: str, *, allow_experimental: bool) -> None: ...

    @abc.abstractmethod
    def ledger(self, instance: str) -> list[dict[str, Any]]: ...

    @abc.abstractmethod
    def state(self, instance: str) -> dict[str, dict[str, Any]]: ...

    @abc.abstractmethod
    def destroy(self, instance: str) -> None: ...

    def revisions(self) -> dict[str, Any]:
        return {"compiler": None, "kernel": None, "sdk": None}

    def bind_cassette(self, instance: str, cassette: Path | None) -> None:
        """Bind the replay cassette of this Instance; kernels without a gateway ignore it."""
        del instance, cassette

    def export_ledger(self, instance: str, path: Path) -> None:
        """Write the Instance ledger to path; the CLI binding copies the kernel's bytes unchanged."""
        write_ledger(path, self.ledger(instance))

    def surface(self) -> dict[str, Any]:
        """How the context was composed and the turn protocol applied; recorded in the attempt."""
        return {"context": "kernel", "protocol": "kernel", "harness_protocol_refusals": 0}


class ModelAdapter(Protocol):
    model: str

    def complete_context(
        self, context: str, *, max_tokens: int, params: dict[str, Any] | None = None
    ) -> Any: ...


def cwr_binary() -> Path:
    """The cwr CLI: $CWR_BIN, else <CARGO_TARGET_DIR or workspace target>/release|debug/cwr."""
    if os.environ.get("CWR_BIN"):
        return Path(os.environ["CWR_BIN"])
    target = Path(os.environ.get("CARGO_TARGET_DIR") or REPO / "target")
    for profile in ("release", "debug"):
        candidate = target / profile / "cwr"
        if candidate.exists():
            return candidate
    return target / "debug" / "cwr"


class KernelCommandError(RuntimeError):
    """A `cwr` command failed; carries its exit code and the tail of its stderr."""

    def __init__(self, command: str, returncode: int, stderr: str):
        tail = stderr.strip().splitlines()[-3:]
        super().__init__(f"`cwr {command}` exited {returncode}: {' | '.join(tail)}")
        self.returncode = returncode


def extract_blocks(message: str) -> list[tuple[str, str | None]]:
    """Fenced blocks of a model message as (language, body); body None when the fence never closes.

    A fence is a line of three or more backticks, optionally followed by a language word; it closes at
    the next line consisting of the same backtick run. Text outside fences is ignored (model-surface.md
    section 3).
    """
    blocks: list[tuple[str, str | None]] = []
    lines = message.split("\n")
    index = 0
    opening = re.compile(r"(`{3,})[ \t]*([^`\s]*)[ \t]*")
    while index < len(lines):
        match = opening.fullmatch(lines[index].strip())
        if not match:
            index += 1
            continue
        fence, language = match.group(1), match.group(2)
        end = index + 1
        while end < len(lines) and lines[end].strip() != fence:
            end += 1
        if end >= len(lines):
            blocks.append((language, None))
            break
        blocks.append((language, "\n".join(lines[index + 1 : end]) + "\n"))
        index = end + 1
    return blocks


def protocol_refusal(reason: str, world: Mapping[str, Any] | None) -> dict[str, Any]:
    """The INVALID envelope of model-surface.md section 3 for a message without exactly one python block."""
    return {
        "status": "error",
        "returned": None,
        "error": {
            "code": "INVALID",
            "primitive": "turn.protocol",
            "precondition_index": None,
            "retry_after": None,
            "limit": None,
            "detail": reason,
        },
        "trace": [],
        "world": dict(world or {}),
        "resources": {"primitive_calls": 0, "adapter_calls": 0},
    }


def _kernel_bytes(content: Any) -> dict[str, str]:
    """A view file content (UTF-8 text or {"base64": ...}) in the kernel state codec."""
    if isinstance(content, Mapping) and isinstance(content.get("base64"), str):
        return {"$bytes": content["base64"]}
    if isinstance(content, str):
        return {"$bytes": base64.b64encode(content.encode("utf-8")).decode("ascii")}
    raise ValueError(f"file content {content!r} is neither text nor {{'base64': ...}}")


def _view_bytes(content: Any) -> Any:
    """A kernel file content ({"$bytes": <base64>}) as the view's UTF-8 text or {"base64": ...}."""
    if isinstance(content, Mapping) and isinstance(content.get("$bytes"), str):
        raw = base64.b64decode(content["$bytes"])
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return {"base64": content["$bytes"]}
    return content


def kernel_initial_state(view: Mapping[str, Any]) -> dict[str, Any]:
    """Translate a task's initial.kvx (the predicate state-view layout) into kernel module state.

    File contents are bytes in the kernel's state codec (`{"$bytes": <base64>}`) and UTF-8 text or
    `{"base64": ...}` in the view; kv is identical in both layouts. jobs: the view's `submitted_at` is the kernel's
    `submitted`, `fail_at` and `cancelled_at` default to null, and `next_id` continues after the
    largest `j<N>` id so jobs.submit never reuses one. world and gateway are derived, not stored.
    """
    out: dict[str, Any] = {}
    if "fs" in view:
        files = (view["fs"].get("files") or {}).items()
        out["fs"] = {
            "files": {path: _kernel_bytes(content) for path, content in sorted(files)},
            "locks": sorted(view["fs"].get("locks") or []),
        }
    if "kv" in view:
        out["kv"] = view["kv"]
    if "jobs" in view:
        jobs = {}
        numbers = [0]
        for job_id, job in sorted((view["jobs"].get("jobs") or {}).items()):
            jobs[job_id] = {
                "kind": job["kind"],
                "n": job["n"],
                "submitted": job.get("submitted_at", job.get("submitted", 0)),
                "fail_at": job.get("fail_at"),
                "cancelled_at": job.get("cancelled_at"),
            }
            if re.fullmatch(r"j[0-9]+", job_id):
                numbers.append(int(job_id[1:]))
        out["jobs"] = {"next_id": max(numbers) + 1, "jobs": jobs}
    return out


def _job_status(job: Mapping[str, Any], now: int) -> str:
    """refworld jobs status rule (examples/refworld/lib/jobs.py; reference-world.md section 2)."""
    cancelled = job.get("cancelled_at")
    if cancelled is not None and now >= cancelled:
        return "failed"
    start, length = job["submitted"], job["n"]
    fail_at = job.get("fail_at")
    if fail_at is not None and fail_at <= start + length and now >= fail_at:
        return "failed"
    if now < start + 1:
        return "queued"
    if now < start + length:
        return "running"
    return "done"


def predicate_state(
    modules: Mapping[str, Any],
    world: Mapping[str, int],
    idempotency_events: Iterable[Mapping[str, Any]],
    ledger: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """The predicate state view of benchmark/predicates/common.py from the kernel's final state.

    modules is `cwr instance state`, world is {time, turn} of the Instance, idempotency_events the
    gateway's key store (part of Instance state, outside the Build) and ledger the Instance ledger,
    used only to locate the seq of the call that produced each stored outcome.
    """
    now = int(world["time"])
    jobs = {}
    for job_id, job in sorted(((modules.get("jobs") or {}).get("jobs") or {}).items()):
        status = _job_status(job, now)
        entry: dict[str, Any] = {
            "kind": job["kind"],
            "n": job["n"],
            "submitted_at": job["submitted"],
            "status": status,
        }
        if job.get("fail_at") is not None:
            entry["fail_at"] = job["fail_at"]
        if job.get("cancelled_at") is not None:
            entry["cancelled_at"] = job["cancelled_at"]
        if status == "done":
            entry["result"] = f"{job['kind']}:{job_id}:{job['n']}"
        jobs[job_id] = entry
    first_seq: dict[tuple[str, str], int] = {}
    for record in ledger:
        gateway = record.get("gateway")
        if record.get("kind") == "adapter" and isinstance(gateway, Mapping):
            key = (str(record.get("primitive")), str(gateway.get("request_hash")))
            first_seq.setdefault(key, record["seq"])
    store: dict[str, dict[str, Any]] = {}
    for event in idempotency_events:
        adapter, key = event.get("adapter"), event.get("key")
        if event.get("event") == "cleared":
            store.get(adapter, {}).pop(key, None)
        elif event.get("event") == "done" and event.get("outcome") is not None:
            store.setdefault(adapter, {})[key] = {
                "receipt": event["outcome"],
                "payload_hash": event.get("payload_hash"),
                "seq": first_seq.get((adapter, str(event.get("request_hash")))),
            }
    return {
        "meta": {"format": STATE_VIEW_FORMAT, "schema_version": 1},
        "fs": {
            "files": {
                path: _view_bytes(content)
                for path, content in sorted(((modules.get("fs") or {}).get("files") or {}).items())
            },
            "locks": (modules.get("fs") or {}).get("locks") or [],
        },
        "kv": {
            "namespaces": {
                ns: {key: _view_bytes(value) for key, value in sorted(entries.items())}
                for ns, entries in sorted(
                    ((modules.get("kv") or {}).get("namespaces") or {}).items()
                )
            }
        },
        "jobs": {"jobs": jobs},
        "world": {"time": now, "turn": int(world["turn"])},
        "gateway": {"idempotency": {a: dict(sorted(k.items())) for a, k in sorted(store.items())}},
    }


class CwrCliKernel(KernelAdapter):
    """Binding to the real `cwr` CLI (kernel.md section 2; model-surface.md sections 1 and 3).

      create   cwr instance create <build> --seed N --tail K --arm LABEL --model M [--initial F]
               [--allow-experimental] --home H --builds R...
      context  cwr instance context <id> (task 3.1); the documented fallback below otherwise
      turn     cwr instance turn <id> --message - --gateway replay --cassette C
               (or --gateway live [--stub N=U]); the kernel applies the turn protocol
      upgrade  cwr instance upgrade <id> <build> [--allow-experimental]
      state    cwr instance state <id> plus cwr instance show <id> (world time and turn)
      destroy  cwr instance destroy <id>
    The ledger is the Instance's ledger.jsonl, read strictly and exported byte for byte.

    With a CLI that predates task 3.1 the binding assembles the same four components itself
    (manual.md of the Build byte for byte, `cwr instance tail`, the last envelope the kernel printed)
    and applies the turn protocol before submitting: a message without exactly one python block is
    refused with INVALID/turn.protocol, nothing is submitted and no tick passes. `surface()` reports
    which path was used, and the runner records it in the attempt.
    """

    def __init__(
        self,
        gateway_mode: str = "replay",
        binary: Path | None = None,
        *,
        home: Path | None = None,
        builds: Iterable[Path] = (),
        stubs: Mapping[str, str] | None = None,
        timeout_s: float = 900.0,
    ):
        if gateway_mode not in GATEWAY_MODES:
            raise ValueError(f"gateway mode {gateway_mode!r} is not replay or live")
        self.gateway_mode = gateway_mode
        self.binary = Path(binary) if binary else cwr_binary()
        self.home = Path(home) if home else REPO / ".cwr"
        self.builds = [Path(b) for b in builds] or [REPO / "build", REPO / "xbuild"]
        self.stubs = dict(stubs or {})
        self.timeout_s = timeout_s
        self._cassettes: dict[str, Path] = {}
        self._envelopes: dict[str, dict[str, Any]] = {}
        self._kernel_surface: bool | None = None
        self._refusals = 0

    # -- plumbing

    def _run(self, *args: str, stdin: str | None = None, check: bool = True):
        if not self.binary.exists():
            raise KernelCommandError(
                args[0] if args else "",
                127,
                f"cwr binary not found at {self.binary}; build it with `cargo build -p cwr-cli`",
            )
        proc = subprocess.run(
            [str(self.binary), *args],
            input=stdin,
            capture_output=True,
            text=True,
            timeout=self.timeout_s,
            check=False,
        )
        if check and proc.returncode != 0:
            raise KernelCommandError(" ".join(args[:2]), proc.returncode, proc.stderr)
        return proc

    def _home(self) -> list[str]:
        return ["--home", str(self.home)]

    def _roots(self) -> list[str]:
        return [arg for root in self.builds for arg in ("--builds", str(root))]

    def instance_dir(self, instance: str) -> Path:
        return self.home / "instances" / instance

    def has_kernel_surface(self) -> bool:
        """True when the CLI composes the context and applies the turn protocol itself (task 3.1).

        Detected from the usage text: `cwr instance context` and `cwr instance turn --message`.
        """
        if self._kernel_surface is None:
            instance = self._run("instance", check=False)
            turn = self._run("instance", "turn", check=False)
            self._kernel_surface = "instance context" in instance.stdout + instance.stderr and (
                "--message" in turn.stdout + turn.stderr
            )
        return self._kernel_surface

    def _show(self, instance: str) -> dict[str, Any]:
        return json.loads(self._run("instance", "show", instance, *self._home()).stdout)

    # -- KernelAdapter

    def create_instance(
        self,
        *,
        build: str,
        seed: int,
        tail: int | str,
        arm: str,
        model: str,
        allow_experimental: bool,
        initial_state: Path | None,
    ) -> str:
        args = ["instance", "create", build, "--seed", str(seed), "--tail", str(tail)]
        args += ["--arm", arm, "--model", model, *self._home(), *self._roots()]
        if allow_experimental:
            args.append("--allow-experimental")
        if initial_state is not None:
            view = state_view(read_kvx(initial_state))
            text = json.dumps(kernel_initial_state(view), sort_keys=True, separators=(",", ":"))
            path = self.home / "initial" / (hashlib.sha256(text.encode()).hexdigest() + ".json")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            args += ["--initial", str(path)]
        instance = self._run(*args).stdout.strip()
        if not re.fullmatch(r"[0-9A-HJKMNP-TV-Z]{26}", instance):
            raise KernelCommandError("instance create", 0, f"unexpected Instance id {instance!r}")
        return instance

    def bind_cassette(self, instance: str, cassette: Path | None) -> None:
        if cassette is not None:
            self._cassettes[instance] = Path(cassette)

    def context(self, instance: str) -> str:
        if self.has_kernel_surface():
            return self._run("instance", "context", instance, *self._home()).stdout
        meta = self._show(instance)
        manual = (Path(meta["build_dir"]) / "manual.md").read_bytes().decode("utf-8")
        tail = self._run("instance", "tail", instance, *self._home()).stdout
        parts = [manual, FALLBACK_TAIL_HEADING, tail.rstrip("\n")]
        if instance in self._envelopes:
            envelope = json.dumps(self._envelopes[instance], sort_keys=True, separators=(",", ":"))
            parts += [FALLBACK_ENVELOPE_HEADING, envelope]
        return "".join(parts)

    def _gateway_args(self, instance: str) -> list[str]:
        if self.gateway_mode == "live":
            stubs = [arg for n, u in sorted(self.stubs.items()) for arg in ("--stub", f"{n}={u}")]
            return ["--gateway", "live", *stubs]
        cassette = self._cassettes.get(instance)
        if cassette is None:
            raise KernelCommandError(
                "instance turn", 1, "replay mode needs a cassette bound to the Instance"
            )
        return ["--gateway", "replay", "--cassette", str(cassette)]

    def turn(self, instance: str, message: str) -> dict[str, Any]:
        source = ["--message", "-"]
        if not self.has_kernel_surface():
            source = []
            blocks = extract_blocks(message)
            if len(blocks) != 1 or blocks[0][0] != "python" or blocks[0][1] is None:
                reason = (
                    f"the message has {len(blocks)} fenced blocks; exactly one python block is "
                    "required and nothing was executed"
                )
                world = (self._envelopes.get(instance) or {}).get("world")
                envelope = protocol_refusal(reason, world)
                self._refusals += 1
                self._envelopes[instance] = envelope
                return envelope
            message = blocks[0][1] or ""
        args = ["instance", "turn", instance, *source, *self._home(), *self._gateway_args(instance)]
        proc = self._run(*args, stdin=message, check=False)
        if proc.returncode not in (0, 4):
            raise KernelCommandError("instance turn", proc.returncode, proc.stderr)
        lines = [line for line in proc.stdout.splitlines() if line.strip()]
        envelope = json.loads(lines[-1]) if lines else protocol_refusal("no envelope", None)
        self._envelopes[instance] = envelope
        return envelope

    def upgrade(self, instance: str, build: str, *, allow_experimental: bool) -> None:
        args = ["instance", "upgrade", instance, build, *self._home(), *self._roots()]
        if allow_experimental:
            args.append("--allow-experimental")
        self._run(*args)

    def ledger(self, instance: str) -> list[dict[str, Any]]:
        return read_ledger(self.instance_dir(instance) / "ledger.jsonl")

    def export_ledger(self, instance: str, path: Path) -> None:
        source = self.instance_dir(instance) / "ledger.jsonl"
        read_ledger(source)
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, path)

    def state(self, instance: str) -> dict[str, dict[str, Any]]:
        modules = json.loads(self._run("instance", "state", instance, *self._home()).stdout)
        meta = self._show(instance)
        store = self.instance_dir(instance) / "gateway" / "idempotency.jsonl"
        events = []
        if store.exists():
            events = [json.loads(x) for x in store.read_text("utf-8").splitlines() if x.strip()]
        world = {"time": meta["world_time"], "turn": meta["turn"]}
        return predicate_state(modules, world, events, self.ledger(instance))

    def destroy(self, instance: str) -> None:
        self._run("instance", "destroy", instance, *self._home())

    def revisions(self) -> dict[str, Any]:
        digest = None
        if self.binary.exists():
            digest = "sha256:" + hashlib.sha256(self.binary.read_bytes()).hexdigest()
        return {"compiler": None, "kernel": digest, "sdk": None}

    def surface(self) -> dict[str, Any]:
        kernel = self.has_kernel_surface()
        return {
            "context": "kernel" if kernel else "harness-fallback",
            "protocol": "kernel" if kernel else "harness-fallback",
            "harness_protocol_refusals": self._refusals,
        }


def build_manifest(build_dir: Path) -> dict[str, Any]:
    """The [build] section of a Build's manifest.kvx."""
    return parse_kvx((build_dir / "manifest.kvx").read_text("utf-8"), str(build_dir)).get(
        "build", {}
    )


def check_arm_build(arm: Arm, build_dir: Path) -> str | None:
    """Refusal text when the Build is not the arm's CWR Build (requirement 12.1), else None."""
    manifest = build_manifest(build_dir)
    flag = manifest.get("experimental_flag")
    flag = flag if flag is None else "--" + str(flag).lstrip("-")
    if manifest.get("name") != "refworld":
        return f"Build {build_dir.name} is world {manifest.get('name')!r}, not refworld"
    if bool(manifest.get("experimental")) != arm.experimental or flag != arm.flag:
        return (
            f"Build {build_dir.name} carries experimental flag {flag!r}; "
            f"arm {arm.hypothesis}/{arm.key} needs {arm.flag!r}"
        )
    return None


def cassette_set_digest(root: Path) -> str:
    """Digest of a cassette set directory (benchmark/cassettes/FORMAT.md section 5)."""
    lines = []
    for path in sorted(root.rglob("*.jsonl"), key=lambda p: p.relative_to(root).as_posix()):
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        lines.append(f"{path.relative_to(root).as_posix()}\t{digest}\n")
    return "sha256:" + hashlib.sha256("".join(lines).encode()).hexdigest()


# ---------------------------------------------------------------------------------------------
# Episode


@dataclass
class UpgradePlan:
    after_turn: int  # upgrade between this turn and the next (the pair's frozen midpoint)
    build_b: str
    pair: str


@dataclass
class EpisodeSpec:
    hypothesis: str
    arm: Arm
    task: str
    seed: int
    build: str
    predicate_path: Path
    turn_limit: int
    initial_state: Path | None = None
    max_tokens_per_turn: int = 16000
    token_budget: int | None = None
    model_params: dict[str, Any] = field(default_factory=dict)
    upgrade: UpgradePlan | None = None
    results_root: Path = RESULTS
    registered: bool = True
    cassette_digest: str | None = None
    suite_digest: str | None = None
    drift_spec: str | None = None
    bin: str | None = None
    cassette: Path | None = None
    instruction: str | None = None
    reuse_rule: dict[str, str] | None = None
    upgrade_pair: dict[str, Any] | None = None

    @property
    def arm_id(self) -> str:
        return f"{self.hypothesis}/{self.arm.key}"


def cell_dir(spec: EpisodeSpec, mode: str) -> Path:
    root = spec.results_root / "live" if mode == "live" else spec.results_root
    return root / spec.hypothesis.lower() / spec.arm.key / spec.task / str(spec.seed)


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _surface(kernel: Any) -> dict[str, Any]:
    method = getattr(kernel, "surface", None)
    return method() if callable(method) else KernelAdapter.surface(kernel)


def run_episode(
    spec: EpisodeSpec,
    kernel: KernelAdapter,
    model: ModelAdapter,
    predicate: Callable[[Any, list], bool] | None = None,
) -> dict[str, Any]:
    """Run one Instance to its stop condition and write the run layout. Returns the attempt record."""
    mode = kernel.gateway_mode
    if mode not in GATEWAY_MODES:
        raise ValueError(
            f"gateway mode {mode!r}: campaigns run in replay; live is a labelled robustness run"
        )
    predicate = predicate or load_predicate(spec.predicate_path)
    started, t0 = _now(), time.monotonic()
    tokens: list[dict[str, Any]] = []
    reported_models: set[str] = set()
    exit_status, detail, turns = "turn_limit", None, 0
    instance = kernel.create_instance(
        build=spec.build,
        seed=spec.seed,
        tail=spec.arm.tail,
        arm=spec.arm_id,
        model=model.model,
        allow_experimental=spec.arm.experimental,
        initial_state=spec.initial_state,
    )
    if spec.cassette is not None and hasattr(kernel, "bind_cassette"):
        kernel.bind_cassette(instance, spec.cassette)
    task_text = TASK_TEMPLATE.format(instruction=spec.instruction) if spec.instruction else ""
    out = cell_dir(spec, mode)
    if out.exists():
        shutil.rmtree(out)
    (out / "states").mkdir(parents=True)
    ledger: list[dict[str, Any]] = []
    state: dict[str, dict[str, Any]] = {}
    states: dict[int, Any] = {}
    try:
        for number in range(1, spec.turn_limit + 1):
            if spec.upgrade and number == spec.upgrade.after_turn + 1:
                kernel.upgrade(
                    instance, spec.upgrade.build_b, allow_experimental=spec.arm.experimental
                )
            context = kernel.context(instance) + task_text
            try:
                reply = model.complete_context(
                    context, max_tokens=spec.max_tokens_per_turn, params=spec.model_params
                )
            except Exception as error:
                exit_status, detail = "model_error", f"{type(error).__name__}: {error}"
                break
            reported_models.add(str(getattr(reply, "model", model.model)))
            tokens.append(
                {
                    "turn": number,
                    "input": getattr(reply, "input_tokens", None),
                    "output": getattr(reply, "output_tokens", None),
                }
            )
            kernel.turn(instance, reply.text)
            turns = number
            ledger = kernel.ledger(instance)
            state = kernel.state(instance)
            write_kvx(out / "states" / f"turn_{number:04d}.kvx", state)
            states[number] = state_view(state)
            if metrics.has_invariant_violation(ledger):
                exit_status = "invariant_violation"
                break
            if predicate(state_view(state), list(ledger)) is True:
                exit_status = "success"
                break
            spent = sum((t["input"] or 0) + (t["output"] or 0) for t in tokens)
            if spec.token_budget is not None and spent >= spec.token_budget:
                exit_status = "budget"
                break
        ledger = kernel.ledger(instance)
        state = kernel.state(instance)
    except NotImplementedError:
        raise
    except Exception as error:
        exit_status, detail = "kernel_error", f"{type(error).__name__}: {error}"
    finally:
        if ledger:
            export = getattr(kernel, "export_ledger", None)
            if callable(export):
                export(instance, out / "ledger.jsonl")
            else:
                write_ledger(out / "ledger.jsonl", ledger)
        write_kvx(out / "final_state.kvx", state or {"meta": {"empty": True}})
        with contextlib.suppress(Exception):
            kernel.destroy(instance)
    head = ledger[0] if ledger else {}
    surface = _surface(kernel)
    attempt = {
        "hypothesis": spec.hypothesis,
        "arm": spec.arm_id,
        "arm_label": spec.arm.label,
        "task": spec.task,
        "seed": spec.seed,
        "bin": spec.bin,
        "mode": mode,
        "registered": spec.registered,
        "build": head.get("build", spec.build),
        "build_b": spec.upgrade.build_b if spec.upgrade else None,
        "pair": spec.upgrade.pair if spec.upgrade else None,
        "experimental_flag": spec.arm.flag,
        "drift_spec": spec.drift_spec,
        "instance": head.get("instance", instance),
        "cassette": str(spec.cassette) if spec.cassette else None,
        "cassette_digest": head.get("cassette_digest", spec.cassette_digest),
        "suite_digest": spec.suite_digest,
        "model": model.model,
        "model_reported": sorted(reported_models),
        "sampling": spec.model_params,
        "max_tokens_per_turn": spec.max_tokens_per_turn,
        "turn_limit": spec.turn_limit,
        "token_budget": spec.token_budget,
        **{f"revision_{k}": v for k, v in kernel.revisions().items()},
        "context_source": surface["context"],
        "protocol_source": surface["protocol"],
        "harness_protocol_refusals": surface["harness_protocol_refusals"],
        "start": started,
        "end": _now(),
        "duration_s": round(time.monotonic() - t0, 3),
        "exit_status": exit_status,
        "detail": detail,
        "turns": turns,
        "tokens_input_total": sum(t["input"] or 0 for t in tokens),
        "tokens_output_total": sum(t["output"] or 0 for t in tokens),
        "tokens_label": "harness-recorded at the model boundary; not a ledger quantity",
    }
    sections: dict[str, dict[str, Any]] = {"attempt": attempt, "tokens": {"per_turn": tokens}}
    if ledger and exit_status != "kernel_error":
        values = metrics.instance_metrics(
            ledger,
            state_view(state),
            predicate,
            reuse_rule=spec.reuse_rule,
            upgrade_pair=spec.upgrade_pair,
            states_after_turn=states or None,
        )
        sections["metrics"] = {
            "source": "benchmark/harness/metrics.py over the exported ledger and states only",
            "values": values,
        }
    write_kvx(
        out / "attempt.kvx",
        sections,
        header="CWR attempt record written by benchmark/harness/run.py",
    )
    if exit_status == "kernel_error":
        raise RuntimeError(
            f"kernel failed during {spec.arm_id} {spec.task} seed {spec.seed}: {detail}"
        )
    return attempt


# ---------------------------------------------------------------------------------------------
# Command line


def _turn_limit(campaign: dict[str, Any], bin_name: str | None) -> int:
    by_bin = campaign.get("turn_limit_by_bin") or {}
    limit = by_bin.get(bin_name) if bin_name else None
    return int(min(limit or campaign["turn_limit_max"], campaign["turn_limit_max"]))


def _reuse_rule(info: Mapping[str, Any]) -> dict[str, str] | None:
    rule = info.get("reused_rule")
    if info.get("rule_reuse") is False or rule in (None, ""):
        return None
    if isinstance(rule, Mapping):
        return {"primitive": rule["primitive"], "code": rule["code"]}
    match = re.fullmatch(r"([a-z]+\.[a-z_]+)(?:\[[A-Za-z0-9_]+\])?:([A-Z_]+)", str(rule))
    return {"primitive": match.group(1), "code": match.group(2)} if match else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one CWR Instance for an arm, task and seed.")
    parser.add_argument("--hypothesis", required=True, choices=["H1", "H2", "H3", "H4"])
    parser.add_argument("--arm", required=True, help="arm key from arms.kvx, e.g. drift-1")
    parser.add_argument("--task", required=True)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument(
        "--build", help="Build hash for per-task or per-pair bindings (from the campaign file)"
    )
    parser.add_argument(
        "--live", action="store_true", help="labelled live-mode robustness run (never pooled)"
    )
    parser.add_argument(
        "--allow-unregistered",
        action="store_true",
        help="run with a draft registration; the attempt is marked registered=false",
    )
    parser.add_argument("--prereg", default=str(REPO / "benchmark" / "preregistration.kvx"))
    parser.add_argument("--tasks-dir", default=str(REPO / "benchmark" / "tasks"))
    parser.add_argument("--suite", default=str(REPO / "benchmark" / "suite.kvx"))
    parser.add_argument("--results", default=str(RESULTS), help="results root")
    parser.add_argument("--home", help="Instance store (default $CWR_HOME, else .cwr)")
    parser.add_argument(
        "--builds", action="append", default=[], help="Build root to resolve hashes in (repeatable)"
    )
    parser.add_argument(
        "--cassettes", help="cassette set directory holding <task>/seed-<n>.jsonl (replay mode)"
    )
    parser.add_argument("--stub", action="append", default=[], help="live mode: NAME=URL")
    parser.add_argument(
        "--model",
        choices=["gateway", "stub"],
        default="gateway",
        help="gateway: the registered model, credentials read from the environment; "
        "stub: the deterministic offline model",
    )
    parser.add_argument("--turn-limit", type=int, help="lower the registered turn limit")
    parser.add_argument("--max-tokens", type=int, help="lower the registered max tokens per turn")
    args = parser.parse_args(argv)

    prereg = load_prereg(args.prereg)
    try:
        check_digests(prereg)
    except DigestMismatch as error:
        print(f"refused: {error}", file=sys.stderr)
        return 3
    meta, arms = load_arms()
    arm = arms.get((args.hypothesis, args.arm))
    if arm is None:
        print(f"unknown arm {args.hypothesis}/{args.arm}", file=sys.stderr)
        return 2
    missing = missing_fields(prereg, arms, meta["control"])
    if missing and not args.allow_unregistered:
        print("pre-registration incomplete: " + ", ".join(missing), file=sys.stderr)
        return 2
    campaign = prereg["campaign"]
    if args.seed not in campaign["seeds"] and not args.allow_unregistered:
        print(f"seed {args.seed} is not a registered seed", file=sys.stderr)
        return 2
    build = args.build or resolve_build(arm, meta["control"])
    if not build:
        print(
            f"no Build hash for {args.hypothesis}/{args.arm}; pass --build from the campaign file",
            file=sys.stderr,
        )
        return 2
    if args.hypothesis == "H4":
        print(
            "H4 runs need the upgrade plan from benchmark/upgrades.kvx; bound by task 5.3",
            file=sys.stderr,
        )
        return 2
    home = Path(args.home or os.environ.get("CWR_HOME") or ".cwr")
    roots = [Path(b) for b in args.builds] or [REPO / "build", REPO / "xbuild"]
    hexdigest = build.removeprefix("sha256:")
    build_dir = next((r / hexdigest for r in roots if (r / hexdigest).is_dir()), None)
    if build_dir is None:
        print(f"Build {build} not found under {', '.join(map(str, roots))}", file=sys.stderr)
        return 2
    refusal = check_arm_build(arm, build_dir)
    if refusal:
        print(refusal, file=sys.stderr)
        return 2
    suite = read_kvx(args.suite) if Path(args.suite).exists() else {}
    info = suite.get(f"task.{args.task}", {})
    task_dir = Path(args.tasks_dir) / args.task
    cassette, cassette_digest = None, campaign.get("cassette_digest")
    if not args.live:
        if not args.cassettes:
            print("replay mode needs --cassettes <set directory>", file=sys.stderr)
            return 2
        cassette = Path(args.cassettes) / args.task / f"seed-{args.seed}.jsonl"
        if not cassette.is_file():
            print(f"no cassette {cassette} in the set", file=sys.stderr)
            return 2
        cassette_digest = cassette_set_digest(Path(args.cassettes))
        if campaign.get("cassette_digest") and cassette_digest != campaign["cassette_digest"]:
            print("the cassette set digest differs from the registered one", file=sys.stderr)
            return 2
    from .model import GatewayClient, ReferenceStubModel

    if args.model == "stub":
        model = ReferenceStubModel.for_task(args.task, seed=args.seed)
    else:
        model = GatewayClient.from_env()
    if campaign.get("model") and model.model != campaign["model"] and not args.allow_unregistered:
        print("the configured model identity differs from the registered one", file=sys.stderr)
        return 2
    turn_limit = _turn_limit(campaign, info.get("bin"))
    if args.turn_limit is not None:
        turn_limit = min(turn_limit, args.turn_limit)
    max_tokens = campaign.get("max_tokens_per_turn", 16000)
    if args.max_tokens is not None:
        max_tokens = min(max_tokens, args.max_tokens)
    spec = EpisodeSpec(
        hypothesis=args.hypothesis,
        arm=arm,
        task=args.task,
        seed=args.seed,
        build=build,
        predicate_path=task_dir / "success.py",
        initial_state=task_dir / "initial.kvx",
        turn_limit=turn_limit,
        bin=info.get("bin"),
        max_tokens_per_turn=max_tokens,
        token_budget=campaign.get("token_budget_per_instance"),
        model_params=campaign.get("sampling") or {},
        registered=not missing,
        cassette_digest=cassette_digest,
        suite_digest=campaign.get("suite_digest"),
        results_root=Path(args.results),
        cassette=cassette,
        instruction=(task_dir / "task.md").read_text("utf-8"),
        reuse_rule=_reuse_rule(info),
    )
    stubs = dict(s.split("=", 1) for s in args.stub)
    kernel = CwrCliKernel(
        gateway_mode="live" if args.live else "replay", home=home, builds=roots, stubs=stubs
    )
    try:
        attempt = run_episode(spec, kernel, model)
    except (NotImplementedError, KernelCommandError, RuntimeError) as error:
        print(str(error), file=sys.stderr)
        return 3
    print(
        f"{attempt['arm']} {attempt['task']} seed {attempt['seed']} mode {attempt['mode']}: "
        f"{attempt['exit_status']} after {attempt['turns']} turns"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
