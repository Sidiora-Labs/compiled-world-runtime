# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Load and check benchmark/preregistration.kvx (requirement 12.5, experiments.md section 5).

The registration freezes, before any campaign, the arms, task sets, metric definitions (by the sha256 of
benchmark/harness/metrics.py), analysis parameters and expected directions. `check_digests` is the gate
analyse.py and run.py apply: a metrics.py or arms.kvx whose digest differs from the registered one is
refused. `missing_fields` lists campaign inputs still null (model identity, Build hashes, digests);
a registration with missing fields is a draft and cannot be used for a campaign.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .ledgerio import REPO, read_kvx, sha256_file

DEFAULT_PATH = REPO / "benchmark" / "preregistration.kvx"
METRICS_PATH = REPO / "benchmark" / "harness" / "metrics.py"
ARMS_PATH = REPO / "benchmark" / "harness" / "arms.kvx"
EXPECTS = {"a>b", "a<b", "a>=b", "a<=b", "a=b", "long>short", "undetermined"}
REQUIRED_CAMPAIGN = ("model", "sampling", "suite_digest", "cassette_digest", "upgrades_digest")


class InvalidPreregistration(ValueError):  # noqa: N818 - public name used by callers
    pass


class DigestMismatch(InvalidPreregistration):
    pass


def load_prereg(path: Path | str = DEFAULT_PATH) -> dict[str, dict[str, Any]]:
    data = read_kvx(path)
    meta = data.get("meta", {})
    if meta.get("format") != "cwr.preregistration" or meta.get("schema_version") != 1:
        raise InvalidPreregistration(
            f"{path}: [meta] must declare format cwr.preregistration, schema_version 1"
        )
    for section in ("digests", "campaign", "analysis"):
        if section not in data:
            raise InvalidPreregistration(f"{path}: missing [{section}]")
    analysis = data["analysis"]
    if not (isinstance(analysis.get("resamples"), int) and analysis["resamples"] > 0):
        raise InvalidPreregistration("[analysis].resamples must be a positive integer")
    if not (0 < analysis.get("confidence", 0) < 1):
        raise InvalidPreregistration("[analysis].confidence must be in (0, 1)")
    if not isinstance(analysis.get("seed"), int):
        raise InvalidPreregistration("[analysis].seed must be an integer")
    hypotheses = {k: v for k, v in data.items() if k.startswith("hypothesis.")}
    if sorted(hypotheses) != ["hypothesis.H1", "hypothesis.H2", "hypothesis.H3", "hypothesis.H4"]:
        raise InvalidPreregistration(
            f"{path}: hypotheses H1 to H4 must each be registered exactly once"
        )
    metrics = {k.split(".", 1)[1] for k in data if k.startswith("metric.")}
    for name, body in hypotheses.items():
        for field in (
            "claim",
            "arms",
            "control",
            "task_set",
            "primary",
            "secondary",
            "directions",
            "grouping",
        ):
            if field not in body:
                raise InvalidPreregistration(f"[{name}] missing {field}")
        if body["control"] not in body["arms"]:
            raise InvalidPreregistration(f"[{name}] control arm is not among its arms")
        for metric in [body["primary"], *body["secondary"]]:
            if metric.split(".", 1)[0] not in metrics and metric not in metrics:
                raise InvalidPreregistration(f"[{name}] metric {metric} is not registered")
        for direction in body["directions"]:
            if direction.get("expect") not in EXPECTS:
                raise InvalidPreregistration(
                    f"[{name}] direction {direction.get('id')} has unknown expect"
                )
    return data


def check_digests(
    prereg: dict[str, dict[str, Any]],
    *,
    metrics_path: Path | str = METRICS_PATH,
    arms_path: Path | str = ARMS_PATH,
) -> dict[str, str]:
    """Raise DigestMismatch unless metrics.py and arms.kvx match the registered sha256 digests."""
    registered = prereg["digests"]
    current = {"metrics_py": sha256_file(metrics_path), "arms_kvx": sha256_file(arms_path)}
    for key, value in current.items():
        if registered.get(key) != value:
            raise DigestMismatch(
                f"{key} digest {value} differs from the registered {registered.get(key)}; "
                "a changed definition is a new pre-registration and a new campaign"
            )
    return current


def missing_fields(
    prereg: dict[str, dict[str, Any]], arms: dict, control: dict[str, Any]
) -> list[str]:
    """Campaign inputs that are still null. Empty means the registration is complete."""
    missing = [
        f"campaign.{f}" for f in REQUIRED_CAMPAIGN if prereg["campaign"].get(f) in (None, "")
    ]
    if not control.get("build"):
        missing.append("arms.control.build")
    for (hypothesis, key), arm in sorted(arms.items()):
        if arm.build_binding == "flag" and not arm.build:
            missing.append(f"arms.{hypothesis.lower()}.{key}.build")
    if prereg["meta"].get("status") != "registered":
        missing.append('meta.status (must be "registered")')
    return missing
