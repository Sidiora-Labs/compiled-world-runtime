# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Synthetic ledgers, fake kernel and fake model for harness unit tests.

Records follow the ledger schema of spec/cwr/kernel.md section 8. Nothing here is product evidence.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any

BUILD = "sha256:" + "a" * 64
BUILD_B = "sha256:" + "b" * 64
CASSETTE = "sha256:" + "c" * 64
MODEL = "test-model"


class LedgerBuilder:
    def __init__(
        self,
        *,
        arm: str = "H1/frozen",
        seed: int = 1,
        tail: Any = 20,
        model: str = MODEL,
        build: str = BUILD,
        stamp: str | None = None,
        cassette: str | None = CASSETTE,
        instance: str = "01J0000000000000000000TEST",
    ):
        self.build, self.stamp, self.instance = build, stamp, instance
        self.turn_no, self.time = 1, 0
        self.records: list[dict[str, Any]] = []
        head = self._base("header", turn=0)
        head.update(
            {"seed": seed, "tail_size": tail, "arm": arm, "model": model, "created_from": None}
        )
        if cassette is not None:
            head["cassette_digest"] = cassette
        self.records.append(head)

    def _base(self, kind: str, turn: int | None = None) -> dict[str, Any]:
        return {
            "seq": len(self.records),
            "instance": self.instance,
            "build": self.build,
            "turn": self.turn_no if turn is None else turn,
            "world_time": self.time,
            "kind": kind,
            "primitive": None,
            "params_hash": None,
            "preconditions": [],
            "outcome": None,
            "duration_us": 7,
            "host_ts": "2026-11-06T00:00:00Z",
            "experimental": self.stamp,
        }

    def call(
        self,
        primitive: str,
        code: str | None = None,
        side_effect: str = "read_only",
        kind: str = "primitive",
    ) -> LedgerBuilder:
        if kind == "primitive" and side_effect == "external":
            kind = "adapter"
        self.time += 1
        record = self._base(kind)
        record.update(
            {
                "primitive": primitive,
                "params_hash": "blake3:00",
                "outcome": {
                    "status": "error" if code else "ok",
                    "code": code,
                    "shape": "Any",
                    "side_effect": side_effect,
                },
            }
        )
        self.records.append(record)
        return self

    def calls(self, primitive: str, n: int, side_effect: str = "read_only") -> LedgerBuilder:
        for _ in range(n):
            self.call(primitive, side_effect=side_effect)
        return self

    def turn(self, status: str = "ok") -> LedgerBuilder:
        self.time += 10
        record = self._base("turn")
        record.update({"status": status, "returned_hash": None, "resources": {}, "calls": 0})
        self.records.append(record)
        self.turn_no += 1
        return self

    def drift(self) -> LedgerBuilder:
        self.records.append(self._base("drift"))
        return self

    def proposal(self) -> LedgerBuilder:
        self.records.append(self._base("proposal"))
        return self

    def upgrade(self, new_build: str = BUILD_B, stamp: str | None = None) -> LedgerBuilder:
        # As the kernel writes it: the upgrade record already carries the new Build and its stamp.
        old_build = self.build
        self.build, self.stamp = new_build, stamp
        record = self._base("upgrade", turn=self.turn_no - 1)
        record.update({"old_build": old_build, "new_build": new_build, "migrations": []})
        self.records.append(record)
        return self

    def build_ledger(self) -> list[dict[str, Any]]:
        return copy.deepcopy(self.records)


# ---------------------------------------------------------------------------------------------
# Fake kernel and model for runner tests. The "script" language is one instruction per line:
#   CALL <primitive> <ok|CODE> <side_effect>
#   SET <module> <key> <json-free token>


@dataclass
class FakeReply:
    text: str
    model: str
    input_tokens: int
    output_tokens: int


class FakeModel:
    def __init__(self, scripts: list[str], model: str = MODEL, fail_at: int | None = None):
        self.scripts, self.model, self.fail_at, self.calls = scripts, model, fail_at, 0
        self.contexts: list[str] = []

    def complete_context(
        self, context: str, *, max_tokens: int, params: dict | None = None
    ) -> FakeReply:
        self.calls += 1
        self.contexts.append(context)
        if self.fail_at is not None and self.calls == self.fail_at:
            raise RuntimeError("gateway unavailable")
        text = self.scripts[min(self.calls - 1, len(self.scripts) - 1)]
        return FakeReply(f"```python\n{text}\n```", self.model, 100, 10)


class FakeKernel:
    def __init__(self, gateway_mode: str = "replay"):
        self.gateway_mode = gateway_mode
        self.instances: dict[str, dict[str, Any]] = {}
        self.destroyed: list[str] = []

    def create_instance(
        self,
        *,
        build,
        seed,
        tail,
        arm,
        model,
        allow_experimental,
        initial_state,
        cassette_digest=None,
    ):
        stamp = (
            "x-drift"
            if allow_experimental and "drift" in arm
            else ("x-one-primitive" if allow_experimental and "one-primitive" in arm else None)
        )
        iid = f"inst-{arm}-{seed}".replace("/", "-")
        builder = LedgerBuilder(
            arm=arm,
            seed=seed,
            tail=tail,
            model=model,
            build=build,
            stamp=stamp,
            cassette=cassette_digest or CASSETTE,
        )
        self.instances[iid] = {
            "ledger": builder,
            "state": {"task": {"progress": 0}},
            "allow": allow_experimental,
        }
        return iid

    def bind_cassette(self, instance, cassette):
        self.instances[instance]["cassette"] = cassette

    def context(self, instance):
        b = self.instances[instance]["ledger"]
        return f"MANUAL\n--\ntail {len(b.records)}"

    def turn(self, instance, message):
        inst = self.instances[instance]
        body = message.split("```python\n", 1)[1].rsplit("\n```", 1)[0]
        status = "ok"
        mutated = False
        for line in body.splitlines():
            parts = line.split()
            if parts and parts[0] == "CALL":
                code = None if parts[2] == "ok" else parts[2]
                inst["ledger"].call(parts[1], code, parts[3])
                if code is None and parts[3] != "read_only":
                    mutated = True
                if code is not None:
                    status = "partial" if mutated else "error"
                    break
            elif parts and parts[0] == "SET":
                inst["state"].setdefault(parts[1], {})[parts[2]] = int(parts[3])
        inst["ledger"].turn(status)
        return {"status": status}

    def upgrade(self, instance, build, *, allow_experimental):
        self.instances[instance]["ledger"].upgrade(build, None)

    def ledger(self, instance):
        return self.instances[instance]["ledger"].build_ledger()

    def state(self, instance):
        return copy.deepcopy(self.instances[instance]["state"])

    def destroy(self, instance):
        self.destroyed.append(instance)

    def revisions(self):
        return {"compiler": "test", "kernel": "test", "sdk": "test"}


def progress_predicate(target: int):
    def success(state, ledger):
        return state.get("task", {}).get("progress", 0) >= target

    return success


PREDICATE_SOURCE = """
def success(state, ledger):
    return state.get("task", {}).get("progress", 0) >= 2
"""
