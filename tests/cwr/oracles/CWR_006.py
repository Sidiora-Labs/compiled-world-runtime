# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Independent oracle of case CWR-006, ledger, snapshot, world clock and RNG (clauses 1.5, 6.6, 8.1,
8.2 and 9.4).

tests/cwr/check_evidence.py loads this module after its separation audit and calls recompute(ctx).
It reads only its context: the attempt's verified Instance ledgers and final states and the
attempt-level final states the task 2.1 driver writes (cwr006-scenarios.json with the primitive
results each scenario printed, the snapshot file, the abandoned ledger lineage, the strace of one turn
with the ledger after it, the torn ledger and the bytes restore retained). The driver's ASSERT lines
and verdicts are never read. Each assertion is decided again:

  a1  The restored Instance's live ledger is the snapshot's ledger prefix plus one recovery record
      resuming from the snapshot's seq at its world_time; the snapshot names the seq, world_time,
      chain, K, Build and seed of that prefix; the turns run after restore record the same
      primitives, outcomes and world times and return the same values (draws included) as the turns
      run from the snapshot before restore; the state after restore is the snapshot's state.
  a2  In the strace of one turn every ledger write is followed by an fsync of the ledger before the
      next result line reaches stdout, and the ledger writes are exactly the turn's records.
  a3  No primitive result or turn result of two Instances created and run at different host times
      carries a number within ten days of the host clock (seconds to nanoseconds, the host clock taken
      from the ledgers' own host_ts), a timestamp, or a hidden ledger field, and both return equal
      values.
  a4  Two Instances with equal seed have ledgers equal except host_ts, duration_us and Instance id and
      draw equal values, and the draws equal a PCG32 stream recomputed here from the header seed; a
      different seed draws its own PCG32 stream.
  a5  A ledger whose final record is torn is refused, naming the truncation, by every command that
      reads it, its bytes stay untouched, and only the explicit restore appends a recovery record and
      retains the torn bytes.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime

SCENARIOS = "cwr006-scenarios.json"
M64 = (1 << 64) - 1
PCG_STREAM = 0xDA3E39CB94B95BDB
HOST = ("host_ts", "duration_us", "instance")
HIDDEN = {"host_ts", "duration_us", "instance", "arm"}
TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}")
DAY = 86400.0
REFUSERS = ("probe", "snapshot", "tail", "ledger", "turn", "create --from-snapshot", "replay")


class Pcg32:
    """pcg32_srandom_r / pcg32_random_r (O'Neill), 64-bit state, 32-bit output."""

    def __init__(self, initstate: int, initseq: int) -> None:
        self.state = 0
        self.inc = ((initseq << 1) | 1) & M64
        self.next()
        self.state = (self.state + initstate) & M64
        self.next()

    def next(self) -> int:
        old = self.state
        self.state = (old * 6364136223846793005 + self.inc) & M64
        shifted = (((old >> 18) ^ old) >> 27) & 0xFFFFFFFF
        rot = old >> 59
        return ((shifted >> rot) | (shifted << ((-rot) & 31))) & 0xFFFFFFFF


def pcg(seed, n):
    generator = Pcg32(seed, PCG_STREAM)
    return [generator.next() for _ in range(n)]


def _file(ctx, name):
    found = [data for path, data in ctx.final_states.items() if path.endswith("/" + name)]
    return found[0] if len(found) == 1 else None


def _json(data):
    try:
        return json.loads(data.decode("utf-8"))
    except (AttributeError, UnicodeDecodeError, ValueError):
        return None


def _lines(data):
    out = []
    for line in (data or b"").decode("utf-8", errors="replace").splitlines():
        try:
            value = json.loads(line)
        except ValueError:
            return None
        out.append(value)
    return out


def _strip(record, extra=()):
    return {k: v for k, v in record.items() if k not in HOST and k not in extra}


def _state(ctx, iid):
    for inst in ctx.instances:
        if inst.get("id") == iid and inst.get("final_state"):
            return _json(ctx.final_states.get(inst["final_state"]["path"]))
    return None


