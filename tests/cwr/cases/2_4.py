#!/usr/bin/env python3
# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Task 2.4 driver: crash recovery and ledger replay.

Case CWR-010. Exercises clauses 6.6, 8.3, 8.4 and 8.5 against the real implementation: it builds the
`cwr` binary, runs the `cwr-kernel` and `cwr-cli` unit tests, compiles a Build from a copy of
examples/refworld with the real `cwr build`, starts the refworld stub servers of benchmark/stubs and
drives `cwr instance create|turn|recover|replay|compare|state|ledger` as subprocesses.

Crashes are real: the kernel process is killed with SIGKILL in the middle of a turn, either by the
driver from outside (os.killpg while the turn's script is running or while the publish stub holds a
request) or by the kernel's own fault-injection seam (`CWR_FAULT_INJECT=<point>:<selector>`, see
crates/cwr-kernel/src/recovery.rs), which makes the process SIGKILL itself at an exact boundary:
before an external primitive executes, after it executed but before its ledger record, after the
record is durable but before the Instance metadata follows it. Recovery then runs through
`cwr instance recover` (and, in one scenario, implicitly on the next `cwr instance turn`).

Oracles read only what clause 8.5 allows: ledger files, the final state printed by
`cwr instance state`, and the stub servers' own request logs (`GET /_stub/log`, the external
service's record of what reached it). Kernel internals (instance.json, envelopes, the gateway store,
sandbox output) are never used to judge a clause; the recovery report is printed as evidence only.

Prints one line per clause, `CLAUSE <req.ac> PASS|FAIL <evidence>`, and exits 0 only when every clause
passes. Honours CARGO_TARGET_DIR (default: the workspace target dir). Python 3.12, standard library only.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import protocol

sys.dont_write_bytecode = True

REPO = Path(__file__).resolve().parents[3]
REFWORLD = REPO / "examples/refworld"
STUBS = REPO / "benchmark/stubs"
CLAUSES = ["6.6", "8.3", "8.4", "8.5"]
TIMEOUT = 900
FAULT_ENV = "CWR_FAULT_INJECT"
REPLAY_TURNS = 40


class Results:
    def __init__(self) -> None:
        self.ok: dict[str, list[str]] = {c: [] for c in CLAUSES}
        self.bad: dict[str, list[str]] = {c: [] for c in CLAUSES}

    def check(self, clause: str, condition, evidence: str) -> bool:
        (self.ok if condition else self.bad)[clause].append(evidence)
        return bool(condition)


R = Results()
OBS = protocol.Observer("2.4", protocol.ok_bad(R))


def run(cmd, env: dict | None = None, timeout: int = TIMEOUT, **kw) -> subprocess.CompletedProcess:
    full = None if env is None else {**os.environ, **env}
    return subprocess.run(
        [str(c) for c in cmd],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=full,
        start_new_session=True,
        **kw,
    )


def target_dir() -> Path:
    env = os.environ.get("CARGO_TARGET_DIR")
    return Path(env) if env else REPO / "target"


def build_binaries() -> tuple[Path, bool, str]:
    env = dict(os.environ)
    env.setdefault("CARGO_TARGET_DIR", str(target_dir()))
    b = subprocess.run(
        ["cargo", "build", "-p", "cwr-cli"],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=TIMEOUT,
    )
    if b.returncode != 0:
        print(b.stderr[-4000:], file=sys.stderr)
        raise SystemExit("cargo build failed")
    t = subprocess.run(
        ["cargo", "test", "-p", "cwr-kernel", "-p", "cwr-cli"],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=TIMEOUT,
    )
    out = t.stdout + t.stderr
    passed = sum(int(n) for n in re.findall(r"test result: ok\. (\d+) passed", out))
    mine = len(re.findall(r"test (?:recovery|replay)::\S+ \.\.\. ok", out))
    return (
        target_dir() / "debug" / "cwr",
        t.returncode == 0 and mine >= 4,
        f"cargo test -p cwr-kernel -p cwr-cli rc={t.returncode} passed={passed} (recovery/replay unit tests ok={mine})",
    )


