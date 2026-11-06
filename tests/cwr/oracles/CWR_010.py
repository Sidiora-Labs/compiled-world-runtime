# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Independent oracle of case CWR-010, crash recovery and ledger replay (clauses 6.6, 8.3 to 8.5).

tests/cwr/check_evidence.py loads this module after its separation audit and calls recompute(ctx).
The oracle reads only its context: the attempt's verified ledgers, the verified final state of every
Instance and the attempt-level final state cwr010-scenarios.json that the task 2.4 driver writes. That
file names the Instance of each crash scenario and carries the publish stub server's own request log
(GET /_stub/log, the external service's record of what reached it) at the crash and at the end, and
the original and replayed Instance of the 40-turn replay. The driver's ASSERT lines and verdicts are
never read. Every assertion is decided again from the ledger records:

  a1  For every crash boundary the stub saw exactly one request for the Instance, already at the
      crash unless the kernel was killed before the request executed, and as many as the ledger
      records external executions (`via: service`) of publish.post.
  a2  The ledger holds a recovery record for the interrupted turn and closes that turn with code
      UNAVAILABLE, status error when no record of the turn was durable before the crash and partial
      when one was.
  a3  No external request executed before the crash is sent again: every later publish.post record
      with an earlier request hash is answered from the gateway store, and the stub saw no request
      after the crash except the first execution of a request the crash prevented.
  a4  The replayed Instance's ledger equals the original 40-turn ledger record for record except
      host_ts, duration_us and the Instance id, both end in the same final state, and the replay
      reached no stub server.
"""

from __future__ import annotations

import json

SCENARIOS = "cwr010-scenarios.json"
BOUNDARIES = (
    "before_execute",
    "after_execute",
    "in_flight",
    "after_append_kill",
    "after_append_meta",
)
NOT_EXECUTED_AT_CRASH = ("before_execute",)
IGNORED = ("host_ts", "duration_us", "instance")
ADAPTER = "publish.post"


def _document(ctx):
    found = [data for path, data in ctx.final_states.items() if path.endswith("/" + SCENARIOS)]
    if len(found) != 1:
        return None, f"the attempt carries {len(found)} verified {SCENARIOS} final states, not one"
    try:
        doc = json.loads(found[0].decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        return None, f"{SCENARIOS} is not JSON: {error}"
    if not isinstance(doc, dict) or doc.get("format") != "cwr.cwr010-scenarios":
        return None, f"{SCENARIOS} is not a cwr.cwr010-scenarios document"
    if not isinstance(doc.get("boundaries"), dict):
        return None, f"{SCENARIOS} names no crash scenarios"
    return doc, None


def _final_state(ctx, iid):
    for inst in ctx.instances:
        if inst.get("id") == iid and inst.get("final_state"):
            return ctx.final_states.get(inst["final_state"]["path"])
    return None


def _requests(log, iid):
    if not isinstance(log, list):
        return None
    return [e for e in log if isinstance(e, dict) and e.get("instance") == iid]


def _gateway(record):
    gateway = record.get("gateway")
    return gateway if isinstance(gateway, dict) else {}


def _boundary(ctx, label, entry):
    """Problems per assertion (a1, a2, a3) for one crash scenario."""
    out = {"a1": [], "a2": [], "a3": []}
    where = f"[{label}]"
    if not isinstance(entry, dict):
        for key in out:
            out[key].append(f"{where} scenario missing")
        return out
    iid, n = entry.get("instance"), entry.get("interrupted_turn")
    ledger = ctx.ledgers.get(iid)
    if ledger is None or type(n) is not int:
        for key in out:
            out[key].append(f"{where} Instance {iid} has no verified ledger in the attempt")
        return out
    at_crash, final = (
        _requests(entry.get("stub_at_crash"), iid),
        _requests(entry.get("stub_final"), iid),
    )
    if at_crash is None or final is None:
        out["a1"].append(f"{where} no stub request log")
        out["a3"].append(f"{where} no stub request log")
        return out
    calls = [r for r in ledger if r.get("kind") == "adapter" and r.get("primitive") == ADAPTER]
    executions = [r for r in calls if _gateway(r).get("via") == "service"]
    want_at_crash = 0 if label in NOT_EXECUTED_AT_CRASH else 1

    # a1: exactly one request reached the service, when the boundary says, as the ledger records
    if len(final) != 1:
        out["a1"].append(f"{where} the stub saw {len(final)} requests, not exactly one")
    if len(at_crash) != want_at_crash:
        out["a1"].append(
            f"{where} the stub saw {len(at_crash)} requests at the crash, not {want_at_crash}"
        )
    if len(executions) != len(final):
        out["a1"].append(
            f"{where} the ledger records {len(executions)} executions of {ADAPTER}, the stub saw {len(final)}"
        )

    # a2: a recovery record and the interrupted turn closed with UNAVAILABLE
    recovery = [r for r in ledger if r.get("kind") == "recovery" and r.get("interrupted_turn") == n]
    closing = [r for r in ledger if r.get("kind") == "turn" and r.get("turn") == n]
    durable = [
        r
        for r in ledger
        if r.get("turn") == n
        and r.get("kind") in ("primitive", "adapter")
        and not r.get("reconstructed")
        and (r.get("outcome") or {}).get("status") == "ok"
    ]
    want_status = "partial" if durable else "error"
    if len(recovery) != 1:
        out["a2"].append(f"{where} {len(recovery)} recovery records name interrupted turn {n}")
    if len(closing) != 1:
        out["a2"].append(f"{where} turn {n} is closed by {len(closing)} turn records")
    else:
        close = closing[0]
        if close.get("code") != "UNAVAILABLE" or close.get("status") != want_status:
            out["a2"].append(
                f"{where} turn {n} closed {close.get('status')}/{close.get('code')}, "
                f"not {want_status}/UNAVAILABLE"
            )
        if recovery and recovery[0].get("seq", -1) > close.get("seq", -1):
            out["a2"].append(f"{where} the recovery record follows the turn's closing record")

    # a3: nothing executed before the crash executes again
    before = {_gateway(r).get("request_hash") for r in calls if r.get("turn") == n}
    again = [
        r.get("seq")
        for r in calls
        if r.get("turn", 0) > n
        and _gateway(r).get("request_hash") in before
        and _gateway(r).get("via") == "service"
    ]
    if again:
        out["a3"].append(f"{where} records {again} re-execute a request sent before the crash")
    if label not in NOT_EXECUTED_AT_CRASH and len(final) != len(at_crash):
        out["a3"].append(
            f"{where} the stub saw {len(final) - len(at_crash)} more requests after the crash"
        )
    retried = [r for r in calls if r.get("turn", 0) > n]
    if not retried:
        out["a3"].append(
            f"{where} no later turn calls {ADAPTER} again, so nothing shows it is not re-sent"
        )
    return out


def _replay(ctx, entry):
    if not isinstance(entry, dict):
        return ["the replay scenario is missing"]
    original, replayed = entry.get("original"), entry.get("replayed")
    a, b = ctx.ledgers.get(original), ctx.ledgers.get(replayed)
    if a is None or b is None:
        return [f"the replay pair {original}/{replayed} has no verified ledgers in the attempt"]
    problems = []
    turns = sum(1 for r in a if r.get("kind") == "turn")
    if turns != entry.get("turns") or turns < 40:
        problems.append(
            f"the original ledger holds {turns} turns, not {entry.get('turns')} (>= 40)"
        )
    if len(a) != len(b):
        problems.append(f"the replay holds {len(b)} records, the original {len(a)}")
    differ = [
        x.get("seq")
        for x, y in zip(a, b, strict=False)
        if {k: v for k, v in x.items() if k not in IGNORED}
        != {k: v for k, v in y.items() if k not in IGNORED}
    ]
    if differ:
        problems.append(f"records {differ[:10]} differ beyond host_ts, duration_us and Instance id")
    state_a, state_b = _final_state(ctx, original), _final_state(ctx, replayed)
    if state_a is None or state_b is None:
        problems.append("the replay pair lacks a verified final state")
    elif state_a != state_b:
        problems.append("the replayed Instance ends in a different final state")
    if entry.get("stub_requests_of_replayed") != [] or entry.get(
        "stub_requests_before"
    ) != entry.get("stub_requests_after"):
        problems.append("the stub servers saw requests during the replay")
    return problems


def recompute(ctx):
    """assertion id -> (PASS or FAIL, diagnostic) for every expected CWR-010 assertion."""
    doc, problem = _document(ctx)
    found = {"a1": [], "a2": [], "a3": [], "a4": []}
    if problem:
        for key in found:
            found[key].append(problem)
    else:
        for label in BOUNDARIES:
            for key, items in _boundary(ctx, label, doc["boundaries"].get(label)).items():
                found[key] += items
        found["a4"] += _replay(ctx, doc.get("replay"))
    held = {
        "a1": f"{len(BOUNDARIES)} crash boundaries: one stub request each, matching the ledger",
        "a2": f"{len(BOUNDARIES)} interrupted turns recovered and closed UNAVAILABLE",
        "a3": f"{len(BOUNDARIES)} crash boundaries: no request re-sent after recovery",
        "a4": "replayed ledger identical except host_ts, duration_us and Instance id; equal final state",
    }
    out = {}
    for ident in ctx.expected:
        key = ident.rpartition(".")[2]
        if key not in found:
            continue
        out[ident] = ("FAIL", "; ".join(found[key])) if found[key] else ("PASS", held[key])
    return out