def _draws(calls):
    return [
        c.get("returned")
        for c in calls or []
        if isinstance(c, dict) and c.get("primitive") == "world.random" and c.get("status") == "ok"
    ]


def _epoch(text):
    try:
        return datetime.fromisoformat(str(text)).timestamp()
    except ValueError:
        return None


def check_snapshot(ctx, doc):
    entry = doc.get("snapshot")
    if not isinstance(entry, dict):
        return ["the snapshot scenario is missing"]
    ledger = ctx.ledgers.get(entry.get("instance"))
    snap = _json(_file(ctx, "cwr006-snapshot.json"))
    abandoned = _lines(_file(ctx, "cwr006-abandoned.jsonl"))
    if ledger is None or not isinstance(snap, dict) or not abandoned:
        return ["the snapshot Instance, snapshot file or abandoned lineage is missing"]
    problems = []
    seq = snap.get("seq")
    if type(seq) is not int or seq >= len(ledger):
        return [f"snapshot seq {seq!r} is not a record of the live ledger"]
    header, at = ledger[0], ledger[seq]
    for key, want in (
        ("world_time", at.get("world_time")),
        ("chain", at.get("chain")),
        ("tail_size", header.get("tail_size")),
        ("build", header.get("build")),
        ("seed", header.get("seed")),
    ):
        if snap.get(key) != want:
            problems.append(f"snapshot {key} {snap.get(key)!r} != ledger {want!r}")
    recovery = [r for r in ledger if r.get("kind") == "recovery" and r.get("resumed_from") == seq]
    if not recovery:
        problems.append(f"no recovery record resumes from seq {seq}")
    else:
        first = recovery[0]
        if first.get("seq") != seq + 1 or first.get("world_time") != snap.get("world_time"):
            problems.append(
                f"the recovery record is at seq {first.get('seq')} world_time "
                f"{first.get('world_time')}, not {seq + 1} / {snap.get('world_time')}"
            )
        if first.get("snapshot") != entry.get("snapshot"):
            problems.append("the recovery record names another snapshot")
    if [_strip(r) for r in ledger[: seq + 1]] != [_strip(r) for r in abandoned[: seq + 1]]:
        problems.append("the live ledger is not the snapshot's prefix of the abandoned lineage")
    before = entry.get("branch_before_restore") or {}
    after = entry.get("branch_after_restore") or {}
    calls_before = [c.get("returned") for c in before.get("calls") or []]
    calls_after = [c.get("returned") for c in after.get("calls") or []]
    if not calls_before or calls_before != calls_after:
        problems.append("the turns after restore return other values than before restore")
    if before.get("worlds") != after.get("worlds"):
        problems.append("the world coordinates after restore differ from before restore")
    count = len(calls_before)
    shape = ("seq", "chain")
    old = [_strip(r, shape) for r in abandoned[seq + 1 :] if r.get("kind") == "primitive"][:count]
    start = recovery[0].get("seq", 0) + 1 if recovery else len(ledger)
    new = [_strip(r, shape) for r in ledger[start:] if r.get("kind") == "primitive"][:count]
    if len(old) != count or old != new:
        problems.append("the primitive records after restore differ from those before restore")
    if entry.get("state_after_restore") != snap.get("state"):
        problems.append("the state after restore is not the snapshot's state")
    return problems


