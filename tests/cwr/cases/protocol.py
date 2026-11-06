# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""The qualification protocol of tests/cwr/qualify.py, shared by every task driver.

A driver records its checks per specification clause, as before. It also wraps each group of checks
in `observer.observe("CWR-NNN.aK", ...)`, which attributes every check recorded inside the block to
those registered assertions. When the runner starts the driver (CWR_QUALIFY=1), `observer.emit()`
prints one line per assertion of every registered subset the task owns:

  ASSERT CWR-NNN.aK PASS|FAIL <diagnostic>

An assertion passes only when at least one check was attributed to it and every attributed check
passed. A registered assertion that no block observed is printed as FAIL, so a driver always reports
its whole subset and a mapping gap can never pass silently. `observer.blocked(reason)` prints a
`BLOCKED <reason>` line for a missing resource.

The observer also writes $CWR_EVIDENCE_DIR/attempt.json from the Builds, Instances and final state a
driver declares: ledgers and final state files are copied into the evidence directory and named by
their path there, and experimental flags are written in the form the kernel stamps on ledger records
(`x-<flag>`). Outside the runner nothing is printed or written beyond the usual clause lines.

Standard library only. The registry is read with a strict line parser of its own; nothing here
imports the runner, the checker or the product.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
REGISTRY = REPO / "tests/cwr/registry.kvx"
ASSERTION = re.compile(r"^CWR-\d{3}\.a[1-9]\d*$")
FLAG = re.compile(r"^(?:--)?(x-[a-z][a-z-]*)$")
SHA = re.compile(r"^sha256:[0-9a-f]{64}$")
DIAGNOSTIC_LIMIT = 1500

# A snapshot maps a key to an append-only list of recorded checks. An item is either a label, whose
# outcome is the second element of its key (for example (clause, True) for a passing check), or an
# (ok, label) pair.
Snapshot = Callable[[], dict]


def registered_subsets(task: str, registry: Path = REGISTRY) -> dict[str, list[str]]:
    """case -> the assertion keys the registry assigns to task, for every case that names the task."""
    out: dict[str, list[str]] = {}
    section = None
    for raw in registry.read_text().splitlines():
        line = raw.strip()
        header = re.fullmatch(r"\[([A-Za-z0-9_.-]+)\]", line)
        if header:
            section = header.group(1)
            continue
        match = re.fullmatch(r"subsets\s*=\s*(.+)", line)
        if match and section and section.startswith("case."):
            subsets = json.loads(match.group(1))
            if isinstance(subsets, dict) and isinstance(subsets.get(task), list):
                out[section[5:]] = [str(k) for k in subsets[task]]
    return out


def ok_bad(results) -> Snapshot:
    """The snapshot of a results object with `ok` and `bad` dicts of label lists per clause."""
    return lambda: {
        **{(c, True): labels for c, labels in results.ok.items()},
        **{(c, False): labels for c, labels in results.bad.items()},
    }


def pairs(store: dict) -> Snapshot:
    """The snapshot of a dict of lists of (ok, label) pairs."""
    return lambda: dict(store)


def _check(key, item) -> tuple[bool, str]:
    if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[0], bool):
        return item[0], str(item[1])
    ok = key[1] if isinstance(key, tuple) and len(key) == 2 and isinstance(key[1], bool) else False
    return ok, str(item)


