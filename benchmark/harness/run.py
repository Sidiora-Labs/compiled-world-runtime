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
  h<N>/<arm>/<task>/<seed>/messages/turn_NNNN.md  the model message the harness submitted as turn N,
                                              byte for byte: the input analyse.py re-submits to replay
                                              state per turn from the ledger (never a metric source)
  h<N>/<arm>/<task>/<seed>/kernel.log         the kernel's stderr of every turn, under `## turn N`
  h<N>/<arm>/<task>/<seed>/attempt.kvx        attempt record (experiments.md section 6) with the
                                              harness's own ledger-only metrics in [metrics], the
                                              compiler, kernel and SDK revisions, the log path and
                                              the registration digests the run was checked against

A cell is written into a fresh staging directory (<results>/_staging) and moved into place only when
complete, so a reader never sees a half-written cell. An existing cell is never deleted: a cell whose
attempt ended in a retryable status (model_error, kernel_error, metrics_error, predicate_error) or
that has no attempt record is moved aside under <results>/_replaced before the new one takes its
place, and a finished cell is refused unless the caller asks for a rerun, which moves it aside too.

Every entry point runs the same pre-flight (`preflight`): registration digests, completeness, seed,
model identity, the arm's registered Build (control, flag, per-task drift or upgrade pair) found on
disk and carrying exactly the arm's flag, the cassette set digest and, for H4, the upgrades digest.

The final state is read only through the kernel's public commands: `cwr instance state` (module
state), `cwr instance keys` (the gateway's key idempotency store, Instance state outside the Build)
and the ledger (world time and turn, the seq of each stored outcome). It is mapped to the predicate
state view by benchmark/predicates/common.py, the mapping task 4.2 defines; job status there is a
pure function of the stored job and the final world time. No file of the Instance store other than
the ledger is opened. Per-turn states are kept in memory for the stop rule and the runner's own
turns to success; they are never written, because analyse.py replays them from the ledger.

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
import tempfile
import threading
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
    sha256_file,
    state_view,
    write_kvx,
    write_ledger,
)
from .prereg import (
    ARMS_PATH,
    DEFAULT_PATH,
    METRICS_PATH,
    DigestMismatch,
    check_digests,
    load_prereg,
    missing_fields,
)

RESULTS = REPO / "benchmark" / "results"
EXIT_STATUSES = (
    "success",
    "turn_limit",
    "budget",
    "model_error",
    "invariant_violation",
    "kernel_error",
    "predicate_error",
    "metrics_error",
)
# Statuses whose cell a later run may replace (the old cell is moved aside, never deleted).
RETRYABLE_EXITS = ("model_error", "kernel_error", "metrics_error", "predicate_error")
GATEWAY_MODES = ("replay", "live")
CAMPAIGNS = REPO / "benchmark" / "campaigns"
SUITE = REPO / "benchmark" / "suite.kvx"
TASKS = REPO / "benchmark" / "tasks"
UPGRADES = REPO / "benchmark" / "upgrades.kvx"
STAGING = "_staging"
REPLACED = "_replaced"
LOG_NAME = "kernel.log"
# A task id or arm key: one path segment, never `..` or a separator.
SAFE_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")

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
        cassette_digest: str | None = None,
    ) -> str:
        """Create the Instance; seed, tail, arm, model and the cassette digest go into its header."""

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

    def turn_log(self, instance: str) -> str:
        """The kernel's diagnostic output of the last turn of this Instance ("" when it has none)."""
        del instance
        return ""


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


def _last_envelope(stdout: str) -> dict[str, Any]:
    """The result envelope `cwr instance turn` printed: its last non-blank stdout line.

    Lines end at LF only. serde_json leaves U+0085, U+2028 and U+2029 raw inside strings, so
    `str.splitlines()` would cut an envelope that carries one. Output that is not one JSON object is
    a KernelCommandError, never a JSONDecodeError.
    """
    lines = [line for line in stdout.split("\n") if line.strip()]
    if not lines:
        return protocol_refusal("no envelope", None)
    try:
        envelope = json.loads(lines[-1])
    except json.JSONDecodeError as error:
        raise KernelCommandError(
            "instance turn", 0, f"stdout line is not a JSON envelope: {error}"
        ) from None
    if not isinstance(envelope, dict):
        raise KernelCommandError("instance turn", 0, "stdout line is not a JSON object")
    return envelope


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


def _kv_bytes(value: Any) -> Any:
    """A view kv value in the kernel state codec: text and {"base64": ...} become bytes (kv values are
    bytes in refworld's kv contract); any other JSON value is passed through unchanged."""
    if isinstance(value, str) or (isinstance(value, Mapping) and "base64" in value):
        return _kernel_bytes(value)
    return value