def check_durability(ctx, doc):
    entry = doc.get("durability")
    trace = _file(ctx, "cwr006-strace.txt")
    ledger = _lines(_file(ctx, "cwr006-durability-ledger.jsonl"))
    if not isinstance(entry, dict) or trace is None or not ledger:
        return ["the strace scenario, its trace or its ledger is missing"]
    iid = entry.get("instance")
    results = [x for x in entry.get("stdout") or [] if isinstance(x, dict)]
    turn = (results[-1].get("world") or {}).get("turn") if results else None
    records = [
        r for r in ledger if r.get("turn") == turn and r.get("kind") in ("primitive", "turn")
    ]
    events = []
    pattern = re.compile(r"^\d+\s+(write|fsync|fdatasync)\((\d+)<([^>]*)>")
    for line in trace.decode("utf-8", errors="replace").splitlines():
        match = pattern.match(line)
        if not match:
            continue
        call, fd, path = match.group(1), int(match.group(2)), match.group(3)
        if path.endswith(f"/instances/{iid}/ledger.jsonl"):
            events.append("L" if call == "write" else "S")
        elif fd == 1 and call == "write":
            events.append("O")
    problems = []
    pending = synced = outs = 0
    for event in events:
        if event == "L":
            pending += 1
        elif event == "S":
            synced, pending = synced + pending, 0
        else:
            outs += 1
            if pending or not synced:
                problems.append(
                    f"result line {outs} reached stdout before its ledger record was synced"
                )
            synced = 0
    if entry.get("returncode") != 0:
        problems.append(f"the traced turn exited {entry.get('returncode')}")
    if not records or events.count("L") != len(records):
        problems.append(
            f"{events.count('L')} ledger writes for {len(records)} records of turn {turn}"
        )
    if outs != len(results) or not outs:
        problems.append(f"{outs} stdout writes for {len(results)} result lines")
    return problems


def _leaks(value, windows, path, found):
    if isinstance(value, bool):
        return
    if isinstance(value, (int, float)):
        for low, high in windows:
            if low <= value <= high:
                found.append(f"{path}={value} is near host time")
    elif isinstance(value, str):
        if TIMESTAMP.search(value):
            found.append(f"{path} holds a timestamp")
    elif isinstance(value, dict):
        for key, item in value.items():
            if key in HIDDEN:
                found.append(f"{path}.{key} is a hidden ledger field")
            _leaks(item, windows, f"{path}.{key}", found)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _leaks(item, windows, f"{path}[{index}]", found)


def check_host_time(ctx, doc):
    entry = doc.get("host_time")
    if not isinstance(entry, dict) or len(entry.get("instances") or []) != 2:
        return ["the host-time scenario is missing"]
    problems, seen, starts = [], [], []
    for iid in entry["instances"]:
        ledger = ctx.ledgers.get(iid)
        probe = (entry.get("probes") or {}).get(iid)
        if ledger is None or not isinstance(probe, dict):
            problems.append(f"Instance {iid}: no verified ledger or no results")
            continue
        times = [t for t in (_epoch(r.get("host_ts")) for r in ledger) if t is not None]
        if not times:
            problems.append(f"Instance {iid}: the ledger carries no host_ts to compare with")
            continue
        starts.append(min(times))
        windows = [
            ((min(times) - 10 * DAY) * scale, (max(times) + 10 * DAY) * scale)
            for scale in (1, 1e3, 1e6, 1e9)
        ]
        calls = [c for c in probe.get("calls") or [] if isinstance(c, dict)]
        values = [c.get("returned") for c in calls if c.get("primitive") != "world.random"]
        result = {k: v for k, v in (probe.get("result") or {}).items() if k != "returned"}
        found = []
        _leaks([values, result], windows, iid, found)
        problems += found
        seen.append([c.get("returned") for c in calls])
    if len(seen) == 2 and seen[0] != seen[1]:
        problems.append("the two Instances return different values")
    if len(starts) == 2 and abs(starts[0] - starts[1]) < 1.0:
        problems.append("the two Instances did not run at different host times")
    return problems


