# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Load and validate benchmark/harness/arms.kvx (requirement 12.1).

Validation is structural: every arm must be a CWR Build of the registered world, built either as the
control Build or from the control source with exactly one registered experimental flag, and an arm with
a flag must name the single pillar it ablates. Anything else is refused.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .ledgerio import read_kvx

FLAGS = {
    "--x-drift": "stability",
    "--x-one-primitive": "direct_action",
    "--x-version-only": "controlled_change",
    "--x-silent-change": "controlled_change",
}
BINDINGS = {"control", "per_task", "flag", "pair_b"}
ROLES = {"control", "ablation", "variant"}
HYPOTHESES = ("H1", "H2", "H3", "H4")
DEFAULT_PATH = Path(__file__).resolve().parent / "arms.kvx"


class InvalidArms(ValueError):  # noqa: N818 - public name used by callers
    pass


@dataclass(frozen=True)
class Arm:
    key: str  # directory name under benchmark/results/h<N>/, e.g. drift-1
    hypothesis: str  # H1..H4
    label: str
    role: str
    flag: str | None
    pillar_ablated: str | None
    experimental_stamp: str | None
    build_binding: str
    build: str | None
    tail: int | str
    task_set: str
    raw: dict

    @property
    def experimental(self) -> bool:
        return self.flag is not None

    @property
    def results_dir(self) -> str:
        return f"{self.hypothesis.lower()}/{self.key}"


def load_arms(path: Path | str = DEFAULT_PATH) -> tuple[dict[str, Any], dict[tuple[str, str], Arm]]:
    data = read_kvx(path)
    meta = data.get("meta", {})
    if meta.get("format") != "cwr.arms" or meta.get("no_external_baseline") is not True:
        raise InvalidArms(
            f"{path}: [meta] must declare format cwr.arms and no_external_baseline = true"
        )
    world = meta.get("world")
    control = data.get("control")
    if not control or control.get("world") != world:
        raise InvalidArms(f"{path}: [control] must be a Build of world {world!r}")
    arms: dict[tuple[str, str], Arm] = {}
    for section, body in data.items():
        if not section.startswith("arm."):
            if section not in ("meta", "control"):
                raise InvalidArms(f"{path}: unexpected section [{section}]")
            continue
        parts = section.split(".", 2)
        if len(parts) != 3:
            raise InvalidArms(f"{path}: arm section [{section}] must be arm.h<N>.<key>")
        _, hyp_dir, key = parts
        hypothesis = body.get("hypothesis")
        if hypothesis not in HYPOTHESES or hyp_dir != hypothesis.lower():
            raise InvalidArms(f"[{section}]: hypothesis {hypothesis!r} does not match {hyp_dir}")
        flag = body.get("flag")
        pillar = body.get("pillar_ablated")
        if flag is not None and flag not in FLAGS:
            raise InvalidArms(f"[{section}]: flag {flag!r} is not a CWR experimental flag")
        if (flag is None) != (pillar is None) or (flag is not None and FLAGS[flag] != pillar):
            raise InvalidArms(
                f"[{section}]: a flagged arm must ablate exactly the flag's pillar and an unflagged arm none"
            )
        stamp = body.get("experimental_stamp")
        if (flag is None) != (stamp is None) or (flag is not None and stamp != flag.lstrip("-")):
            raise InvalidArms(
                f"[{section}]: experimental_stamp must be the flag name without dashes"
            )
        role = body.get("role")
        if role not in ROLES or (role == "ablation" and flag is None and hypothesis != "H3"):
            raise InvalidArms(f"[{section}]: invalid role {role!r}")
        binding = body.get("build_binding")
        if binding not in BINDINGS:
            raise InvalidArms(f"[{section}]: build_binding {binding!r} unknown")
        tail = body.get("tail")
        if not (tail == "full" or (isinstance(tail, int) and tail > 0)):
            raise InvalidArms(f'[{section}]: tail must be a positive integer or "full"')
        build = body.get("build")
        if build is not None and not (
            isinstance(build, str) and build.startswith("sha256:") and len(build) == 71
        ):
            raise InvalidArms(f"[{section}]: build must be sha256:<64hex> or null")
        arms[(hypothesis, key)] = Arm(
            key=key,
            hypothesis=hypothesis,
            label=body.get("label", key),
            role=role,
            flag=flag,
            pillar_ablated=pillar,
            experimental_stamp=stamp,
            build_binding=binding,
            build=build,
            tail=tail,
            task_set=body.get("task_set", ""),
            raw=dict(body),
        )
    for hypothesis in HYPOTHESES:
        if sum(1 for (h, _), a in arms.items() if h == hypothesis and a.role == "control") != 1:
            raise InvalidArms(f"{path}: {hypothesis} must have exactly one control arm")
    return {"meta": meta, "control": control}, arms


def resolve_build(arm: Arm, control: dict[str, Any]) -> str | None:
    """The single registered Build hash for an arm, or None when it is bound per task or per pair."""
    if arm.build_binding == "control":
        return control.get("build")
    if arm.build_binding == "flag":
        return arm.build
    return None