def kernel_initial_state(view: Mapping[str, Any]) -> dict[str, Any]:
    """Translate a task's initial.kvx (the predicate state-view layout) into kernel module state.

    File contents and kv values are bytes in the kernel's state codec (`{"$bytes": <base64>}`) and UTF-8 text or
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
        kv = view["kv"]
        if set(kv) == {"namespaces"} and isinstance(kv["namespaces"], Mapping):
            out["kv"] = {
                "namespaces": {
                    ns: {key: _kv_bytes(value) for key, value in sorted(entries.items())}
                    for ns, entries in sorted(kv["namespaces"].items())
                }
            }
        else:  # a flat pre-0.2.0 kv (upgrade pair U5, side A)
            out["kv"] = {key: _kv_bytes(value) for key, value in sorted(kv.items())}
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


def _state_view_mapping() -> Any:
    """benchmark/predicates/common.py, imported as the suite's predicates import it (`common`)."""
    predicates = REPO / "benchmark" / "predicates"
    if str(predicates) not in sys.path:
        sys.path.insert(0, str(predicates))
    import common  # type: ignore[import-not-found]

    return common


def predicate_state(
    modules: Mapping[str, Any],
    ledger: list[dict[str, Any]],
    key_events: Iterable[Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    """The predicate state view of an Instance, with a [meta] section, from public kernel outputs.

    modules is `cwr instance state`, ledger the Instance ledger (world time, turn and the seq of the
    call behind each stored outcome) and key_events `cwr instance keys` (the gateway's key store).
    The mapping is benchmark/predicates/common.py `state_view_from_instance` (task 4.2).
    """
    view = _state_view_mapping().state_view_from_instance(
        dict(modules), list(ledger), [dict(e) for e in key_events]
    )
    return {"meta": {"format": STATE_VIEW_FORMAT, "schema_version": 1}, **view}


class CwrCliKernel(KernelAdapter):
    """Binding to the real `cwr` CLI (kernel.md section 2; model-surface.md sections 1 and 3).

      create   cwr instance create <build> --seed N --tail K --arm LABEL --model M [--initial F]
               [--cassette-digest D] [--allow-experimental] --home H --builds R...
      context  cwr instance context <id> (task 3.1); the documented fallback below otherwise
      turn     cwr instance turn <id> --message - --gateway replay --cassette C
               (or --gateway live [--stub N=U]); the kernel applies the turn protocol
      upgrade  cwr instance upgrade <id> <build> [--allow-experimental]
      state    cwr instance state <id> and cwr instance keys <id>, with the ledger
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
        self._turn_stderr: dict[str, str] = {}

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
        cassette_digest: str | None = None,
    ) -> str:
        args = ["instance", "create", build, "--seed", str(seed), "--tail", str(tail)]
        args += ["--arm", arm, "--model", model, *self._home(), *self._roots()]
        if cassette_digest is not None:
            args += ["--cassette-digest", cassette_digest]
        if allow_experimental:
            args.append("--allow-experimental")
        if initial_state is not None:
            view = state_view(read_kvx(initial_state))
            text = json.dumps(kernel_initial_state(view), sort_keys=True, separators=(",", ":"))
            path = self.home / "initial" / (hashlib.sha256(text.encode()).hexdigest() + ".json")
            path.parent.mkdir(parents=True, exist_ok=True)
            # Written atomically: concurrent episodes of one task share this content-addressed file.
            partial = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
            partial.write_text(text, encoding="utf-8")
            os.replace(partial, path)
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
        self._turn_stderr[instance] = proc.stderr
        if proc.returncode not in (0, 4):
            raise KernelCommandError("instance turn", proc.returncode, proc.stderr)
        envelope = _last_envelope(proc.stdout)
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
        keys = json.loads(self._run("instance", "keys", instance, *self._home()).stdout)
        return predicate_state(modules, self.ledger(instance), keys)

    def destroy(self, instance: str) -> None:
        self._run("instance", "destroy", instance, *self._home())

    def revisions(self) -> dict[str, Any]:
        digest = None
        if self.binary.exists():
            digest = "sha256:" + hashlib.sha256(self.binary.read_bytes()).hexdigest()
        return {"compiler": None, "kernel": digest, "sdk": None}

    def turn_log(self, instance: str) -> str:
        return self._turn_stderr.pop(instance, "")

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


def check_build(build_dir: Path, flag: str | None, label: str) -> str | None:
    """Refusal text unless the Build is refworld carrying exactly `flag` (None: no flag)."""
    manifest = build_manifest(build_dir)
    found = manifest.get("experimental_flag")
    found = found if found is None else "--" + str(found).lstrip("-")
    if manifest.get("name") != "refworld":
        return f"Build {build_dir.name} is world {manifest.get('name')!r}, not refworld"
    if bool(manifest.get("experimental")) != (flag is not None) or found != flag:
        return f"Build {build_dir.name} carries experimental flag {found!r}; {label} needs {flag!r}"
    return None


def check_arm_build(arm: Arm, build_dir: Path) -> str | None:
    """Refusal text when the Build is not the arm's CWR Build (requirement 12.1), else None."""
    return check_build(build_dir, arm.flag, f"arm {arm.hypothesis}/{arm.key}")


def tree_digest(root: Path, pattern: str = "*") -> str:
    """Digest of the files under root matching pattern: sha256 of `<relative path>\\t<sha256>\\n`
    lines in path order (benchmark/cassettes/FORMAT.md section 5 for pattern *.jsonl)."""
    lines = []
    files = (p for p in root.rglob(pattern) if p.is_file())
    for path in sorted(files, key=lambda p: p.relative_to(root).as_posix()):
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        lines.append(f"{path.relative_to(root).as_posix()}\t{digest}\n")
    return "sha256:" + hashlib.sha256("".join(lines).encode()).hexdigest()


def cassette_set_digest(root: Path) -> str:
    """Digest of a cassette set directory (benchmark/cassettes/FORMAT.md section 5)."""
    return tree_digest(root, "*.jsonl")


def build_revisions(build_dir: Path) -> dict[str, str | None]:
    """The compiler revision from the Build manifest and the digest of the Build's generated SDK."""
    sdk = build_dir / "sdk"
    return {
        "compiler": build_manifest(build_dir).get("compiler"),
        "sdk": tree_digest(sdk) if sdk.is_dir() else None,
    }


MODEL_RULES = ("exact", "routing_prefix")


def model_matches(reported: str, configured: str, rule: str = "exact") -> bool:
    """Whether the identity the gateway reports is the configured (registered) model.

    exact: identical strings. routing_prefix ([campaign].model_reported_rule): identical, or equal to
    the configured identity without its routing prefix, the text up to and including the first `/`.
    """
    if reported == configured:
        return True
    return (
        rule == "routing_prefix" and "/" in configured and reported == configured.split("/", 1)[1]
    )


# ---------------------------------------------------------------------------------------------
# Episode


@dataclass
class UpgradePlan:
    """Upgrade the Instance to build_b between turn after_turn and the next (H4 pair midpoint)."""

    after_turn: int
    build_b: str
    pair: str


@dataclass
class EpisodeSpec:
    """Everything one Instance run needs; built by `Preflight.episode` for registered campaigns."""

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
    rerun: bool = False
    model_rule: str = "exact"
    registration_digests: dict[str, str] | None = None
    build_revisions: dict[str, str | None] | None = None

    @property
    def arm_id(self) -> str:
        return f"{self.hypothesis}/{self.arm.key}"


class CellExists(RuntimeError):  # noqa: N818 - public name used by callers
    """The cell already holds a finished attempt and the run was not asked to rerun it."""


def validate_cell(spec: EpisodeSpec) -> None:
    """Raise ValueError unless hypothesis, arm, task and seed are safe single path segments."""
    if spec.hypothesis not in ("H1", "H2", "H3", "H4") or spec.arm.hypothesis != spec.hypothesis:
        raise ValueError(f"hypothesis {spec.hypothesis!r} does not match arm {spec.arm_id!r}")
    for what, name in (("arm", spec.arm.key), ("task", spec.task)):
        if not SAFE_NAME.fullmatch(name):
            raise ValueError(f"{what} {name!r} is not a single lower-case path segment")
    if isinstance(spec.seed, bool) or not isinstance(spec.seed, int) or spec.seed < 0:
        raise ValueError(f"seed {spec.seed!r} is not a non-negative integer")


def cell_dir(spec: EpisodeSpec, mode: str) -> Path:
    """results/[live/]h<N>/<arm>/<task>/<seed> of one Instance."""
    root = spec.results_root / "live" if mode == "live" else spec.results_root
    return root / spec.hypothesis.lower() / spec.arm.key / spec.task / str(spec.seed)


def cell_status(path: Path) -> str:
    """missing (no directory), partial (no readable attempt record) or the attempt's exit_status."""
    if not path.exists():
        return "missing"
    try:
        status = read_kvx(path / "attempt.kvx").get("attempt", {}).get("exit_status")
    except (OSError, ValueError):
        return "partial"
    return str(status) if status else "partial"


def _display_path(path: Path) -> str:
    """Repository-relative POSIX path when under the repository, else the absolute path."""
    resolved = path.resolve()
    return resolved.relative_to(REPO).as_posix() if resolved.is_relative_to(REPO) else str(resolved)


def _publish(staging: Path, out: Path, mode_root: Path) -> None:
    """Move a complete staging cell into place, moving any existing cell aside first."""
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        aside = mode_root / REPLACED / out.relative_to(mode_root).parent / f"{out.name}-{stamp}"
        aside.parent.mkdir(parents=True, exist_ok=True)
        os.replace(out, aside)
    os.replace(staging, out)


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _surface(kernel: Any) -> dict[str, Any]:
    method = getattr(kernel, "surface", None)
    return method() if callable(method) else KernelAdapter.surface(kernel)


def _model_failure(error: BaseException) -> str:
    """The attempt detail of a failed model call: category and status only, never the message."""
    category = getattr(error, "category", None)
    if category is None:
        return f"model_error: unexpected {type(error).__name__}"
    status = getattr(error, "status", None)
    attempts = getattr(error, "attempts", None)
    return f"model_error: {category}, status {status}, attempts {attempts}"


def run_episode(
    spec: EpisodeSpec,
    kernel: KernelAdapter,
    model: ModelAdapter,
    predicate: Callable[[Any, list], bool] | None = None,
) -> dict[str, Any]:
    """Run one Instance to its stop condition and write the run layout. Returns the attempt record.

    Refuses (CellExists) a cell that already holds a finished attempt unless spec.rerun is set, and
    raises ValueError for an unsafe cell identity, both before any Instance exists. The Instance is
    always destroyed, the cell appears only complete, and an attempt record is always written once
    the Instance was created. A kernel failure is recorded and then raised as RuntimeError.
    """
    mode = kernel.gateway_mode
    if mode not in GATEWAY_MODES:
        raise ValueError(
            f"gateway mode {mode!r}: campaigns run in replay; live is a labelled robustness run"
        )
    validate_cell(spec)
    out = cell_dir(spec, mode)
    mode_root = out.parents[3]
    existing = cell_status(out)
    if existing not in ("missing", "partial", *RETRYABLE_EXITS) and not spec.rerun:
        raise CellExists(f"{_display_path(out)} already holds a {existing} attempt; pass a rerun")
    predicate = predicate or load_predicate(spec.predicate_path)
    started, t0 = _now(), time.monotonic()
    tokens: list[dict[str, Any]] = []
    model_match: bool | None = None
    exit_status, detail, turns = "turn_limit", None, 0
    instance = kernel.create_instance(
        build=spec.build,
        seed=spec.seed,
        tail=spec.arm.tail,
        arm=spec.arm_id,
        model=model.model,
        allow_experimental=spec.arm.experimental,
        initial_state=spec.initial_state,
        cassette_digest=spec.cassette_digest if mode == "replay" else None,
    )
    ledger: list[dict[str, Any]] = []
    state: dict[str, dict[str, Any]] = {}
    states: dict[int, Any] = {}
    staging: Path | None = None
    try:
        try:
            (mode_root / STAGING).mkdir(parents=True, exist_ok=True)
            prefix = f"{spec.hypothesis.lower()}-{spec.arm.key}-{spec.task}-{spec.seed}-"
            staging = Path(tempfile.mkdtemp(prefix=prefix, dir=mode_root / STAGING))
            (staging / "messages").mkdir()
            if spec.cassette is not None and hasattr(kernel, "bind_cassette"):
                kernel.bind_cassette(instance, spec.cassette)
            task_text = (
                TASK_TEMPLATE.format(instruction=spec.instruction) if spec.instruction else ""
            )
            with (staging / LOG_NAME).open("w", encoding="utf-8") as log:
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
                        exit_status, detail = "model_error", _model_failure(error)
                        break
                    text = getattr(reply, "text", None)
                    if not isinstance(text, str):
                        exit_status, detail = "model_error", "model_error: reply without text"
                        break
                    reported = str(getattr(reply, "model", model.model))
                    model_match = model_matches(reported, model.model, spec.model_rule)
                    if not model_match:
                        exit_status = "model_error"
                        detail = "model_error: identity: the gateway reported another model"
                        break
                    tokens.append(
                        {
                            "turn": number,
                            "input": getattr(reply, "input_tokens", None),
                            "output": getattr(reply, "output_tokens", None),
                        }
                    )
                    (staging / "messages" / f"turn_{number:04d}.md").write_bytes(
                        text.encode("utf-8")
                    )
                    kernel.turn(instance, text)
                    turn_log = getattr(kernel, "turn_log", None)
                    log.write(f"## turn {number}\n")
                    log.write(turn_log(instance) if callable(turn_log) else "")
                    turns = number
                    ledger = kernel.ledger(instance)
                    state = kernel.state(instance)
                    if ledger and ledger[-1].get("kind") == "turn":
                        states[ledger[-1]["turn"]] = state_view(state)
                    if metrics.has_invariant_violation(ledger):
                        exit_status = "invariant_violation"
                        break
                    try:
                        satisfied = predicate(state_view(state), list(ledger)) is True
                    except Exception as error:
                        exit_status = "predicate_error"
                        detail = f"predicate_error: {type(error).__name__}: {error}"
                        break
                    if satisfied:
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
            try:
                if staging is not None and ledger:
                    export = getattr(kernel, "export_ledger", None)
                    if callable(export):
                        export(instance, staging / "ledger.jsonl")
                    else:
                        write_ledger(staging / "ledger.jsonl", ledger)
                if staging is not None:
                    write_kvx(staging / "final_state.kvx", state or {"meta": {"empty": True}})
            except Exception as error:
                exit_status, detail = "kernel_error", f"export: {type(error).__name__}: {error}"
            finally:
                with contextlib.suppress(Exception):
                    kernel.destroy(instance)
        if staging is None:
            raise RuntimeError(f"no staging directory for {spec.arm_id} {spec.task}: {detail}")
        attempt, sections = _attempt_record(
            spec, kernel, model, mode, instance, ledger, tokens, model_match, out
        )
        attempt.update(
            start=started,
            end=_now(),
            duration_s=round(time.monotonic() - t0, 3),
            turns=turns,
        )
        if ledger and exit_status not in ("kernel_error", "predicate_error"):
            try:
                values = metrics.instance_metrics(
                    ledger,
                    state_view(state),
                    predicate,
                    reuse_rule=spec.reuse_rule,
                    upgrade_pair=spec.upgrade_pair,
                    states_after_turn=states or None,
                )
            except Exception as error:
                detail = f"metrics_error after {exit_status}: {type(error).__name__}: {error}"
                exit_status = "metrics_error"
            else:
                sections["metrics"] = {
                    "source": "benchmark/harness/metrics.py over the ledger, the final state and "
                    "the states observed after each turn",
                    "values": values,
                }
        attempt.update(exit_status=exit_status, detail=detail)
        write_kvx(
            staging / "attempt.kvx",
            sections,
            header="CWR attempt record written by benchmark/harness/run.py",
        )
        _publish(staging, out, mode_root)
        staging = None
    finally:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)
    if exit_status == "kernel_error":
        raise RuntimeError(
            f"kernel failed during {spec.arm_id} {spec.task} seed {spec.seed}: {detail}"
        )
    return attempt


def _attempt_record(
    spec: EpisodeSpec,
    kernel: Any,
    model: ModelAdapter,
    mode: str,
    instance: str,
    ledger: list[dict[str, Any]],
    tokens: list[dict[str, Any]],
    model_match: bool | None,
    out: Path,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """The attempt section (experiments.md section 6) and the record's sections, without metrics."""
    head = ledger[0] if ledger else {}
    surface = _surface(kernel)
    revisions = dict(kernel.revisions())
    for key, value in (spec.build_revisions or {}).items():
        revisions[key] = value if value is not None else revisions.get(key)
    attempt = {
        "hypothesis": spec.hypothesis,
        "arm": spec.arm_id,
        "arm_label": spec.arm.label,
        "task": spec.task,
        "seed": spec.seed,
        "bin": spec.bin,
        "mode": mode,
        "registered": spec.registered,
        "registration_digests": spec.registration_digests,
        "build": head.get("build", spec.build),
        "build_b": spec.upgrade.build_b if spec.upgrade else None,
        "pair": spec.upgrade.pair if spec.upgrade else None,
        "experimental_flag": spec.arm.flag,
        "drift_spec": spec.drift_spec,
        "instance": head.get("instance", instance),
        "cassette": str(spec.cassette) if spec.cassette else None,
        "cassette_digest": head.get("cassette_digest", spec.cassette_digest),
        "initial_state": str(spec.initial_state) if spec.initial_state else None,
        "build_roots": [str(root) for root in getattr(kernel, "builds", [])],
        "suite_digest": spec.suite_digest,
        "model": model.model,
        "model_reported_match": model_match,
        "model_reported_rule": spec.model_rule,
        "sampling": spec.model_params,
        "max_tokens_per_turn": spec.max_tokens_per_turn,
        "turn_limit": spec.turn_limit,
        "token_budget": spec.token_budget,
        "revision_compiler": revisions.get("compiler"),
        "revision_kernel": revisions.get("kernel"),
        "revision_sdk": revisions.get("sdk"),
        "log": _display_path(out / LOG_NAME),
        "context_source": surface["context"],
        "protocol_source": surface["protocol"],
        "harness_protocol_refusals": surface["harness_protocol_refusals"],
        "tokens_input_total": sum(t["input"] or 0 for t in tokens),
        "tokens_output_total": sum(t["output"] or 0 for t in tokens),
        "tokens_label": "harness-recorded at the model boundary; not a ledger quantity",
    }
    return attempt, {"attempt": attempt, "tokens": {"per_turn": tokens}}


# ---------------------------------------------------------------------------------------------
# Pre-flight shared by every entry point (run.py main, campaigns/h1h2.py, the task 5.3 driver)


class PreflightRefused(Exception):  # noqa: N818 - public name used by callers
    """A cell may not run. code 3: a registration digest differs; code 2: any other refusal."""

    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Preflight:
    """The checked inputs of one cell; `episode()` turns them into an EpisodeSpec."""

    hypothesis: str
    arm: Arm
    task: str
    seed: int
    campaign: dict[str, Any]
    build: str
    build_dir: Path
    revisions: dict[str, str | None]
    registered: bool
    missing: list[str]
    digests: dict[str, str]
    cassette: Path | None
    cassette_digest: str | None
    task_dir: Path
    predicate_path: Path
    initial_state: Path | None
    bin: str | None
    reuse_rule: dict[str, str] | None
    drift_spec: str | None = None
    upgrade: UpgradePlan | None = None
    upgrade_pair: dict[str, Any] | None = None

    def episode(
        self,
        *,
        results_root: Path = RESULTS,
        turn_limit: int | None = None,
        max_tokens: int | None = None,
        rerun: bool = False,
    ) -> EpisodeSpec:
        """The EpisodeSpec of this cell; turn_limit and max_tokens may only lower the registration."""
        campaign = self.campaign
        limit = _turn_limit(campaign, self.bin)
        tokens = int(campaign.get("max_tokens_per_turn", 16000))
        return EpisodeSpec(
            hypothesis=self.hypothesis,
            arm=self.arm,
            task=self.task,
            seed=self.seed,
            build=self.build,
            predicate_path=self.predicate_path,
            initial_state=self.initial_state,
            turn_limit=min(limit, turn_limit) if turn_limit is not None else limit,
            bin=self.bin,
            max_tokens_per_turn=min(tokens, max_tokens) if max_tokens is not None else tokens,
            token_budget=campaign.get("token_budget_per_instance"),
            model_params=campaign.get("sampling") or {},
            upgrade=self.upgrade,
            results_root=results_root,
            registered=self.registered,
            cassette_digest=self.cassette_digest,
            suite_digest=campaign.get("suite_digest"),
            drift_spec=self.drift_spec,
            cassette=self.cassette,
            instruction=(self.task_dir / "task.md").read_text("utf-8"),
            reuse_rule=self.reuse_rule,
            upgrade_pair=self.upgrade_pair,
            rerun=rerun,
            model_rule=campaign.get("model_reported_rule") or "exact",
            registration_digests=dict(self.digests),
            build_revisions=dict(self.revisions),
        )


def _find_build(build: str, roots: Iterable[Path]) -> Path | None:
    digest = build.removeprefix("sha256:")
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        return None
    return next((r / digest for r in roots if (r / digest).is_dir()), None)


def _campaign_file(campaigns_dir: Path, hypothesis: str) -> dict[str, dict[str, Any]]:
    path = campaigns_dir / f"{hypothesis.lower()}.kvx"
    if not path.is_file():
        raise PreflightRefused(2, f"{_display_path(path)} is not registered")
    return read_kvx(path)


def _pair_path(pair_name: str | None, key: str, value: Any) -> Path:
    """A path named by an h4.kvx pair, relative to the repository root; refused unless a string."""
    if not isinstance(value, str) or not value:
        raise PreflightRefused(2, f"pair {pair_name} {key} must be a path string, not {value!r}")
    return REPO / value


def _pair_inputs(
    pair_name: str | None, pair: Mapping[str, Any]
) -> tuple[int, dict[str, Any], Path, Path, Path]:
    """The checked inputs of one h4.kvx pair: upgrade turn, metric pair, task dir, predicate, initial.

    A missing or mistyped field is a PreflightRefused (code 2) naming the pair and the field.
    """
    after = pair.get("upgrade_after_turn")
    if isinstance(after, bool) or not isinstance(after, int) or after < 1:
        raise PreflightRefused(
            2, f"pair {pair_name} upgrade_after_turn must be a positive integer, not {after!r}"
        )
    changed = pair.get("changed_primitives") or []
    if not isinstance(changed, list) or not all(isinstance(c, str) and c for c in changed):
        raise PreflightRefused(
            2, f"pair {pair_name} changed_primitives must be a list of primitive names"
        )
    misuse_code = pair.get("misuse_code")
    if misuse_code is not None and not isinstance(misuse_code, str):
        raise PreflightRefused(2, f"pair {pair_name} misuse_code must be a string or null")
    scope = pair.get("misuse_scope", "call")
    if scope not in metrics.MISUSE_SCOPES:
        raise PreflightRefused(
            2, f"pair {pair_name} misuse_scope {scope!r} is not one of {metrics.MISUSE_SCOPES}"
        )
    task_dir = _pair_path(pair_name, "task_dir", pair.get("task_dir"))
    predicate = pair.get("predicate", f"{pair.get('task_dir')}/success.py")
    predicate_path = _pair_path(pair_name, "predicate", predicate)
    if pair.get("initial"):
        initial = _pair_path(pair_name, "initial", pair["initial"])
    else:
        initial = task_dir / "initial.kvx"
    metric_pair = {
        "changed_primitives": list(changed),
        "misuse_code": misuse_code,
        "misuse_scope": scope,
    }
    return after, metric_pair, task_dir, predicate_path, initial


def preflight(
    hypothesis: str,
    arm_key: str,
    task: str,
    seed: int,
    *,
    model_identity: str,
    build_roots: Iterable[Path],
    cassettes: Path | None,
    build: str | None = None,
    allow_unregistered: bool = False,
    prereg_path: Path = DEFAULT_PATH,
    arms_path: Path = ARMS_PATH,
    metrics_path: Path = METRICS_PATH,
    suite_path: Path = SUITE,
    tasks_dir: Path = TASKS,
    upgrades_path: Path = UPGRADES,
    campaigns_dir: Path = CAMPAIGNS,
) -> Preflight:
    """Check one cell against the registration and resolve its inputs; raise PreflightRefused.

    Covers: registration digests of metrics.py and arms.kvx (code 3), completeness, seed membership
    and model identity (refused unless allow_unregistered, which marks the attempt unregistered),
    the task (a suite task, or a registered upgrade-pair task for H4), the arm's registered Build
    (arms.kvx for control and flag arms, campaigns/h1.kvx for per-task drift arms, campaigns/h4.kvx
    Build A and per-arm Build B for pair arms; a `build` argument must equal it), each Build present
    under build_roots with exactly the right flag, the cassette set digest (cassettes None means a
    live run) and, for H4, the upgrades digest. Nothing is written.
    """
    roots = [Path(r) for r in build_roots]
    try:
        prereg = load_prereg(prereg_path)
        digests = check_digests(prereg, metrics_path=metrics_path, arms_path=arms_path)
        meta, arms = load_arms(arms_path)
    except DigestMismatch as error:
        raise PreflightRefused(3, str(error)) from None
    except (ValueError, OSError) as error:
        raise PreflightRefused(2, f"registration unusable: {error}") from None
    digests = {"preregistration": sha256_file(prereg_path), **digests}
    arm = arms.get((hypothesis, arm_key))
    if arm is None:
        raise PreflightRefused(2, f"unknown arm {hypothesis}/{arm_key}")
    if not SAFE_NAME.fullmatch(task) or isinstance(seed, bool) or seed < 0:
        raise PreflightRefused(2, f"task {task!r} or seed {seed!r} is not a valid cell identity")
    campaign = prereg["campaign"]
    missing = missing_fields(prereg, arms, meta["control"])
    if missing and not allow_unregistered:
        raise PreflightRefused(2, "pre-registration incomplete: " + ", ".join(missing))
    if seed not in campaign["seeds"] and not allow_unregistered:
        raise PreflightRefused(2, f"seed {seed} is not a registered seed")
    if campaign.get("model") and model_identity != campaign["model"] and not allow_unregistered:
        raise PreflightRefused(2, "the configured model identity differs from the registered one")
    rule = campaign.get("model_reported_rule") or "exact"
    if rule not in MODEL_RULES:
        raise PreflightRefused(2, f"[campaign].model_reported_rule {rule!r} is not {MODEL_RULES}")

    drift_spec, upgrade, upgrade_pair, extra = None, None, None, []
    if arm.build_binding == "pair_b":
        h4 = _campaign_file(campaigns_dir, hypothesis)
        pair_name, pair = next(
            (
                (n.split(".", 1)[1], b)
                for n, b in h4.items()
                if n.startswith("pair.") and b.get("task") == task
            ),
            (None, None),
        )
        if pair is None:
            raise PreflightRefused(2, f"task {task} is not a registered upgrade-pair task")
        registered_build = pair.get("build_a")
        build_b = (pair.get("build_b") or {}).get(arm.key)
        if not registered_build or not build_b:
            raise PreflightRefused(
                2, f"pair {pair_name} registers no build_a or build_b[{arm.key}]"
            )
        after, upgrade_pair, task_dir, predicate_path, initial = _pair_inputs(pair_name, pair)
        upgrade = UpgradePlan(after, build_b, pair_name)
        bin_name, info = pair.get("bin"), {}
        extra.append((build_b, arm.flag, f"Build B of arm {arm.hypothesis}/{arm.key}"))
        registered_upgrades = campaign.get("upgrades_digest")
        if registered_upgrades and sha256_file(upgrades_path) != registered_upgrades:
            raise PreflightRefused(2, "the upgrades.kvx digest differs from the registered one")
    else:
        suite = read_kvx(suite_path) if suite_path.is_file() else {}
        info = suite.get(f"task.{task}")
        if info is None:
            raise PreflightRefused(2, f"task {task} is not a task of {_display_path(suite_path)}")
        if arm.build_binding == "per_task":
            section = _campaign_file(campaigns_dir, hypothesis).get(f"task.{task}", {})
            key = arm.key.replace("-", "_")
            registered_build = section.get(f"{key}_build")
            drift_spec = section.get(f"{key}_spec")
        else:
            registered_build = resolve_build(arm, meta["control"])
        task_dir = tasks_dir / task
        predicate_path = task_dir / "success.py"
        initial = task_dir / "initial.kvx"
        bin_name = info.get("bin")
    same_build = build is not None and registered_build is not None
    if same_build:
        # A Build reference is `sha256:<hex>` or the bare hex directory name, as everywhere in cwr.
        same_build = build.removeprefix("sha256:") == str(registered_build).removeprefix("sha256:")
    if build is not None and registered_build and not same_build:
        # The flag check runs first so a wrong Build is named by what it carries.
        found = _find_build(build, roots)
        refusal = check_arm_build(arm, found) if found is not None and upgrade is None else None
        raise PreflightRefused(
            2, refusal or f"Build {build} is not the registered Build {registered_build}"
        )
    build = registered_build or build
    if not build:
        raise PreflightRefused(2, f"no Build is registered for {hypothesis}/{arm_key} {task}")
    build_dir = _find_build(build, roots)
    if build_dir is None:
        raise PreflightRefused(2, f"Build {build} not found under {', '.join(map(str, roots))}")
    checks = [
        (build, None, "Build A")
        if upgrade
        else (build, arm.flag, f"arm {arm.hypothesis}/{arm.key}")
    ]
    for hash_, flag, label in checks + extra:
        found = build_dir if hash_ == build else _find_build(hash_, roots)
        if found is None:
            raise PreflightRefused(2, f"Build {hash_} not found under {', '.join(map(str, roots))}")
        refusal = check_build(found, flag, label)
        if refusal:
            raise PreflightRefused(2, refusal)

    cassette, cassette_digest = None, None
    if cassettes is not None:
        cassette = Path(cassettes) / task / f"seed-{seed}.jsonl"
        if not cassette.is_file():
            raise PreflightRefused(2, f"no cassette {cassette} in the set")
        cassette_digest = cassette_set_digest(Path(cassettes))
        registered_set = prereg.get(f"hypothesis.{hypothesis}", {}).get(
            "cassette_digest"
        ) or campaign.get("cassette_digest")
        if registered_set and cassette_digest != registered_set:
            raise PreflightRefused(2, "the cassette set digest differs from the registered one")
    registered = (
        not missing and seed in campaign["seeds"] and model_identity == campaign.get("model")
    )
    return Preflight(
        hypothesis=hypothesis,
        arm=arm,
        task=task,
        seed=seed,
        campaign=campaign,
        build=build,
        build_dir=build_dir,
        revisions=build_revisions(build_dir),
        registered=registered,
        missing=missing,
        digests=digests,
        cassette=cassette,
        cassette_digest=cassette_digest,
        task_dir=task_dir,
        predicate_path=predicate_path,
        initial_state=initial if initial.is_file() else None,
        bin=bin_name,
        reuse_rule=_reuse_rule(info),
        drift_spec=drift_spec,
        upgrade=upgrade,
        upgrade_pair=upgrade_pair,
    )


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


EXIT_CODES = """exit codes:
  0  the Instance ran and its attempt record was written (any exit_status but kernel_error)
  2  refused before any Instance was created: inputs, registration, Build, cassette set, an
     existing finished cell without --rerun, or missing gateway configuration
  3  a registration digest (metrics.py or arms.kvx) differs from the pre-registration
  4  the episode failed: kernel error, unreadable ledger or KVX, or an unexpected harness error;
     the attempt record names it when the Instance was created"""


def main(argv: list[str] | None = None) -> int:
    """Run one cell through the shared pre-flight and run_episode; see EXIT_CODES."""
    parser = argparse.ArgumentParser(
        description="Run one CWR Instance for an arm, task and seed.",
        epilog=EXIT_CODES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--hypothesis", required=True, choices=["H1", "H2", "H3", "H4"])
    parser.add_argument("--arm", required=True, help="arm key from arms.kvx, e.g. drift-1")
    parser.add_argument("--task", required=True)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument(
        "--build",
        help="expected Build hash; refused unless it equals the registered Build of the cell",
    )
    parser.add_argument(
        "--live", action="store_true", help="labelled live-mode robustness run (never pooled)"
    )
    parser.add_argument(
        "--allow-unregistered",
        action="store_true",
        help="run with a draft registration; the attempt is marked registered=false",
    )
    parser.add_argument(
        "--rerun", action="store_true", help="replace a finished cell (moved aside)"
    )
    parser.add_argument("--prereg", default=str(DEFAULT_PATH))
    parser.add_argument(
        "--arms",
        default=str(ARMS_PATH),
        help="arm registration; its sha256 must equal the pre-registration's arms_kvx digest",
    )
    parser.add_argument("--tasks-dir", default=str(TASKS))
    parser.add_argument("--suite", default=str(SUITE))
    parser.add_argument("--campaigns", default=str(CAMPAIGNS))
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
        help="gateway: the registered model, configured from the process environment; "
        "stub: the deterministic offline model",
    )
    parser.add_argument("--turn-limit", type=int, help="lower the registered turn limit")
    parser.add_argument("--max-tokens", type=int, help="lower the registered max tokens per turn")
    args = parser.parse_args(argv)

    if not args.live and not args.cassettes:
        print("replay mode needs --cassettes <set directory>", file=sys.stderr)
        return 2
    if not SAFE_NAME.fullmatch(args.task) or not SAFE_NAME.fullmatch(args.arm) or args.seed < 0:
        print("refused: task, arm and seed must be single path segments", file=sys.stderr)
        return 2
    from .model import GatewayClient, ModelError, ReferenceStubModel

    try:
        if args.model == "stub":
            model: Any = ReferenceStubModel.for_task(args.task, seed=args.seed)
        else:
            model = GatewayClient.from_env()
    except ModelError as error:
        print(f"refused: {error}", file=sys.stderr)
        return 2
    roots = [Path(b) for b in args.builds] or [REPO / "build", REPO / "xbuild"]
    try:
        checked = preflight(
            args.hypothesis,
            args.arm,
            args.task,
            args.seed,
            model_identity=model.model,
            build_roots=roots,
            cassettes=None if args.live else Path(args.cassettes),
            build=args.build,
            allow_unregistered=args.allow_unregistered,
            prereg_path=Path(args.prereg),
            arms_path=Path(args.arms),
            suite_path=Path(args.suite),
            tasks_dir=Path(args.tasks_dir),
            campaigns_dir=Path(args.campaigns),
        )
        spec = checked.episode(
            results_root=Path(args.results),
            turn_limit=args.turn_limit,
            max_tokens=args.max_tokens,
            rerun=args.rerun,
        )
        if (
            cell_status(cell_dir(spec, "live" if args.live else "replay"))
            not in (
                "missing",
                "partial",
                *RETRYABLE_EXITS,
            )
            and not args.rerun
        ):
            raise PreflightRefused(2, "the cell already holds a finished attempt; pass --rerun")
    except PreflightRefused as error:
        print(f"refused: {error}", file=sys.stderr)
        return error.code
    home = Path(args.home or os.environ.get("CWR_HOME") or ".cwr")
    stubs = dict(s.split("=", 1) for s in args.stub)
    kernel = CwrCliKernel(
        gateway_mode="live" if args.live else "replay", home=home, builds=roots, stubs=stubs
    )
    try:
        attempt = run_episode(spec, kernel, model)
    except (NotImplementedError, RuntimeError, ValueError, OSError) as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 4
    print(
        f"{attempt['arm']} {attempt['task']} seed {attempt['seed']} mode {attempt['mode']}: "
        f"{attempt['exit_status']} after {attempt['turns']} turns"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