def canonical(v) -> str:
    return json.dumps(v, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


# ------------------------------------------------------------------------------------------------
# Stub servers
# ------------------------------------------------------------------------------------------------


class Stub:
    def __init__(self, name: str, seed: int, scratch: Path) -> None:
        self.name = name
        self.proc = subprocess.Popen(
            [
                sys.executable,
                "-I",
                str(STUBS / f"{name}_server.py"),
                "--port",
                "0",
                "--seed",
                str(seed),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            cwd=scratch,
        )
        ready = json.loads(self.proc.stdout.readline())
        if ready.get("event") != "ready":
            raise SystemExit(f"stub {name} did not start: {ready}")
        self.url = f"http://127.0.0.1:{ready['port']}"

    def _req(self, method: str, path: str, body: dict | None = None) -> dict:
        data = None if body is None else canonical(body).encode()
        r = urllib.request.Request(
            self.url + path, data=data, method=method, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(r, timeout=10) as resp:
            return json.loads(resp.read())

    def log(self, instance: str | None = None) -> list[dict]:
        return [
            e
            for e in self._req("GET", "/_stub/log")["requests"]
            if instance is None or e["instance"] == instance
        ]

    def schedule(self, faults: list[dict]) -> None:
        self._req(
            "PUT",
            "/_stub/schedule",
            {
                "format": "cwr.stub.schedule",
                "schema_version": 1,
                "allow_undeclared": False,
                "faults": faults,
            },
        )

    def stop(self) -> None:
        self.proc.terminate()
        try:
            self.proc.wait(5)
        except subprocess.TimeoutExpired:
            self.proc.kill()


# ------------------------------------------------------------------------------------------------
# The world under test
# ------------------------------------------------------------------------------------------------


class World:
    def __init__(self, cwr: Path, scratch: Path, stubs: dict[str, Stub]) -> None:
        self.cwr = cwr
        self.scratch = scratch
        self.home = scratch / "home"
        self.stubs = stubs
        self.n = 0
        self.build: Path | None = None
        self.initial: Path | None = None

    def compile(self) -> Path:
        src = self.scratch / "src"
        shutil.copytree(REFWORLD, src, ignore=shutil.ignore_patterns("README.md", "__pycache__"))
        p = run([self.cwr, "build", src, "--json", "--out", self.scratch / "builds"])
        rep = json.loads(p.stdout) if p.stdout.strip().startswith("{") else {}
        b = rep.get("build") or {}
        if p.returncode != 0 or not b.get("dir"):
            raise SystemExit(
                f"cwr build failed ({p.returncode}): {rep.get('summary')} {p.stderr[-800:]}"
            )
        self.build = Path(b["dir"])
        return self.build

    def create(self, seed: int, initial: Path | None = None, home: Path | None = None) -> str:
        cmd = [
            self.cwr,
            "instance",
            "create",
            "--home",
            home or self.home,
            self.build,
            "--seed",
            str(seed),
        ]
        if initial is not None:
            cmd += ["--initial", initial]
        p = run(cmd)
        if p.returncode != 0:
            raise SystemExit(f"instance create failed: {p.stderr}")
        return p.stdout.strip()

    def turn_cmd(
        self,
        iid: str,
        script: str,
        mode: str = "live",
        cassette: Path | None = None,
        home: Path | None = None,
    ) -> list:
        self.n += 1
        f = self.scratch / "scripts" / f"turn-{self.n}.py"
        f.parent.mkdir(exist_ok=True)
        f.write_text(script)
        cmd = [
            self.cwr,
            "instance",
            "turn",
            iid,
            "--home",
            home or self.home,
            "--script",
            f,
            "--gateway",
            mode,
        ]
        for name, stub in self.stubs.items():
            cmd += ["--stub", f"{name}={stub.url}"]
        if cassette is not None:
            cmd += ["--cassette", cassette]
        return cmd

    def turn(
        self, iid: str, script: str, env: dict | None = None, **kw
    ) -> tuple[int, dict | None, str]:
        p = run(self.turn_cmd(iid, script, **kw), env=env)
        envl = None
        if p.returncode == 0:
            try:
                envl = json.loads(p.stdout.strip().splitlines()[-1])
            except (ValueError, IndexError):
                envl = None
        return p.returncode, envl, p.stderr

    def spawn(self, iid: str, script: str, **kw) -> subprocess.Popen:
        return subprocess.Popen(
            [str(c) for c in self.turn_cmd(iid, script, **kw)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )

    def ledger_path(self, iid: str, home: Path | None = None) -> Path:
        return (home or self.home) / "instances" / iid / "ledger.jsonl"

    def ledger(self, iid: str, home: Path | None = None) -> list[dict]:
        raw = self.ledger_path(iid, home).read_bytes()
        complete = raw[: raw.rfind(b"\n") + 1] if b"\n" in raw else b""
        return [json.loads(line) for line in complete.decode().splitlines() if line.strip()]

    def state(self, iid: str, home: Path | None = None) -> tuple[dict | None, str]:
        p = run([self.cwr, "instance", "state", iid, "--home", home or self.home])
        if p.returncode != 0:
            return None, f"state rc={p.returncode} {p.stderr.strip()[-300:]}"
        text = p.stdout.strip()
        return json.loads(text), "sha256:" + sha(text.encode())

    def recover(self, iid: str) -> tuple[int, dict, str]:
        p = run([self.cwr, "instance", "recover", iid, "--home", self.home])
        try:
            return p.returncode, json.loads(p.stdout.strip().splitlines()[-1]), p.stderr
        except (ValueError, IndexError):
            return p.returncode, {}, p.stderr


def kill_group(p: subprocess.Popen) -> None:
    with contextlib.suppress(ProcessLookupError):
        os.killpg(p.pid, signal.SIGKILL)
    p.wait(30)


def wait_for(pred, limit: float = 30.0, step: float = 0.02) -> bool:
    end = time.monotonic() + limit
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(step)
    return False


PRE = """import cwr, publish, search, world, kv, fs
def call(f, **kw):
    try:
        return {"ok": f(**kw)}
    except cwr.errors.WorldError as e:
        return {"code": e.code}
"""

WARMUP = PRE + 'kv.set(ns="w", key="x", value=b"warm")\nreturn world.now()\n'


def post_script(key: str, spin: bool = False) -> str:
    s = PRE + f'r = publish.post(key={key!r}, title="T", body="B")\n'
    if spin:
        s += "x = 0\nfor i in range(60_000_000):\n    x += i\n"
    return s + "return r\n"


def retry_script(key: str) -> str:
    return PRE + f'return call(publish.post, key={key!r}, title="T", body="B")\n'


def turn_records(ledger: list[dict], n: int) -> list[dict]:
    return [r for r in ledger if r["turn"] == n and r["kind"] in ("primitive", "adapter")]


def closing(ledger: list[dict], n: int) -> dict | None:
    return next((r for r in ledger if r["kind"] == "turn" and r["turn"] == n), None)


def recovery_record(ledger: list[dict], n: int) -> dict | None:
    return next(
        (r for r in ledger if r["kind"] == "recovery" and r.get("interrupted_turn") == n), None
    )


def strict_ledger_ok(wd: World, iid: str) -> bool:
    return run([wd.cwr, "instance", "ledger", iid, "--home", wd.home]).returncode == 0


def default_initial(wd: World) -> Path:
    """The Build's default initial state, as `cwr instance state` prints it for a fresh Instance."""
    home = wd.scratch / "inithome"
    st, _ = wd.state(wd.create(seed=0, home=home), home=home)
    f = wd.scratch / "default-initial.json"
    f.write_text(canonical(st))
    return f


def replay_prefix(wd: World, iid: str, raw: bytes, label: str) -> tuple[int, dict, str, int]:
    """Replays the completed turns of a crashed ledger from a directory holding only that ledger
    prefix and its turn scripts. Returns (rc, report, state digest of the replay, records)."""
    lines = raw[: raw.rfind(b"\n") + 1].splitlines(keepends=True)
    kinds = [json.loads(x)["kind"] for x in lines]
    cut = max(i for i, k in enumerate(kinds) if k in ("turn", "header")) + 1
    d = wd.scratch / f"prefix-{label}"
    (d / "scripts").mkdir(parents=True)
    (d / "ledger.jsonl").write_bytes(b"".join(lines[:cut]))
    turns = {json.loads(x)["turn"] for x in lines[:cut] if json.loads(x)["kind"] == "turn"}
    for t in turns:
        name = f"turn-{t:06d}.py"
        shutil.copy(wd.home / "instances" / iid / "scripts" / name, d / "scripts" / name)
    into = wd.scratch / f"prefixhome-{label}"
    p = run(
        [
            wd.cwr,
            "instance",
            "replay",
            d / "ledger.jsonl",
            "--build",
            wd.build,
            "--initial",
            wd.initial,
            "--into-home",
            into,
        ]
    )
    rep = json.loads(p.stdout.strip().splitlines()[-1]) if p.stdout.strip() else {}
    digest = wd.state(rep["into"], home=into)[1] if rep.get("into") else ""
    if p.returncode != 0:
        digest += f" replay rc={p.returncode} {p.stderr.strip()[-300:]}"
    return p.returncode, rep, digest, cut


# ------------------------------------------------------------------------------------------------
# Clause 8.3: a crash at every boundary of a turn with one external call
# ------------------------------------------------------------------------------------------------


def boundary(wd: World, label: str) -> None:
    """One scenario. label: before_execute, after_execute, in_flight, after_append_kill, after_append_meta."""
    pub = wd.stubs["publish"]
    iid = wd.create(seed=5)
    wd.turn(iid, WARMUP)
    key = f"key-{label}"
    base = wd.ledger(iid)
    _state_before, digest_before = wd.state(iid)
    n = 2
    first_seq = len(base)
    t0 = time.monotonic()
    killed = False
    if label in ("before_execute", "after_execute"):
        rc, _, _ = wd.turn(iid, post_script(key), env={FAULT_ENV: f"{label}:publish.post"})
        killed = rc == -signal.SIGKILL
    elif label == "after_append_meta":
        rc, _, _ = wd.turn(iid, post_script(key), env={FAULT_ENV: f"after_append:{first_seq}"})
        killed = rc == -signal.SIGKILL
    elif label == "after_append_kill":
        p = wd.spawn(iid, post_script(key, spin=True))
        seen = wait_for(
            lambda: any(r["kind"] == "adapter" and r["turn"] == n for r in wd.ledger(iid))
        )
        time.sleep(0.3)
        killed = seen and p.poll() is None
        kill_group(p)
    elif label == "in_flight":
        pub.schedule(
            [
                {
                    "adapter": "publish.post",
                    "instance": iid,
                    "index": [0],
                    "fault": "TIMEOUT",
                    "delay_ms": 4000,
                }
            ]
        )
        p = wd.spawn(iid, post_script(key))
        seen = wait_for(lambda: len(pub.log(iid)) == 1)
        time.sleep(0.2)
        killed = seen and p.poll() is None
        kill_group(p)
        pub.schedule([])
    crashed_raw = wd.ledger_path(iid).read_bytes()
    crashed = wd.ledger(iid)
    sent_at_crash = len(pub.log(iid))
    R.check(
        "8.3", killed, f"[{label}] kernel process SIGKILLed mid-turn ({time.monotonic() - t0:.1f}s)"
    )

    rc, rep, err = wd.recover(iid)
    after = wd.ledger(iid)
    rec = recovery_record(after, n)
    close = closing(after, n)
    calls = turn_records(after, n)
    adapters = [r for r in calls if r["kind"] == "adapter"]
    _state_after, digest_after = wd.state(iid)
    expected_sent = 0 if label == "before_execute" else 1
    ok = (
        rc == 0
        and after[: len(crashed)] == crashed
        and wd.ledger_path(iid).read_bytes().startswith(crashed_raw)
        and rec is not None
        and rec.get("resumed_from") == crashed[-1]["seq"]
        and isinstance(rec.get("snapshot"), str)
        and rec["snapshot"].startswith("sha256:")
        and close is not None
        and close.get("code") == "UNAVAILABLE"
        and strict_ledger_ok(wd, iid)
        and sent_at_crash == expected_sent
        and digest_after == digest_before
    )
    if label == "before_execute":
        ok = ok and adapters == [] and close.get("status") == "error"
        what = "no adapter record; turn error/UNAVAILABLE"
    elif label in ("after_execute", "in_flight"):
        ok = (
            ok
            and len(adapters) == 1
            and adapters[0].get("reconstructed") is True
            and adapters[0]["outcome"]["code"] == "UNAVAILABLE"
            and close.get("status") == "error"
        )
        ok = ok and not any(r["kind"] == "adapter" and r["turn"] == n for r in crashed)
        what = (
            "no record at crash; reconstructed adapter record UNAVAILABLE; turn error/UNAVAILABLE"
        )
    else:
        ok = (
            ok
            and len(adapters) == 1
            and adapters[0]["outcome"]["status"] == "ok"
            and "reconstructed" not in adapters[0]
            and close.get("status") == "partial"
        )
        ok = ok and any(r["kind"] == "adapter" and r["turn"] == n for r in crashed)
        what = (
            "adapter record durable before crash, kept, not re-executed; turn partial/UNAVAILABLE"
        )
    R.check(
        "8.3",
        ok,
        f"[{label}] recovered from snapshot {str(rec and rec.get('snapshot'))[:19]}.. + ledger prefix (seq 0..{crashed[-1]['seq']} byte-identical), {what}; "
        f"stub saw {sent_at_crash} request(s) at crash; state digest unchanged; report={canonical({k: rep.get(k) for k in ('base', 'rolled_forward', 'reconstructed', 'turn_status', 'resolved_keys')})}"
        + ("" if rc == 0 else f" recover rc={rc} {err.strip()[-300:]}"),
    )

    # Recovery and replay agree: rebuilding the crashed ledger's completed turns from the ledger
    # alone (`cwr instance replay <ledger> --build <dir>`) yields the recovered state digest.
    prc, prep, pdigest, pcut = replay_prefix(wd, iid, crashed_raw, label)
    R.check(
        "8.4",
        prc == 0 and prep.get("identical") is True and pdigest == digest_after,
        f"[{label}] replay of the crashed ledger's {pcut} records up to the last completed turn "
        f"(ledger and scripts only, --build): identical={prep.get('identical')}, state digest "
        f"{pdigest[:23]}.. == recovered {digest_after[:23]}..",
    )

    # The model retries the identical call on its next turn: never a second request for the same effect.
    rc2, env2, err2 = wd.turn(iid, retry_script(key))
    last = wd.ledger(iid)
    retry = [r for r in turn_records(last, n + 1) if r["kind"] == "adapter"]
    via = retry[0].get("gateway", {}).get("via") if retry else None
    got = env2 and env2.get("returned")
    total = len(pub.log(iid))
    if label == "before_execute":
        want_via, want = "service", ("ok" in (got or {}))
    elif label == "in_flight":
        want_via, want = "store", (got or {}).get("code") == "UNAVAILABLE"
    else:
        want_via, want = "store", ("ok" in (got or {}))
    R.check(
        "8.3",
        rc2 == 0 and env2 and env2["status"] == "ok" and via == want_via and want and total == 1,
        f"[{label}] identical retry answered via {via} ({canonical(got)[:80]}); publish stub saw exactly {total} request for key {key}"
        + ("" if rc2 == 0 else f" turn rc={rc2} {err2.strip()[-300:]}"),
    )


def mutating_window(wd: World, point: str) -> None:
    """A mutating primitive in flight: absent (before its record) or committed (record durable,
    metadata behind), never half."""
    iid = wd.create(seed=6)
    wd.turn(iid, WARMUP)
    seq = len(wd.ledger(iid))
    script = PRE + 'kv.set(ns="m", key="k", value=b"committed")\nreturn 1\n'
    rc, _, _ = wd.turn(iid, script, env={FAULT_ENV: f"{point}:{seq}"})
    crashed = wd.ledger(iid)
    rc2, rep, err = wd.recover(iid)
    after = wd.ledger(iid)
    st, _ = wd.state(iid)
    has = (
        bool(st)
        and "m" in st.get("kv", {}).get("namespaces", {})
        and "k" in st["kv"]["namespaces"]["m"]
    )
    close = closing(after, 2)
    rec_present = any(
        r["seq"] == seq and r.get("primitive") == "kv.set" and r["outcome"]["status"] == "ok"
        for r in after
    )
    if point == "before_append":
        ok = (
            rc == -signal.SIGKILL
            and len(crashed) == seq
            and not rec_present
            and not has
            and close
            and close["status"] == "error"
            and close["code"] == "UNAVAILABLE"
        )
        what = "record never durable: mutation absent from state and ledger, turn error/UNAVAILABLE"
    else:
        ok = (
            rc == -signal.SIGKILL
            and len(crashed) == seq + 1
            and rec_present
            and has
            and close
            and close["status"] == "partial"
            and close["code"] == "UNAVAILABLE"
        )
        what = "record durable, metadata not yet updated: mutation committed in state, turn partial/UNAVAILABLE"
    ok = ok and rc2 == 0 and strict_ledger_ok(wd, iid)
    R.check(
        "8.3",
        ok,
        f"[kv.set {point}:{seq}] {what}; rolled_forward={rep.get('rolled_forward')}"
        + ("" if rc2 == 0 else f" recover rc={rc2} {err.strip()[-300:]}"),
    )


def truncated_tail(wd: World) -> None:
    iid = wd.create(seed=7)
    wd.turn(iid, WARMUP)
    rc, _, _ = wd.turn(
        iid,
        PRE
        + 'kv.set(ns="t", key="a", value=b"1")\nreturn publish.post(key="k-trunc", title="T", body="B")\n',
        env={FAULT_ENV: "before_execute:publish.post"},
    )
    good = wd.ledger(iid)
    torn = b'{"seq":%d,"kind":"primitive","outco' % len(good)
    with open(wd.ledger_path(iid), "ab") as f:
        f.write(torn)
    refused = run([wd.cwr, "instance", "state", iid, "--home", wd.home]).returncode != 0
    rc2, rep, err = wd.recover(iid)
    after = wd.ledger(iid)
    raw = wd.ledger_path(iid).read_bytes()
    close = closing(after, 2)
    ok = (
        rc == -signal.SIGKILL
        and refused
        and rc2 == 0
        and torn not in raw
        and after[: len(good)] == good
        and strict_ledger_ok(wd, iid)
        and close
        and close["status"] == "partial"
        and close["code"] == "UNAVAILABLE"
    )
    R.check(
        "8.3",
        ok,
        f"[torn final record] {len(torn)} bytes of a never-durable record refused by a strict open, cut by recovery (truncated_bytes={rep.get('truncated_bytes')}); "
        f"durable prefix kept; turn closed partial/UNAVAILABLE after its kv.set"
        + ("" if rc2 == 0 else f" recover rc={rc2} {err.strip()[-300:]}"),
    )


def implicit_and_live(wd: World) -> None:
    """Recovery happens on the next open, and never touches an Instance a live kernel holds."""
    iid = wd.create(seed=8)
    wd.turn(iid, WARMUP)
    rc, _, _ = wd.turn(
        iid, post_script("k-implicit"), env={FAULT_ENV: "after_execute:publish.post"}
    )
    rc2, env2, err2 = wd.turn(iid, PRE + "return world.now()\n")
    led = wd.ledger(iid)
    ok = (
        rc == -signal.SIGKILL
        and rc2 == 0
        and env2
        and env2["world"]["turn"] == 3
        and recovery_record(led, 2) is not None
        and closing(led, 2)["code"] == "UNAVAILABLE"
        and "recovered" in err2
    )
    R.check(
        "8.3",
        ok,
        f"[next open] `cwr instance turn` on a crashed Instance recovered it first (turn 2 closed UNAVAILABLE) and ran turn {env2 and env2['world']['turn']}",
    )

    iid = wd.create(seed=9)
    p = wd.spawn(
        iid, PRE + "t = world.now()\nx = 0\nfor i in range(40_000_000):\n    x += i\nreturn t\n"
    )
    seen = wait_for(lambda: len(wd.ledger(iid)) >= 2)
    rc3, rep3, _ = wd.recover(iid)
    out, _ = p.communicate(timeout=120)
    led = wd.ledger(iid)
    ok = (
        seen
        and rc3 == 0
        and rep3.get("recovered") is False
        and p.returncode == 0
        and json.loads(out.strip().splitlines()[-1])["status"] == "ok"
        and not any(r["kind"] == "recovery" for r in led)
    )
    R.check(
        "8.3",
        ok,
        f"[live kernel] recover during a running turn reported {canonical(rep3)}; the turn finished ok with no recovery record",
    )


# ------------------------------------------------------------------------------------------------
# Clause 6.6: one PCG stream per Instance, advanced only by draws, equal seeds and prefixes draw equal
# ------------------------------------------------------------------------------------------------

DRAWS = PRE + "return [world.random() for _ in range(NDRAWS)]\n"


def rng_checks(wd: World) -> None:
    a, b, c = wd.create(seed=31), wd.create(seed=31), wd.create(seed=32)
    ra = wd.turn(a, DRAWS.replace("NDRAWS", "4"))[1]
    rb = wd.turn(b, DRAWS.replace("NDRAWS", "4"))[1]
    rc_ = wd.turn(c, DRAWS.replace("NDRAWS", "4"))[1]
    mixed = (
        PRE
        + 'kv.set(ns="r", key="a", value=b"1")\nv = world.random()\ncall(kv.get, ns="r", key="missing")\nreturn [v, world.random()]\n'
    )
    ra2 = wd.turn(a, mixed)[1]
    rb2 = wd.turn(b, mixed)[1]
    cmp = run([wd.cwr, "instance", "compare", wd.ledger_path(a), wd.ledger_path(b)])
    host_differs = any(
        x["host_ts"] != y["host_ts"] for x, y in zip(wd.ledger(a), wd.ledger(b), strict=False)
    )
    ok = (
        ra
        and rb
        and rc_
        and ra["returned"] == rb["returned"]
        and ra2["returned"] == rb2["returned"]
        and ra["returned"] != rc_["returned"]
        and cmp.returncode == 0
        and host_differs
    )
    R.check(
        "6.6",
        ok,
        f"seeds 31/31 draw {ra and ra['returned'][:2]}.. == {rb and rb['returned'][:2]}.. and again after equal turns; seed 32 draws {rc_ and rc_['returned'][:2]}..; "
        f"`cwr instance compare` of the two ledgers: rc={cmp.returncode} (host_ts differ: {host_differs})",
    )

    # A draw is consumed exactly when its record is durable: crash around the world.random record.
    for point, consumed in (("before_append", 0), ("after_append", 1)):
        twin, inst = wd.create(seed=41), wd.create(seed=41)
        wd.turn(twin, WARMUP)
        wd.turn(inst, WARMUP)
        seq = len(wd.ledger(inst))
        rc, _, _ = wd.turn(inst, DRAWS.replace("NDRAWS", "1"), env={FAULT_ENV: f"{point}:{seq}"})
        rrc, _rep, _ = wd.recover(inst)
        got = wd.turn(inst, DRAWS.replace("NDRAWS", "2"))[1]
        ref = wd.turn(twin, DRAWS.replace("NDRAWS", "3"))[1]
        ok = (
            rc == -signal.SIGKILL
            and rrc == 0
            and got
            and ref
            and got["returned"] == ref["returned"][consumed : consumed + 2]
        )
        R.check(
            "6.6",
            ok,
            f"[world.random {point}:{seq}] after recovery the stream continues at draw {consumed + 1} of the equal-seed twin "
            f"({got and got['returned']} == twin {ref and ref['returned']}[{consumed}:{consumed + 2}]): advanced only by durable draws",
        )


# ------------------------------------------------------------------------------------------------
# Clauses 8.4 and 8.5: replay of a 40-turn Instance; tampered ledgers refused
# ------------------------------------------------------------------------------------------------


def replay_script(i: int) -> str:
    s = [PRE, "r = {}"]
    k = i % 8
    if k == 0:
        s += [
            f'kv.set(ns="a", key="k{i}", value=b"v{i}")',
            'r["now"] = world.now()',
            'r["rand"] = world.random()',
        ]
    elif k == 1:
        s += [
            f'r["get"] = call(kv.get, ns="a", key="k{i - 1}")',
            'r["miss"] = call(kv.get, ns="a", key="nope")',
            'r["keys"] = kv.keys(ns="a")',
        ]
    elif k == 2:
        s += [
            f'fs.write(path="/d/f{i}", content=b"c{i}")',
            f'r["read"] = fs.read(path="/d/f{i}")',
            f'fs.lock(path="/d/f{i}")',
            f'r["locked_delete"] = call(fs.delete, path="/d/f{i}")',
            f'fs.unlock(path="/d/f{i}")',
        ]
    elif k == 3:
        s += [
            f'r["hits"] = call(search.query, q="topic{i % 3}", limit=3)',
            'r["rand"] = [world.random(), world.random()]',
        ]
    elif k == 4:
        s += [
            f'p = call(publish.post, key="p{i}", title="T{i}", body="B")',
            f'r["again"] = call(publish.post, key="p{i}", title="T{i}", body="B")',
            f'r["dup"] = call(publish.post, key="p{i}", title="T{i}", body="other")',
            'r["p"] = p',
            'kv.set(ns="posts", key="last", value=p["ok"]["id"].encode()) if "ok" in p else None',
        ]
    elif k == 5:
        s += [
            'pid = call(kv.get, ns="posts", key="last")',
            'r["status"] = call(publish.status, post_id=pid["ok"].decode()) if "ok" in pid else pid',
            'r["missing"] = call(publish.status, post_id="post_none")',
        ]
    elif k == 6:
        s += [
            'r["hist"] = [h["primitive"] for h in world.history(n=6)]',
            f'r["prop"] = world.propose(text="idea {i}")',
            'r["lim"] = world.limits()["primitive_calls"]',
            'try:\n    print("STDOUT-MARKER-2-4")\nexcept BaseException:\n    pass',
        ]
    else:
        s += [f'kv.set(ns="b", key="k{i}", value=b"x")', 'kv.get(ns="b", key="never-set")']
    s.append("return r")
    return "\n".join(s) + "\n"


def rechain(records: list[dict]) -> list[str]:
    """Recomputes the hash chain (crates/cwr-kernel/src/ledger.rs chain_of) after an edit."""
    prev = ""
    lines = []
    for r in records:
        body = {
            k: v for k, v in r.items() if k not in ("chain", "host_ts", "duration_us", "instance")
        }
        r["chain"] = "sha256:" + sha(prev.encode() + b"\n" + canonical(body).encode())
        prev = r["chain"]
        lines.append(canonical(r))
    return lines


def replay_checks(wd: World) -> None:
    seed_home = wd.scratch / "seedhome"
    s = wd.create(seed=1, home=seed_home)
    wd.turn(
        s,
        PRE
        + 'kv.set(ns="a", key="init", value=b"i")\nfs.write(path="/d/init", content=b"i")\nreturn 0\n',
        home=seed_home,
    )
    init_state, _ = wd.state(s, home=seed_home)
    init_file = wd.scratch / "initial.json"
    init_file.write_text(canonical(init_state))

    cassette = wd.scratch / "replay-cassette.jsonl"
    iid = wd.create(seed=77, initial=init_file)
    statuses = []
    for i in range(REPLAY_TURNS):
        rc, env, err = wd.turn(iid, replay_script(i), mode="record", cassette=cassette)
        statuses.append(env["status"] if rc == 0 and env else f"rc{rc}:{err.strip()[-200:]}")
    orig = wd.ledger(iid)
    _orig_state, orig_digest = wd.state(iid)
    kinds = sorted({r["kind"] for r in orig})
    variety = {st: statuses.count(st) for st in sorted(set(statuses))}
    R.check(
        "8.4",
        len([r for r in orig if r["kind"] == "turn"]) == REPLAY_TURNS
        and all(st in ("ok", "partial", "error") for st in statuses),
        f"original Instance: {REPLAY_TURNS} turns recorded ({variety}), {len(orig)} records of kinds {kinds}, adapters in record mode",
    )

    pub_before, search_before = len(wd.stubs["publish"].log()), len(wd.stubs["search"].log())
    into = wd.scratch / "replayhome"
    p = run(
        [
            wd.cwr,
            "instance",
            "replay",
            iid,
            "--home",
            wd.home,
            "--cassette",
            cassette,
            "--into-home",
            into,
        ]
    )
    rep = (
        json.loads(p.stdout.strip().splitlines()[-1])
        if p.returncode in (0, 7) and p.stdout.strip()
        else {}
    )
    new = rep.get("into", "")
    replayed = wd.ledger(new, home=into) if new else []
    py_equal = len(orig) == len(replayed) and all(
        {k: v for k, v in a.items() if k not in ("host_ts", "duration_us", "instance")}
        == {k: v for k, v in b.items() if k not in ("host_ts", "duration_us", "instance")}
        for a, b in zip(orig, replayed, strict=False)
    )
    host_differ = sum(
        1 for a, b in zip(orig, replayed, strict=False) if a["host_ts"] != b["host_ts"]
    )
    _new_state, new_digest = wd.state(new, home=into) if new else (None, "")
    no_traffic = (
        len(wd.stubs["publish"].log()) == pub_before
        and len(wd.stubs["search"].log()) == search_before
        and not wd.stubs["publish"].log(new)
        and not wd.stubs["search"].log(new)
    )
    cmp = (
        run([wd.cwr, "instance", "compare", wd.ledger_path(iid), wd.ledger_path(new, into)])
        if new
        else None
    )
    R.check(
        "8.4",
        p.returncode == 0
        and rep.get("identical") is True
        and py_equal
        and host_differ == len(orig)
        and cmp is not None
        and cmp.returncode == 0,
        f"`cwr instance replay` re-ran {rep.get('turns')} turns into {new[:10]}..: {len(replayed)}/{len(orig)} records identical except host_ts/duration_us "
        f"(independent comparison here; host_ts differ in {host_differ} records), `cwr instance compare` rc={cmp and cmp.returncode}"
        + ("" if p.returncode == 0 else f" replay rc={p.returncode} {p.stderr.strip()[-400:]}"),
    )
    R.check(
        "8.4",
        new_digest == orig_digest and rep.get("state_digest") and no_traffic,
        f"final state digest {orig_digest[:23]}.. == replay {new_digest[:23]}.. (from `cwr instance state`); replay opened no stub connection (stub logs unchanged)",
    )
    draws = sum(1 for r in orig if r.get("primitive") == "world.random")
    R.check(
        "6.6",
        py_equal and draws >= 10,
        f"equal seed 77 and equal ledger prefix in the replay produced equal draws: {draws} world.random records and every turn's returned_hash reproduced",
    )

    # Tampered ledgers are refused.
    def replay_of(ledger: Path, extra: list | None = None) -> subprocess.CompletedProcess:
        return run(
            [
                wd.cwr,
                "instance",
                "replay",
                ledger,
                "--build",
                wd.build,
                "--initial",
                init_file,
                "--cassette",
                cassette,
                "--scripts",
                wd.home / "instances" / iid / "scripts",
                "--into-home",
                wd.scratch / "tamperhome",
            ]
            + (extra or [])
        )

    tdir = wd.scratch / "tamper"
    tdir.mkdir()
    lines = wd.ledger_path(iid).read_text().splitlines()
    victim = next(
        i
        for i, r in enumerate(orig)
        if r["kind"] == "primitive"
        and r["outcome"]["status"] == "ok"
        and r.get("primitive") == "kv.get"
    )
    naive = list(lines)
    rec = json.loads(naive[victim])
    rec["outcome"]["status"], rec["outcome"]["code"] = "error", "NOT_FOUND"
    naive[victim] = canonical(rec)
    (tdir / "naive.jsonl").write_text("\n".join(naive) + "\n")
    p1 = replay_of(tdir / "naive.jsonl")
    created1 = (wd.scratch / "tamperhome" / "instances").is_dir() and any(
        (wd.scratch / "tamperhome" / "instances").iterdir()
    )
    R.check(
        "8.4",
        p1.returncode not in (0, 7) and "chain" in p1.stderr and not created1,
        f"edited record {victim} without re-chaining: replay refused before creating anything (rc={p1.returncode}: {p1.stderr.strip()[-120:]})",
    )

    recs = [json.loads(line) for line in lines]
    recs[victim]["outcome"]["status"], recs[victim]["outcome"]["code"] = "error", "NOT_FOUND"
    (tdir / "rechained.jsonl").write_text("\n".join(rechain(recs)) + "\n")
    p2 = replay_of(tdir / "rechained.jsonl")
    rep2 = json.loads(p2.stdout.strip().splitlines()[-1]) if p2.stdout.strip() else {}
    d = rep2.get("divergence") or {}
    sealed_new = rep2.get("into", "")
    blocked = (
        run(
            [
                wd.cwr,
                "instance",
                "turn",
                sealed_new,
                "--home",
                wd.scratch / "tamperhome",
                "--script",
                wd.scratch / "scripts" / "turn-1.py",
            ]
        ).returncode
        != 0
        if sealed_new
        else False
    )
    R.check(
        "8.4",
        p2.returncode == 7
        and rep2.get("identical") is False
        and d.get("seq") == victim
        and d.get("original", {}).get("code") == "NOT_FOUND"
        and d.get("replayed", {}).get("code") is None
        and blocked,
        f"edited and re-chained record {victim}: replay refused with exit 7 at the first divergent record seq {d.get('seq')} field {d.get('field')} "
        f"(original {canonical(d.get('original'))[:60]} vs replayed {canonical(d.get('replayed'))[:60]}); the replay Instance is sealed (turn refused: {blocked})",
    )

    (tdir / "truncated.jsonl").write_text("\n".join(lines[:-1]) + "\n" + lines[-1][:40])
    p3 = replay_of(tdir / "truncated.jsonl")
    p4 = run(
        [
            wd.cwr,
            "instance",
            "replay",
            wd.ledger_path(iid),
            "--build",
            wd.scratch / "nobuild",
            "--initial",
            init_file,
            "--cassette",
            cassette,
            "--into-home",
            wd.scratch / "tamperhome2",
        ]
    )
    R.check(
        "8.4",
        p3.returncode not in (0, 7) and "truncated" in p3.stderr and p4.returncode != 0,
        f"truncated ledger refused (rc={p3.returncode}); missing Build refused (rc={p4.returncode})",
    )

    # 8.5: replay reads the ledger and the declared replay inputs only, never kernel internals.
    bare = wd.scratch / "bare"
    bare.mkdir()
    shutil.copy(wd.ledger_path(iid), bare / "ledger.jsonl")
    shutil.copytree(wd.home / "instances" / iid / "scripts", bare / "scripts")
    p5 = run(
        [
            wd.cwr,
            "instance",
            "replay",
            bare / "ledger.jsonl",
            "--build",
            wd.build,
            "--initial",
            init_file,
            "--cassette",
            cassette,
            "--into-home",
            wd.scratch / "barehome",
        ]
    )
    rep5 = json.loads(p5.stdout.strip().splitlines()[-1]) if p5.returncode == 0 else {}
    bare_digest = wd.state(rep5["into"], home=wd.scratch / "barehome")[1] if rep5 else ""
    R.check(
        "8.5",
        p5.returncode == 0
        and rep5.get("identical") is True
        and bare_digest == orig_digest
        and sorted(os.listdir(bare)) == ["ledger.jsonl", "scripts"],
        f"replay from a directory holding only ledger.jsonl and the turn scripts (no instance.json, objects, envelopes, gateway store, sandbox output): identical, state digest {bare_digest[:23]}..",
    )
    ledger_text = wd.ledger_path(iid).read_text()
    stdout_absent = "STDOUT-MARKER-2-4" not in ledger_text
    post_ids = set(
        re.findall(r"post_[0-9a-f]{8,}", (wd.scratch / "replay-cassette.jsonl").read_text())
    )
    payload_absent = bool(post_ids) and not any(pid in ledger_text for pid in post_ids)
    R.check(
        "8.5",
        stdout_absent and payload_absent,
        f"the ledger holds no sandbox stdout (marker absent) and no raw adapter payload ({len(post_ids)} post ids in the cassette, none in the ledger; adapter records carry hashes only), "
        "so neither can be a metric source; every oracle in this driver reads only ledgers, `cwr instance state` and the stubs' own logs",
    )


def main() -> int:
    cwr, tests_ok, tests_ev = build_binaries()
    R.check("8.3", tests_ok, tests_ev)
    keep = os.environ.get("CWR_KEEP_SCRATCH")
    scratch = Path(tempfile.mkdtemp(prefix="cwr-2_4-"))
    stubs: dict[str, Stub] = {}
    try:
        stubs = {"publish": Stub("publish", 7, scratch), "search": Stub("search", 7, scratch)}
        wd = World(cwr, scratch, stubs)
        wd.compile()
        wd.initial = default_initial(wd)
        for label in (
            "before_execute",
            "after_execute",
            "in_flight",
            "after_append_kill",
            "after_append_meta",
        ):
            boundary(wd, label)
        mutating_window(wd, "before_append")
        mutating_window(wd, "after_append")
        truncated_tail(wd)
        implicit_and_live(wd)
        rng_checks(wd)
        with OBS.observe("CWR-010.a4", match="replay|final state digest"):
            replay_checks(wd)
    finally:
        for s in stubs.values():
            s.stop()
        if keep:
            print(f"scratch kept at {scratch}", file=sys.stderr)
        else:
            shutil.rmtree(scratch, ignore_errors=True)
    OBS.claim("CWR-010.a1", match="stub saw exactly")
    OBS.claim("CWR-010.a2", match="recovered from snapshot|closed UNAVAILABL")
    OBS.claim("CWR-010.a3", match="identical retry answered via|stub saw exactly")
    OBS.emit()
    rc = 0
    for c in CLAUSES:
        bad, ok = R.bad[c], R.ok[c]
        if bad or not ok:
            rc = 1
            print(f"CLAUSE {c} FAIL " + " | ".join(bad or ["no evidence"]))
        else:
            print(f"CLAUSE {c} PASS " + " | ".join(ok))
    return rc


if __name__ == "__main__":
    sys.exit(main())