class Observer:
    """Attributes driver checks to registered assertions and speaks the runner protocol."""

    def __init__(self, task: str, snapshot: Snapshot, registry: Path = REGISTRY) -> None:
        self.task = task
        self.snapshot = snapshot
        self.subsets = registered_subsets(task, registry)
        self.checks: dict[str, list[tuple[bool, str]]] = {}
        self.qualifying = os.environ.get("CWR_QUALIFY") == "1"
        directory = os.environ.get("CWR_EVIDENCE_DIR")
        self.evidence = Path(directory) if self.qualifying and directory else None
        self.manifest: dict = {}
        self.instances: list[dict] = []
        self.final_states: list[str] = []
        self.builds: list[str] = []
        self.flags: list[str] = []

    # ------------------------------------------------------------------ attribution

    def _lengths(self) -> dict:
        return {key: len(items) for key, items in self.snapshot().items()}

    def _since(self, before: dict) -> list[tuple[bool, str]]:
        out = []
        for key, items in self.snapshot().items():
            out += [_check(key, item) for item in items[before.get(key, 0) :]]
        return out

    @contextmanager
    def observe(self, *assertions: str, match: str | None = None) -> Iterator[None]:
        """Attribute every check recorded inside the block (only those whose label matches the regular
        expression `match`, when given) to each named assertion."""
        for ident in assertions:
            if not ASSERTION.match(ident):
                raise ValueError(f"not an assertion id: {ident!r}")
        before = self._lengths()
        try:
            yield
        finally:
            new = self._since(before)
            if match is not None:
                new = [(ok, label) for ok, label in new if re.search(match, label)]
            for ident in assertions:
                self.checks.setdefault(ident, []).extend(new)

    def attribute(self, assertion: str, ok: bool, label: str) -> None:
        """Attribute one observation directly (for a check that is not recorded per clause)."""
        if not ASSERTION.match(assertion):
            raise ValueError(f"not an assertion id: {assertion!r}")
        self.checks.setdefault(assertion, []).append((bool(ok), label))

    def claim(self, *assertions: str, match: str | None = None, clause: str | None = None) -> None:
        """Attribute every check recorded so far whose label matches the regular expression `match`
        and whose clause is `clause` (each when given) to each named assertion. For checks made in
        loops or conditions that an observe block cannot wrap."""
        for ident in assertions:
            if not ASSERTION.match(ident):
                raise ValueError(f"not an assertion id: {ident!r}")
        picked = []
        for key, items in self.snapshot().items():
            key_clause = key[0] if isinstance(key, tuple) else key
            if clause is not None and key_clause != clause:
                continue
            for item in items:
                ok, label = _check(key, item)
                if match is None or re.search(match, label):
                    picked.append((ok, label))
        for ident in assertions:
            self.checks.setdefault(ident, []).extend(picked)

    def outcomes(self) -> dict[str, tuple[bool, str]]:
        """assertion id -> (passed, diagnostic) for every assertion of the task's registered subsets."""
        out = {}
        for case, keys in sorted(self.subsets.items()):
            for key in keys:
                ident = f"{case}.{key}"
                checks = self.checks.get(ident, [])
                failed = [label for ok, label in checks if not ok]
                if not checks:
                    out[ident] = (False, f"no check of the task {self.task} driver observed it")
                elif failed:
                    out[ident] = (
                        False,
                        f"{len(failed)} of {len(checks)} checks failed: " + " | ".join(failed),
                    )
                else:
                    out[ident] = (
                        True,
                        f"{len(checks)} checks: " + " | ".join(label for _, label in checks),
                    )
        return out

    # ------------------------------------------------------------------ evidence

    def add_build(self, build: str) -> None:
        if isinstance(build, str) and SHA.match(build) and build not in self.builds:
            self.builds.append(build)

    def add_flag(self, flag: str) -> None:
        match = FLAG.match(flag) if isinstance(flag, str) else None
        if match and match.group(1) not in self.flags:
            self.flags.append(match.group(1))

    def set(self, key: str, value) -> None:
        """Set a scalar attempt.json key (world, cassette_digest, gateway_mode, model, sandbox, ...)."""
        self.manifest[key] = value

    def _copy(self, source: Path, folder: str, name: str) -> str | None:
        if self.evidence is None or not Path(source).is_file():
            return None
        rel = f"{folder}/{name}"
        target = self.evidence / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        return rel

    def add_instance(
        self,
        instance_id: str,
        ledger: Path,
        *,
        seed: int,
        tail_size,
        build: str,
        final_state: Path | None = None,
        **extra,
    ) -> None:
        """Declare an Instance; its ledger and final state are copied into the evidence directory."""
        self.add_build(build)
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", instance_id)
        ledger_rel = self._copy(ledger, "ledgers", f"{safe}.jsonl")
        if ledger_rel is None:
            return
        state_rel = (
            self._copy(final_state, "states", f"{safe}{Path(final_state).suffix or '.json'}")
            if final_state is not None
            else None
        )
        entry = {
            "id": instance_id,
            "seed": seed,
            "tail_size": tail_size,
            "build": build,
            "ledger": ledger_rel,
            "final_state": state_rel,
        }
        entry.update({k: v for k, v in extra.items() if v is not None})
        self.instances.append(entry)

    def add_final_state(self, source: Path, name: str) -> None:
        rel = self._copy(source, "final", name)
        if rel is not None and rel not in self.final_states:
            self.final_states.append(rel)

    def _write_attempt(self) -> None:
        if self.evidence is None:
            return
        if not (self.builds or self.instances or self.final_states or self.manifest):
            return
        manifest = dict(self.manifest)
        if self.builds:
            manifest["builds"] = list(self.builds)
            manifest.setdefault("build_hash", self.builds[0] if len(self.builds) == 1 else None)
        if self.flags:
            manifest["experimental_flags"] = list(self.flags)
        if self.instances:
            manifest["instances"] = list(self.instances)
        if self.final_states:
            manifest["final_states"] = list(self.final_states)
        (self.evidence / "attempt.json").write_text(json.dumps(manifest, indent=2, sort_keys=True))

    # ------------------------------------------------------------------ protocol lines

    def blocked(self, reason: str) -> None:
        if self.qualifying:
            print(f"BLOCKED {reason}", flush=True)

    def emit(self) -> bool:
        """Print the ASSERT lines (under the runner) and write attempt.json; True when all passed."""
        outcomes = self.outcomes()
        if self.qualifying:
            self._write_attempt()
            for ident, (ok, diagnostic) in outcomes.items():
                line = " ".join(diagnostic.split())[:DIAGNOSTIC_LIMIT]
                print(f"ASSERT {ident} {'PASS' if ok else 'FAIL'} {line}", flush=True)
        return all(ok for ok, _ in outcomes.values())
