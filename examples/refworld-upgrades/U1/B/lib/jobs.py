# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Kernel-side implementation of the refworld jobs module.

Runs inside the kernel process against the kernel state API, never in the sandbox. A job submitted
at world_time t with duration n is queued while world_time < t + 1, running while world_time < t + n
and done afterwards. A job whose ``fail_at`` is set (fixtures and initial states only; jobs.submit
never sets it) is failed from that tick on when fail_at <= t + n; a cancelled job is failed from the
tick of cancellation. Status is a pure function of the stored record and world_time, so the model
can predict it from world.now().

State shape:
    {"next_id": int, "jobs": {id: {"kind": str, "n": int, "submitted": int,
                                   "fail_at": int | None, "cancelled_at": int | None}}}
Job ids are "j1", "j2", ... in submission order, so equal histories give equal ids.
"""

from typing import TYPE_CHECKING, Any, TypedDict

if TYPE_CHECKING:
    import cwr_state  # noqa: F401  kernel state API; permitted import


class JobRecord(TypedDict):
    kind: str
    n: int
    submitted: int
    fail_at: int | None
    cancelled_at: int | None


class JobsState(TypedDict):
    next_id: int
    jobs: dict[str, JobRecord]


INITIAL_STATE: JobsState = {"next_id": 1, "jobs": {}}


def _jobs(state: Any) -> JobsState:
    return state.module("jobs")


def _status_of(job: JobRecord, now: int) -> str:
    if job.get("cancelled_at") is not None and now >= job["cancelled_at"]:
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


# The parameter id shadows the builtin on purpose: parameter names come from contracts/jobs.kvx.
def known(state: Any, id: str) -> bool:
    return id in _jobs(state)["jobs"]


def status(state: Any, id: str) -> str:
    return _status_of(_jobs(state)["jobs"][id], state.world_time())


def active(state: Any) -> int:
    now = state.world_time()
    return sum(
        _status_of(job, now) in ("queued", "running") for job in _jobs(state)["jobs"].values()
    )


def submit(state: Any, kind: str, n: int) -> str:
    table = _jobs(state)
    job_id = f"j{table['next_id']}"
    table["next_id"] += 1
    table["jobs"][job_id] = {
        "kind": kind,
        "n": n,
        "submitted": state.world_time(),
        "fail_at": None,
        "cancelled_at": None,
    }
    return job_id


def result(state: Any, id: str) -> str:
    job = _jobs(state)["jobs"][id]
    return f"{job['kind']}:{id}:{job['n']}"


def cancel(state: Any, id: str) -> None:
    _jobs(state)["jobs"][id]["cancelled_at"] = state.world_time()
    return None