def check_rng(ctx, doc):
    entry = doc.get("rng")
    if not isinstance(entry, dict) or len(entry.get("pair") or []) != 2:
        return ["the RNG scenario is missing"]
    a, b = entry["pair"]
    la, lb = ctx.ledgers.get(a), ctx.ledgers.get(b)
    if la is None or lb is None:
        return ["the equal-seed pair has no verified ledgers"]
    problems = []
    if la[0].get("seed") != lb[0].get("seed"):
        problems.append("the pair does not share one seed")
    if [_strip(r) for r in la] != [_strip(r) for r in lb]:
        problems.append("the equal-seed ledgers differ beyond host_ts, duration_us and Instance id")
    da, db = _draws(entry["calls"].get(a)), _draws(entry["calls"].get(b))
    want = pcg(la[0].get("seed"), len(da))
    if not da or da != db or da != want:
        problems.append(
            f"draws {da[:3]} / {db[:3]} are not the PCG32 stream {want[:3]} of the seed"
        )
    recorded = sum(1 for r in la if r.get("primitive") == "world.random")
    if recorded != len(da):
        problems.append(f"{len(da)} draws but {recorded} world.random records")
    other = entry.get("other_seed") or {}
    lo = ctx.ledgers.get(other.get("instance"))
    do = _draws(other.get("calls"))
    if lo is None or not do:
        problems.append("the different-seed Instance is missing")
    elif do != pcg(lo[0].get("seed"), len(do)) or do == da[: len(do)]:
        problems.append("a different seed does not draw its own PCG32 stream")
    return problems


def check_truncation(ctx, doc):
    entry = doc.get("truncation")
    torn = _file(ctx, "cwr006-torn.jsonl")
    retained = _file(ctx, "cwr006-retained.bin")
    if not isinstance(entry, dict) or torn is None or retained is None:
        return ["the truncation scenario, torn ledger or retained bytes are missing"]
    ledger = ctx.ledgers.get(entry.get("instance"))
    if ledger is None:
        return ["the truncated Instance has no verified ledger"]
    problems = []
    cut = torn.rfind(b"\n") + 1
    good, tail = _lines(torn[:cut]), torn[cut:]
    if not tail or torn.endswith(b"\n") or good is None:
        problems.append("the torn ledger does not end in a partial record")
        return problems
    refusals = entry.get("refusals") or {}
    for command in REFUSERS:
        result = refusals.get(command)
        if not (isinstance(result, list) and len(result) == 2):
            problems.append(f"{command}: no recorded result")
        elif result[0] == 0 or "truncated" not in str(result[1]):
            problems.append(f"{command} was not refused naming the truncation (rc={result[0]})")
    if entry.get("after_refusals_sha256") != "sha256:" + hashlib.sha256(torn).hexdigest():
        problems.append("the ledger bytes changed while the commands refused it")
    recovery = [i for i, r in enumerate(ledger) if r.get("kind") == "recovery"]
    restore = entry.get("restore") or {}
    if restore.get("returncode") != 0 or not recovery:
        problems.append("the explicit restore appended no recovery record")
    else:
        at = recovery[-1]
        if [_strip(r) for r in ledger[:at]] != [_strip(r) for r in good[:at]]:
            problems.append(
                "the restored ledger is not a prefix of the torn ledger's complete records"
            )
    if not retained.endswith(tail):
        problems.append("restore did not retain the torn bytes")
    return problems


CHECKS = {
    "a1": (
        check_snapshot,
        "restore equals the snapshot in ledger seq, world_time, chain, K, RNG and state",
    ),
    "a2": (check_durability, "every ledger record fsynced before its result reached stdout"),
    "a3": (check_host_time, "no primitive or turn result carries host time"),
    "a4": (check_rng, "equal seeds draw equal PCG32 streams with equal ledgers"),
    "a5": (check_truncation, "a torn final record is refused until the explicit restore"),
}


def recompute(ctx):
    """assertion id -> (PASS or FAIL, diagnostic) for every expected CWR-006 assertion."""
    doc = _json(_file(ctx, SCENARIOS))
    out = {}
    for ident in ctx.expected:
        key = ident.rpartition(".")[2]
        if key not in CHECKS:
            continue
        if not isinstance(doc, dict) or doc.get("format") != "cwr.cwr006-scenarios":
            out[ident] = ("FAIL", f"the attempt carries no verified {SCENARIOS}")
            continue
        check, held = CHECKS[key]
        problems = check(ctx, doc)
        out[ident] = ("FAIL", "; ".join(problems)) if problems else ("PASS", held)
    return out
