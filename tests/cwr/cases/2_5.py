#!/usr/bin/env python3
# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Task 2.5 driver: instance upgrade, migrations, recompile notice and proposal outbox (case CWR-011).

Clauses: 3.6, 5.1, 5.2, 5.3, 5.4, 5.5 (spec/cwr/spec.kvx). Everything runs against the real `cwr` binary
(built here with cargo, honouring CARGO_TARGET_DIR) on real Builds compiled by `cwr build` from the six
upgrade pairs under examples/refworld-upgrades, with real sandboxed turns. Pair sources are copied into a
temporary directory first; a B side whose world.version fixture still names A's version is patched there
(the copy only) so that it passes its own contract tests, and the driver reports when it had to.

Prints one line per clause, `CLAUSE <req.ac> PASS|FAIL <evidence>`, and exits 0 only when all pass.
"""

from __future__ import annotations

import ast
import base64
import hashlib
import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import protocol

REPO = Path(__file__).resolve().parents[3]
PAIRS = REPO / "examples" / "refworld-upgrades"
UPGRADES = REPO / "benchmark" / "upgrades.kvx"
CLAUSES = ["3.6", "5.1", "5.2", "5.3", "5.4", "5.5"]
SEP_NOTICE = "\n==== cwr context: recompile notice ====\n"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


VALIDATE = load_module("cwr_validate_spec", REPO / "spec" / "cwr" / "tools" / "validate_spec.py")
WORLD = load_module("cwr_sdk_world", REPO / "sdk" / "python" / "cwr" / "_world.py")


class Results:
    def __init__(self):
        self.items: dict[str, list[tuple[bool, str]]] = {c: [] for c in CLAUSES}

    def check(self, clause: str, ok: bool, evidence: str) -> bool:
        self.items[clause].append((bool(ok), evidence))
        return bool(ok)

    def report(self) -> bool:
        all_ok = True
        for c in CLAUSES:
            items = self.items[c]
            ok = bool(items) and all(o for o, _ in items)
            all_ok &= ok
            shown = [e for o, e in items if not o] if not ok else [e for _, e in items]
            print(f"CLAUSE {c} {'PASS' if ok else 'FAIL'} {'; '.join(shown) or 'no checks ran'}")
        return all_ok


R = Results()
OBS = protocol.Observer("2.5", protocol.pairs(R.items))


# ----------------------------------------------------------------------------------------------- tooling


def target_dir() -> Path:
    return Path(os.environ.get("CARGO_TARGET_DIR") or REPO / "target")


def build_cli() -> Path:
    proc = subprocess.run(
        ["cargo", "build", "-q", "-p", "cwr-cli"], cwd=REPO, capture_output=True, text=True
    )
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr[-4000:])
        raise SystemExit("cargo build -p cwr-cli failed")
    return target_dir() / "debug" / "cwr"


CWR = Path()
# Digest of every Build this driver compiled, taken right after `cwr build` wrote it.
BUILD_DIGESTS: dict[str, str] = {}


def cwr(*args, stdin: str | None = None, env: dict | None = None) -> subprocess.CompletedProcess:
    full_env = dict(os.environ)
    full_env.pop("CWR_FAULT_INJECT", None)
    if env:
        full_env.update(env)
    return subprocess.run(
        [str(CWR), *map(str, args)], input=stdin, capture_output=True, text=True, env=full_env
    )


def compile_world(src: Path, out: Path, previous: str | None = None, extra=()) -> tuple[str, Path]:
    """Runs `cwr build`; returns the Build hash and the directory the compiler reports writing."""
    out.mkdir(parents=True, exist_ok=True)
    args = ["build", src, "--out", out, "--allow-example", *extra]
    if previous:
        args += ["--previous", previous]
    proc = cwr(*args)
    m = re.search(
        r"built (?:experimental )?sha256:([0-9a-f]{64})(?: \([^)]*\))? at (\S+)", proc.stdout
    )
    if proc.returncode != 0 or not m:
        raise RuntimeError(
            f"cwr build {src} failed ({proc.returncode}): {proc.stdout[-1500:]}{proc.stderr[-500:]}"
        )
    d = Path(m.group(2))
    BUILD_DIGESTS[str(d)] = tree_digest(d)
    return m.group(1), d


def tree_digest(root: Path) -> str:
    """sha256 over every path, mode and content byte under root (detects any write)."""
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        st = p.lstat()
        h.update(str(p.relative_to(root)).encode() + b"\0" + oct(st.st_mode).encode() + b"\0")
        if p.is_file():
            h.update(p.read_bytes())
    return h.hexdigest()


def writable_entries(root: Path) -> list[str]:
    bits = stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH
    return [str(p) for p in [root, *root.rglob("*")] if p.lstat().st_mode & bits]


# ----------------------------------------------------------------------------------------------- shapes


def parse_shape(s: str):
    """The compiled state-shape grammar (contracts.json `state`), enough to place bytes values."""
    pos = 0

    def ws():
        nonlocal pos
        while pos < len(s) and s[pos] == " ":
            pos += 1

    def ident():
        nonlocal pos
        ws()
        m = re.compile(r"[A-Za-z0-9_]+").match(s, pos)
        pos = m.end()
        return m.group(0)

    def eat(c):
        nonlocal pos
        ws()
        assert s[pos] == c, (s, pos, c)
        pos += 1

    def atom():
        nonlocal pos
        ws()
        if s[pos] == "{":
            pos += 1
            fields = {}
            ws()
            if s[pos] == "}":
                pos += 1
                return ("record", fields)
            while True:
                name = ident()
                eat(":")
                fields[name] = ty()
                ws()
                pos += 1
                if s[pos - 1] == "}":
                    return ("record", fields)
        name = ident()
        if name in ("list", "Optional"):
            eat("[")
            t = ty()
            eat("]")
            return (name, t)
        if name == "dict":
            eat("[")
            ident()
            eat(",")
            t = ty()
            eat("]")
            return ("dict", t)
        return (name,)

    def ty():
        nonlocal pos
        alts = [atom()]
        ws()
        while pos < len(s) and s[pos] == "|":
            pos += 1
            alts.append(atom())
            ws()
        return alts[0] if len(alts) == 1 else ("union", alts)

    return ty()


def to_codec(value, shape):
    """Task-state values (UTF-8 text where the shape says bytes) to the kernel's stored codec form."""
    kind = shape[0]
    if kind == "bytes" and isinstance(value, str):
        return {"$bytes": base64.b64encode(value.encode("utf-8")).decode("ascii")}
    if kind == "record" and isinstance(value, dict):
        return {k: to_codec(v, shape[1].get(k, ("Any",))) for k, v in value.items()}
    if kind == "dict" and isinstance(value, dict):
        return {k: to_codec(v, shape[1]) for k, v in value.items()}
    if kind == "list" and isinstance(value, list):
        return [to_codec(v, shape[1]) for v in value]
    if kind in ("Optional", "union"):
        alts = [shape[1]] if kind == "Optional" else shape[1]
        if value is None:
            return None
        for a in alts:
            if a[0] != "None":
                return to_codec(value, a)
    return value


def initial_state(initial_kvx: Path, build_dir: Path) -> dict:
    shapes = json.loads((build_dir / "contracts.json").read_text())["state"]
    doc = VALIDATE.parse_kvx(initial_kvx.read_text())
    state = {}
    for module, fields in doc.items():
        if module == "meta":
            continue
        state[module] = to_codec(fields, parse_shape(shapes[module]))
    return state


# ----------------------------------------------------------------------------------------------- instances


class Home:
    def __init__(self, root: Path):
        self.root = root

    def create(self, build_dir: Path, initial: dict | None = None, seed: int = 11, extra=()) -> str:
        args = ["instance", "create", "--home", self.root, build_dir, "--seed", seed, *extra]
        if initial is not None:
            f = self.root.parent / f"initial-{len(list(self.root.parent.glob('initial-*')))}.json"
            f.write_text(json.dumps(initial))
            args += ["--initial", f]
        proc = cwr(*args)
        if proc.returncode != 0:
            raise RuntimeError(f"create failed: {proc.stderr}")
        return proc.stdout.strip()

    def dir(self, iid: str) -> Path:
        return self.root / "instances" / iid

    def turn(self, iid: str, script: str) -> tuple[int, dict | None, str]:
        proc = cwr("instance", "turn", iid, "--home", self.root, stdin=script)
        env = None
        if proc.stdout.strip():
            try:
                env = json.loads(proc.stdout.strip().splitlines()[-1])
            except ValueError:
                env = None
        return proc.returncode, env, proc.stderr

    def state(self, iid: str) -> dict:
        proc = cwr("instance", "state", iid, "--home", self.root)
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr)
        return json.loads(proc.stdout)

    def meta(self, iid: str) -> dict:
        return json.loads((self.dir(iid) / "instance.json").read_text())

    def ledger_bytes(self, iid: str) -> bytes:
        return (self.dir(iid) / "ledger.jsonl").read_bytes()

    def records(self, iid: str) -> list[dict]:
        return [json.loads(x) for x in self.ledger_bytes(iid).decode().splitlines()]

    def upgrade(self, iid: str, build_dir, *extra, env=None) -> subprocess.CompletedProcess:
        return cwr("instance", "upgrade", iid, build_dir, "--home", self.root, *extra, env=env)

    def notice(self, iid: str) -> str:
        proc = cwr("instance", "notice", iid, "--home", self.root)
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr)
        return proc.stdout[:-1] if proc.stdout.endswith("\n") else proc.stdout

    def context(self, iid: str) -> str:
        proc = cwr("instance", "context", iid, "--home", self.root)
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr)
        return proc.stdout


def snapshot_of(home: Home, iid: str) -> dict:
    """Everything a turn could observe plus the binding: state, ledger bytes, Build, status."""
    m = home.meta(iid)
    return {
        "state": home.state(iid),
        "ledger": home.ledger_bytes(iid),
        "build": m["build"],
        "status": m["status"],
        "version": m["world"]["version"],
    }


# ----------------------------------------------------------------------------------------------- pairs


def prepare_pair(work: Path, pair: str) -> tuple[Path, Path, bool]:
    """Copies UN/A and UN/B; patches B's world.version fixture to B's version when it names A's."""
    dst = work / "src" / pair
    shutil.rmtree(dst, ignore_errors=True)
    shutil.copytree(PAIRS / pair / "A", dst / "A")
    shutil.copytree(PAIRS / pair / "B", dst / "B")
    version_b = VALIDATE.parse_kvx((dst / "B" / "world.kvx").read_text())["world"]["version"]
    fx = dst / "B" / "fixtures" / "world" / "version.kvx"
    patched = False
    if fx.is_file():
        text = fx.read_text()
        new = re.sub(r'^returned = "[0-9.]+"$', f'returned = "{version_b}"', text, flags=re.M)
        patched = new != text
        fx.write_text(new)
    return dst / "A", dst / "B", patched


def strip_host(records: list[dict]) -> list[dict]:
    return [
        {k: v for k, v in r.items() if k not in ("host_ts", "duration_us", "instance")}
        for r in records
    ]


def envelope_core(env: dict) -> dict:
    e = dict(env)
    e["resources"] = {k: v for k, v in env["resources"].items() if k not in ("cpu_ms", "wall_ms")}
    return e


# ----------------------------------------------------------------------------------------------- scenario


def run_pairs(work: Path, home: Home, frozen: dict, builds: dict) -> dict:
    """Upgrade one Instance per pair from A to B at the pair's midpoint and check notice and state."""
    out = {}
    for pair in frozen["meta"]["pairs"]:
        task = frozen[f"task.{pair}"]
        meta = frozen[f"pair.{pair}"]
        note = frozen[f"notice.{pair}"]
        src_a, src_b, patched = prepare_pair(work, pair)
        fixture_note = " (B world.version fixture patched in the scratch copy)" if patched else ""
        bout = work / "builds" / pair
        ha, da = compile_world(src_a, bout)
        hb, db = compile_world(src_b, bout, previous=ha)
        builds[pair] = (da, db)
        tdir = REPO / task["dir"]
        iid = home.create(da, initial_state(tdir / "initial.kvx", da))
        for s in task["first_half"]:
            rc, env, _ = home.turn(iid, (PAIRS / pair / s).read_text())
            R.check(
                "5.2",
                rc == 0 and env and env["status"] == "ok",
                f"{pair} first half {s} on A: {env and env['status']} rc={rc}",
            )
        before = snapshot_of(home, iid)
        proc = home.upgrade(iid, db)
        ok = R.check(
            "5.2",
            proc.returncode == 0,
            f"{pair} upgrade {meta['from_version']}->{meta['to_version']}{fixture_note} exit {proc.returncode} {proc.stderr.strip()[:300]}",
        )
        if not ok:
            continue
        res = json.loads(proc.stdout)
        rec = res["record"]
        after = snapshot_of(home, iid)
        records = home.records(iid)
        snap_file = home.dir(iid) / "snapshots" / (rec["snapshot"].split(":")[1] + ".json")
        snap_state = json.loads(snap_file.read_text())["state"] if snap_file.is_file() else None
        R.check(
            "5.2",
            rec["kind"] == "upgrade"
            and rec["old_build"] == f"sha256:{ha}"
            and rec["new_build"] == f"sha256:{hb}"
            and rec["build"] == f"sha256:{hb}"
            and rec["seq"] == len(before["ledger"].splitlines())
            and after["ledger"].startswith(before["ledger"])
            and len(records) == len(before["ledger"].splitlines()) + 1
            and rec["migrations"] == meta["migrations"]
            and after["build"] == f"sha256:{hb}"
            and after["status"] == "idle"
            and snap_state == before["state"],
            f"{pair}: upgrade record seq {rec['seq']} binds sha256:{hb[:12]}, migrations {rec['migrations']}, pre-upgrade snapshot {rec['snapshot'][:19]} holds the prior state, ledger prefix intact",
        )
        # State after the upgrade: unchanged, or migrated by the declared pure function (U5).
        if meta["migrations"]:
            flat = before["state"]["kv"]
            want = dict(before["state"])
            want["kv"] = {"namespaces": {"default": flat} if flat else {}}
            R.check(
                "3.6",
                after["state"] == want,
                f"{pair}: kv migrated by {meta['migrations'][0]}: {len(flat)} flat keys now under namespace default, other modules untouched",
            )
            R.check("5.2", after["state"] == want, f"{pair}: migrated state committed")
        else:
            R.check(
                "5.2",
                after["state"] == before["state"],
                f"{pair}: no migration declared, state byte-identical across the upgrade",
            )
        # Notice: stored, pending, rendered into component 2 of the next context only.
        expected = (
            note["text"].replace("{hash_a}", f"sha256:{ha}").replace("{hash_b}", f"sha256:{hb}")
        )
        shown = home.notice(iid)
        ctx = home.context(iid)
        R.check(
            "5.3",
            shown == expected and SEP_NOTICE + expected + "\n" in ctx and res["notice"] == expected,
            f"{pair}: notice equals frozen [notice.{pair}].text ({len(expected)} bytes, listed {note['listed_targets']}) and is component 2 of the next context",
        )
        for s in task["post_upgrade"][:1]:
            rc, env, _ = home.turn(iid, (PAIRS / pair / s).read_text())
            coords = WORLD.coordinates(env) if env else None
            R.check(
                "5.3",
                rc == 0
                and coords
                and coords.version == meta["to_version"]
                and coords.build == f"sha256:{hb}"
                and env["status"] == "ok",
                f"{pair}: first turn after upgrade ({s}) {env and env['status']}, envelope world.version {coords and coords.version}, world.build new",
            )
        ctx2 = home.context(iid)
        R.check(
            "5.3",
            home.notice(iid) == "" and SEP_NOTICE not in ctx2,
            f"{pair}: notice absent from the context of the second turn after the upgrade",
        )
        out[pair] = {
            "iid": iid,
            "ha": ha,
            "hb": hb,
            "da": da,
            "db": db,
            "src_a": src_a,
            "src_b": src_b,
        }
    return out


def run_failures(work: Path, home: Home, pairs: dict) -> None:
    """Failing migrations, crashes, tampered and refused Builds (clauses 3.6, 5.2, 5.5)."""
    p5 = pairs["U5"]
    da, db, ha = p5["da"], p5["db"], p5["ha"]
    task = REPO / "examples" / "refworld-upgrades" / "U5" / "task"
    base = initial_state(task / "initial.kvx", da)

    # A U5/B variant whose migration passes its fixtures but fails on poisoned states at runtime.
    bad_src = work / "src" / "U5bad"
    shutil.rmtree(bad_src, ignore_errors=True)
    shutil.copytree(p5["src_b"], bad_src)
    mig = next((bad_src / "migrations").glob("*.py"))
    text = mig.read_text()
    guard = (
        "def migrate(state: dict) -> dict:\n"
        '    kv0 = state.get("kv", {})\n'
        '    if "poison" in kv0:\n'
        '        raise ValueError("poisoned key")\n'
        '    if "badshape" in kv0:\n'
        "        return dict(state)\n"
        '    if "mutate" in kv0:\n'
        '        kv0["mutated"] = kv0["mutate"]\n'
    )
    mig.write_text(text.replace("def migrate(state: dict) -> dict:\n", guard, 1))
    _, dbad = compile_world(bad_src, da.parent, previous=ha)

    for key, why in [
        ("poison", "migrate raised"),
        ("badshape", "state.kv"),
        ("mutate", "mutated its argument"),
    ]:
        st = json.loads(json.dumps(base))
        st["kv"][key] = {"$bytes": base64.b64encode(b"x").decode()}
        iid = home.create(da, st)
        before = snapshot_of(home, iid)
        proc = home.upgrade(iid, dbad)
        after = snapshot_of(home, iid)
        leftovers = (
            [p.name for p in (home.dir(iid) / "notices").glob("*")]
            if (home.dir(iid) / "notices").exists()
            else []
        )
        rc, env, _ = home.turn(iid, 'import kv\nreturn kv.get(key="receipt-golf")\n')
        clause = "3.6" if key == "mutate" else "5.2"
        R.check(
            clause,
            proc.returncode == 6
            and why in proc.stderr
            and after == before
            and not leftovers
            and not (home.dir(iid) / "upgrade.json").exists()
            and rc == 0
            and env["status"] == "ok"
            and env["world"]["version"] == "0.2.0",
            f"failing migration ({key}): exit {proc.returncode}, '{why}' reported, rolled back to the snapshot with Build A bound, ledger/state byte-identical, next turn ok on 0.2.0",
        )

    # Crash after the migration ran, before the commit: the next upgrade reports it and rolls back.
    iid = home.create(da, base)
    before = snapshot_of(home, iid)
    crash = home.upgrade(iid, db, env={"CWR_FAULT_INJECT": "upgrade:after_migrate"})
    mid = home.meta(iid)["status"]
    rc_turn, _, _ = home.turn(iid, "import world\nreturn world.now()\n")
    retry = home.upgrade(iid, db)
    after = snapshot_of(home, iid)
    R.check(
        "5.2",
        crash.returncode != 0
        and mid == "upgrading"
        and rc_turn != 0
        and retry.returncode == 6
        and "rolled_back_interrupted" in retry.stdout
        and after == before,
        f"crash mid-migration: kernel killed (rc {crash.returncode}), Instance left '{mid}', turn refused (rc {rc_turn}), next upgrade exits {retry.returncode} reporting the earlier failure, pre-upgrade snapshot restored, no upgrade record",
    )
    again = home.upgrade(iid, db)
    R.check(
        "5.2",
        again.returncode == 0,
        f"after the rollback the same upgrade commits (exit {again.returncode})",
    )

    # Crash after the commit point: the next upgrade completes the commit (roll forward).
    iid = home.create(da, base)
    crash = home.upgrade(iid, db, env={"CWR_FAULT_INJECT": "upgrade:after_commit"})
    retry = home.upgrade(iid, db)
    m = home.meta(iid)
    R.check(
        "5.2",
        crash.returncode != 0
        and retry.returncode == 0
        and "completed_interrupted" in retry.stdout
        and m["build"] == f"sha256:{p5['hb']}"
        and m["status"] == "idle"
        and home.notice(iid).startswith("WORLD RECOMPILED  0.2.0 -> 0.3.0")
        and "namespaces" in home.state(iid)["kv"],
        f"crash after the durable upgrade record: next upgrade exits {retry.returncode} and completes the commit; Build B bound, notice pending, state migrated",
    )

    # Tampered target Build: hash verification refuses it and nothing changes.
    tampered = work / "builds" / "tampered"
    shutil.rmtree(tampered, ignore_errors=True)
    shutil.copytree(db, tampered)
    for p in [tampered, *tampered.rglob("*")]:
        p.chmod(p.stat().st_mode | stat.S_IWUSR)
    with open(tampered / "manual.md", "ab") as f:
        f.write(b" ")
    iid = home.create(da, base)
    before = snapshot_of(home, iid)
    proc = home.upgrade(iid, tampered)
    R.check(
        "5.2",
        proc.returncode == 5 and "verification" in proc.stderr and snapshot_of(home, iid) == before,
        f"one byte appended to manual.md of a copy of Build B: upgrade refused at hash verification (exit {proc.returncode}), Instance unchanged",
    )

    # Version and name rules (5.5) on the U5 Instance that is now on B 0.3.0.
    on_b = pairs["U5"]["iid"]
    before = snapshot_of(home, on_b)
    down = home.upgrade(on_b, da)
    same = home.upgrade(on_b, db)
    equal = home.upgrade(on_b, dbad)
    R.check(
        "5.5",
        down.returncode == 5
        and "not greater" in down.stderr
        and same.returncode == 5
        and equal.returncode == 5
        and "not greater" in equal.stderr
        and snapshot_of(home, on_b) == before,
        f"0.3.0 Instance: upgrade to 0.2.0 exit {down.returncode}, to its own Build exit {same.returncode}, to another 0.3.0 Build exit {equal.returncode}; nothing changed",
    )
    other_src = work / "src" / "other"
    shutil.rmtree(other_src, ignore_errors=True)
    shutil.copytree(pairs["U6"]["src_b"], other_src)
    wk = other_src / "world.kvx"
    wk.write_text(wk.read_text().replace('name = "refworld"', 'name = "otherworld"', 1))
    _, dother = compile_world(other_src, work / "builds" / "other")
    iid = home.create(pairs["U6"]["da"])
    before = snapshot_of(home, iid)
    proc = home.upgrade(iid, dother)
    R.check(
        "5.5",
        proc.returncode == 5 and "world name" in proc.stderr and snapshot_of(home, iid) == before,
        f"refworld 0.2.0 Instance to otherworld 0.2.1: refused (exit {proc.returncode}), nothing changed",
    )

    # 3.6, compiler side: a state-shape change without a declared migration fails the build.
    nomig = work / "src" / "U5nomig"
    shutil.rmtree(nomig, ignore_errors=True)
    shutil.copytree(p5["src_b"], nomig)
    cl = nomig / "CHANGELOG.kvx"
    cl.write_text(
        re.sub(
            r'^migration = "migrations/[^"]+"$',
            'migration = ""',
            cl.read_text(),
            count=1,
            flags=re.M,
        )
    )
    for f in (nomig / "fixtures" / "kv").glob("migration_*.kvx"):
        f.unlink()
    for f in (nomig / "migrations").glob("*.py"):
        f.unlink()
    builds_before = sorted(p.name for p in da.parent.iterdir())
    proc = cwr("build", nomig, "--out", da.parent, "--previous", ha, "--allow-example")
    R.check(
        "3.6",
        proc.returncode != 0
        and "CWR-M001" in proc.stdout + proc.stderr
        and sorted(p.name for p in da.parent.iterdir()) == builds_before,
        f"U5/B with the kv state_shape change's migration removed: cwr build exit {proc.returncode} with CWR-M001, nothing written",
    )


HEADER = (
    "# Copyright Sidiora Labs Inc. CWR\u00ae 2026-2027\n"
    "# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0\n"
)
# Two migrations of release 0.3.1, listed archive first. Their file names sort the other way, so
# only the changelog order applies them correctly: mirror needs the namespace archive creates,
# and both need the namespaced kv that the 0.3.0 migration produces. A kv key "intermediate-bad"
# makes archive leave an unknown jobs field that mirror removes again, so only a check of each
# intermediate result (jobs is targeted by no later migration) can refuse that upgrade.
CHAIN_ARCHIVE = HEADER + (
    "def migrate(state: dict) -> dict:\n"
    '    namespaces = state["kv"]["namespaces"]\n'
    "    out = dict(state)\n"
    "    ns = dict(namespaces)\n"
    '    ns["archive"] = dict(namespaces.get("default", {}))\n'
    '    out["kv"] = {"namespaces": ns}\n'
    '    if "intermediate-bad" in namespaces.get("default", {}):\n'
    '        jobs = dict(state.get("jobs", {}))\n'
    '        jobs["bogus"] = 1\n'
    '        out["jobs"] = jobs\n'
    "    return out\n"
)
CHAIN_MIRROR = HEADER + (
    "def migrate(state: dict) -> dict:\n"
    '    namespaces = state["kv"]["namespaces"]\n'
    "    out = dict(state)\n"
    "    ns = dict(namespaces)\n"
    '    ns["mirror"] = dict(namespaces["archive"])\n'
    '    out["kv"] = {"namespaces": ns}\n'
    '    if "bogus" in state.get("jobs", {}):\n'
    '        jobs = dict(state["jobs"])\n'
    '        del jobs["bogus"]\n'
    '        out["jobs"] = jobs\n'
    "    return out\n"
)
CHAIN_RELEASE = """[release.0.3.1]
version = "0.3.1"
date = "2026-11-06"
summary = "kv gains the namespaces archive and mirror, each a copy of namespace default."
changes = ["kv_archive", "kv_mirror"]

[change.kv_archive]
kind = "doc_only"
target = "kv.has"
before = "kv.has documents no namespace archive."
after = "kv.has documents that namespace archive holds a copy of namespace default."
migration = "migrations/0_3_0_0_3_1_z_archive.py"

[change.kv_mirror]
kind = "doc_only"
target = "kv.get"
before = "kv.get documents no namespace mirror."
after = "kv.get documents that namespace mirror holds a copy of namespace archive."
migration = "migrations/0_3_0_0_3_1_a_mirror.py"

"""


def chain_fixture(name: str, path: str, before: dict, after: dict) -> str:
    return HEADER + (
        "\n[meta]\n"
        'format = "cwr.migration-fixture"\n'
        "schema_version = 1\n"
        'module = "kv"\n'
        f'migration = "{path}"\n'
        f"\n[migration.{name}]\n"
        f"before = {json.dumps(before)}\n"
        f"after = {json.dumps(after)}\n"
    )


def chain_source(work: Path, src_b: Path) -> Path:
    """U5/B (0.3.0) plus release 0.3.1 declaring two more migrations: World Source for 0.3.1."""
    dst = work / "src" / "U5chain"
    shutil.rmtree(dst, ignore_errors=True)
    shutil.copytree(src_b, dst)
    wk = dst / "world.kvx"
    wk.write_text(wk.read_text().replace('version = "0.3.0"', 'version = "0.3.1"', 1))
    fx = dst / "fixtures" / "world" / "version.kvx"
    fx.write_text(fx.read_text().replace('returned = "0.3.0"', 'returned = "0.3.1"'))
    cl = dst / "CHANGELOG.kvx"
    cl.write_text(cl.read_text().replace("[release.0.3.0]", CHAIN_RELEASE + "[release.0.3.0]", 1))
    kv = dst / "contracts" / "kv.kvx"
    kv.write_text(
        kv.read_text()
        .replace(
            'doc = "True when key is stored in namespace ns. Never fails."',
            'doc = "True when key is stored in namespace ns; archive holds a copy of default. Never fails."',
            1,
        )
        .replace(
            'doc = "Value stored under key in namespace ns."',
            'doc = "Value stored under key in namespace ns; mirror holds a copy of archive."',
            1,
        )
    )
    archive, mirror = "migrations/0_3_0_0_3_1_z_archive.py", "migrations/0_3_0_0_3_1_a_mirror.py"
    (dst / archive).write_text(CHAIN_ARCHIVE)
    (dst / mirror).write_text(CHAIN_MIRROR)
    one = {"default": {"a": "1"}}
    two = {"default": {"a": "1"}, "archive": {"a": "1"}}
    three = {"default": {"a": "1"}, "archive": {"a": "1"}, "mirror": {"a": "1"}}
    (dst / "fixtures" / "kv" / "migration_0_3_1_archive.kvx").write_text(
        chain_fixture("copy", archive, {"kv": {"namespaces": one}}, {"kv": {"namespaces": two}})
    )
    (dst / "fixtures" / "kv" / "migration_0_3_1_mirror.kvx").write_text(
        chain_fixture("copy", mirror, {"kv": {"namespaces": two}}, {"kv": {"namespaces": three}})
    )
    return dst


def run_chain(work: Path, home: Home, pairs: dict) -> None:
    """Several migrations across two releases, applied in changelog order, each result validated;
    an interrupted upgrade completed on the way to a different requested Build (clauses 3.6, 5.2)."""
    p5 = pairs["U5"]
    da, db = p5["da"], p5["db"]
    src = chain_source(work, p5["src_b"])
    hc, dc = compile_world(src, da.parent, previous=p5["hb"])
    base = initial_state(REPO / "examples/refworld-upgrades/U5/task/initial.kvx", da)
    chain = [
        "migrations/0_2_0_0_3_0_kv_namespaces.py",
        "migrations/0_3_0_0_3_1_z_archive.py",
        "migrations/0_3_0_0_3_1_a_mirror.py",
    ]

    # 0.2.0 -> 0.3.1 in one upgrade: three migrations over two releases, in changelog order.
    iid = home.create(da, base)
    before = snapshot_of(home, iid)
    proc = home.upgrade(iid, dc)
    rec = json.loads(proc.stdout)["record"] if proc.returncode == 0 else {}
    flat = before["state"]["kv"]
    want = dict(before["state"])
    want["kv"] = {"namespaces": {"default": flat, "archive": flat, "mirror": flat}}
    got = home.state(iid)
    ok = (
        proc.returncode == 0
        and rec.get("migrations") == chain
        and got == want
        and home.meta(iid)["build"] == f"sha256:{hc}"
    )
    evidence = f"0.2.0->0.3.1 upgrade (exit {proc.returncode}) applied {rec.get('migrations')} in changelog order (release 0.3.0, then 0.3.1 as listed, not by file name); kv now holds namespaces {sorted(got['kv'].get('namespaces', {}))} with {len(flat)} keys each"
    R.check("5.2", ok, evidence + (f"; stderr {proc.stderr.strip()[:300]}" if not ok else ""))
    R.check("3.6", ok, f"declared migrations ran as pure functions in order: {len(chain)} applied")

    # An intermediate result that breaks a module no later migration targets is refused, even
    # though the final result would validate.
    bad = json.loads(json.dumps(base))
    bad["kv"]["intermediate-bad"] = {"$bytes": base64.b64encode(b"x").decode()}
    iid = home.create(da, bad)
    before = snapshot_of(home, iid)
    proc = home.upgrade(iid, dc)
    R.check(
        "5.2",
        proc.returncode == 6
        and "0_3_0_0_3_1_z_archive.py" in proc.stderr
        and "state.jobs" in proc.stderr
        and snapshot_of(home, iid) == before
        and not (home.dir(iid) / "upgrade.json").exists(),
        f"the archive migration leaves an unknown jobs field that the next migration would remove: the intermediate result is refused (exit {proc.returncode}, '{proc.stderr.strip()[-160:]}'), Instance rolled back unchanged",
    )

    # Interrupted after the commit to 0.3.0, then asked for 0.3.1: the earlier commit is completed
    # and the requested upgrade runs from 0.3.0; the exit code is the requested upgrade's.
    iid = home.create(da, base)
    crash = home.upgrade(iid, db, env={"CWR_FAULT_INJECT": "upgrade:after_commit"})
    proc = home.upgrade(iid, dc)
    out = json.loads(proc.stdout) if proc.returncode == 0 else {}
    ups = [r for r in home.records(iid) if r["kind"] == "upgrade"]
    R.check(
        "5.2",
        crash.returncode != 0
        and proc.returncode == 0
        and "completed_interrupted" in out
        and [(r["new_version"], r["migrations"]) for r in ups]
        == [("0.3.0", chain[:1]), ("0.3.1", chain[1:])]
        and home.meta(iid)["build"] == f"sha256:{hc}"
        and home.state(iid) == want,
        f"crash after the 0.3.0 commit, then upgrade to 0.3.1: exit {proc.returncode}, earlier commit completed and reported, then the requested upgrade committed ({len(ups)} upgrade records, 0.3.1 bound)",
    )
    # Interrupted after the commit to 0.3.1, then asked for 0.3.0: completing 0.3.1 does not make
    # the request succeed; the request is refused as a downgrade (exit 5) and 0.3.1 stays bound.
    iid = home.create(da, base)
    crash = home.upgrade(iid, dc, env={"CWR_FAULT_INJECT": "upgrade:after_commit"})
    proc = home.upgrade(iid, db)
    R.check(
        "5.2",
        crash.returncode != 0
        and proc.returncode == 5
        and "had committed" in proc.stderr
        and "not greater" in proc.stderr
        and home.meta(iid)["build"] == f"sha256:{hc}"
        and home.meta(iid)["status"] == "idle",
        f"crash after the 0.3.1 commit, then upgrade to 0.3.0: earlier commit completed, request refused (exit {proc.returncode}), 0.3.1 bound",
    )
    retry = home.upgrade(iid, dc)
    R.check(
        "5.2",
        retry.returncode == 5,
        f"asking again for the Build an interrupted upgrade already bound, once resolved, is refused as the bound Build (exit {retry.returncode})",
    )


def forge_experimental(src_build: Path, out_root: Path, flag: str) -> Path:
    """Fallback only when the compiler does not yet write experimental Builds: a copy of a control
    Build with the manifest marker set and the Build hash recomputed (compiler.md section 7)."""
    tmp = out_root / "forge"
    shutil.rmtree(tmp, ignore_errors=True)
    shutil.copytree(src_build, tmp)
    for p in [tmp, *tmp.rglob("*")]:
        p.chmod(p.stat().st_mode | stat.S_IWUSR)
    man = tmp / "manifest.kvx"
    lines = man.read_text().splitlines(keepends=True)[:-1]
    body = (
        "".join(lines)
        .replace("experimental = false", "experimental = true")
        .replace("experimental_flag = null", f'experimental_flag = "{flag}"')
    )
    man.write_text(body)
    files = []
    for name in ["manifest.kvx", "contracts.json", "contract_diff.json", "manual.md", "tests.json"]:
        if (tmp / name).is_file():
            files.append(name)
    for d in ["sdk", "lib", "adapters", "fixtures", "migrations"]:
        if (tmp / d).is_dir():
            sub = sorted(str(p.relative_to(tmp)) for p in (tmp / d).rglob("*") if p.is_file())
            files += sub or [d + "/"]
    files.sort(key=lambda s: s.encode())
    h = hashlib.sha256()
    for f in files:
        data = b"" if f.endswith("/") else (tmp / f).read_bytes()
        h.update(f.encode() + b"\0" + str(len(data)).encode() + b"\0" + data)
    hexd = h.hexdigest()
    man.write_text(body + f'hash = "sha256:{hexd}"\n')
    final = out_root / hexd
    shutil.rmtree(final, ignore_errors=True)
    tmp.rename(final)
    return final


def experimental_build(work: Path, src: Path, flag: str, control: Path) -> tuple[Path, str]:
    out = work / "builds" / f"x-{flag}"
    shutil.rmtree(out, ignore_errors=True)
    _, d = compile_world(src, out, extra=[f"--{flag}"])
    doc = VALIDATE.parse_kvx((d / "manifest.kvx").read_text())
    if doc["build"].get("experimental") is True and doc["build"].get("experimental_flag") == flag:
        return d, "compiled by cwr build"
    return forge_experimental(
        control, out, flag
    ), "forged fixture (cwr build did not mark it experimental)"


def run_experimental(work: Path, home: Home, pairs: dict) -> None:
    p6 = pairs["U6"]
    for flag, rendering in [("x-version-only", "header"), ("x-silent-change", "nothing")]:
        dx, how = experimental_build(work, p6["src_a"], flag, p6["da"])
        iid = home.create(p6["da"])
        rc0, env0, _ = home.turn(
            iid,
            'import fs\nreturn fs.read(path="/notes/readme.md") if fs.exists(path="/notes/readme.md") else None\n',
        )
        before = snapshot_of(home, iid)
        refused = home.upgrade(iid, dx)
        unchanged = snapshot_of(home, iid) == before
        allowed = home.upgrade(iid, dx, "--allow-experimental")
        shown = home.notice(iid) if allowed.returncode == 0 else None
        ctx = home.context(iid) if allowed.returncode == 0 else ""
        stored = sorted((home.dir(iid) / "notices").glob("*.json"))
        header = f"WORLD RECOMPILED  0.2.0 -> 0.2.0   (sha256:{p6['ha']} -> sha256:{dx.name})"
        want = header if rendering == "header" else ""
        R.check(
            "5.5",
            rc0 == 0
            and env0["status"] == "ok"
            and refused.returncode == 5
            and "experimental" in refused.stderr
            and unchanged
            and allowed.returncode == 0,
            f"{flag} Build at the same version 0.2.0 ({how}): refused without --allow-experimental (exit {refused.returncode}), accepted with it (exit {allowed.returncode})",
        )
        R.check(
            "5.3",
            shown == want and (SEP_NOTICE in ctx) == (rendering == "header") and len(stored) == 1,
            f"{flag}: the stored notice exists but component 2 renders {rendering} ({len(shown or '')} bytes)",
        )
    # The flag lifts the version rule only for an experimental target.
    iid = home.create(p6["db"])
    proc = home.upgrade(iid, p6["da"], "--allow-experimental")
    R.check(
        "5.5",
        proc.returncode == 5 and "not greater" in proc.stderr,
        f"--allow-experimental does not admit a control Build with a lower version (exit {proc.returncode})",
    )


def run_outbox(work: Path, home: Home, pairs: dict) -> None:
    p6 = pairs["U6"]
    initial = initial_state(REPO / "examples/refworld-upgrades/U6/task/initial.kvx", p6["da"])
    text = "Please add fs.append so notes can grow without rewriting the file."
    script = f"import world\npid = world.propose(text={text!r})\nreturn {{'pid': pid, 'now': world.now()}}\n"
    probe = (
        "import world, fs\n"
        "h = world.history(n=50)\n"
        "return {'kinds': [r['kind'] for r in h], 'texts': [r.get('text') for r in h if r['kind'] == 'proposal'], "
        "'limits': world.limits(), 'notes': fs.read(path='/notes/readme.md'), 'r': world.random()}\n"
    )
    a = home.create(p6["da"], initial, seed=5)
    b = home.create(p6["da"], initial, seed=5)
    state0 = home.state(a)
    rc, env, _ = home.turn(a, script)
    home.turn(b, script)
    outbox = home.dir(a) / "proposals.jsonl"
    lines = outbox.read_text().splitlines() if outbox.is_file() else []
    recs = [r for r in home.records(a) if r["kind"] == "proposal"]
    pid = env["returned"]["pid"] if env and env["status"] == "ok" else None
    R.check(
        "5.4",
        rc == 0
        and env["status"] == "ok"
        and len(recs) == 1
        and recs[0]["id"] == pid
        and recs[0]["text_hash"] == "sha256:" + hashlib.sha256(text.encode()).hexdigest()
        and "text" not in recs[0]
        and lines
        == [json.dumps({"id": pid, "text": text, "turn": 1}, sort_keys=True, separators=(",", ":"))]
        and home.state(a) == state0,
        f"world.propose returned {pid}: one proposal ledger record carrying only the text hash, one outbox line with the text, state unchanged",
    )
    # Repeated proposals (verification.md CWR-011): every call gets its own id, in call order, and
    # the outbox lines and ledger records match one to one by id. A refused proposal takes no id.
    texts = ["first idea", "second idea", "third idea", "next turn idea"]
    c = home.create(p6["da"], initial, seed=5)
    rc1, env1, _ = home.turn(
        c,
        "import world\n"
        f"ids = [world.propose(text={texts[0]!r}), world.propose(text={texts[1]!r})]\n"
        "try:\n"
        "    world.propose(text='x' * 5000)\n"
        "    ids.append('accepted')\n"
        "except Exception as e:\n"
        "    ids.append('refused')\n"
        f"ids.append(world.propose(text={texts[2]!r}))\n"
        "return ids\n",
    )
    rc2, env2, _ = home.turn(c, f"import world\nreturn world.propose(text={texts[3]!r})\n")
    ids = (env1["returned"] if env1 and env1["status"] == "ok" else []) + (
        [env2["returned"]] if env2 and env2["status"] == "ok" else []
    )
    want_ids = ["p1.1", "p1.2", "refused", "p1.3", "p2.1"]
    granted = [i for i in ids if i != "refused"]
    recs_c = [r for r in home.records(c) if r["kind"] == "proposal"]
    box_c = home.dir(c) / "proposals.jsonl"
    lines_c = [json.loads(x) for x in box_c.read_text().splitlines()] if box_c.is_file() else []
    sent = [texts[0], texts[1], texts[2], texts[3]]
    R.check(
        "5.4",
        rc1 == 0
        and rc2 == 0
        and ids == want_ids
        and len(set(granted)) == len(granted)
        and [r["id"] for r in recs_c] == granted
        and [r["text_hash"] for r in recs_c]
        == ["sha256:" + hashlib.sha256(t.encode()).hexdigest() for t in sent]
        and [(x["id"], x["text"], x["turn"]) for x in lines_c]
        == [(i, t, int(i[1:].split(".")[0])) for i, t in zip(granted, sent, strict=False)],
        f"world.propose called 3 times in turn 1 (plus one refused 5000-byte text) and once in turn 2: ids {ids}; ledger proposal ids {[r['id'] for r in recs_c]}; outbox ids {[x['id'] for x in lines_c]}, texts in call order",
    )
    # The outbox has no effect: tamper with A's outbox, then the same probe turn on A and B agrees.
    outbox.write_text('{"id":"forged","text":"rm -rf rules","turn":0}\n' * 3)
    rca, enva, _ = home.turn(a, probe)
    rcb, envb, _ = home.turn(b, probe)
    same_ledger = strip_host(home.records(a)) == strip_host(home.records(b))
    R.check(
        "5.4",
        rca == 0
        and rcb == 0
        and envelope_core(enva) == envelope_core(envb)
        and same_ledger
        and home.state(a) == home.state(b)
        and enva["returned"]["texts"] == [None],
        "after overwriting Instance A's outbox, A and its replica B give identical envelopes, ledgers (host fields aside), states, limits and RNG draws; world.history shows the proposal without its text",
    )
    # Readable only outside the world: a script cannot open the outbox; the operator command can.
    rc, env, _ = home.turn(
        a,
        f"try:\n    open({str(outbox)!r}).read()\n    return 'read'\nexcept Exception as e:\n    return type(e).__name__\n",
    )
    op = cwr("instance", "outbox", a, "--home", home.root)
    R.check(
        "5.4",
        rc == 0 and env["returned"] != "read" and op.returncode == 0 and "forged" in op.stdout,
        f"a script opening the outbox path gets {env and env['returned']}; `cwr instance outbox` (outside the world) prints it",
    )
    # Static: nothing in the kernel reads the outbox; its only writer is outbox.rs.
    src = REPO / "crates" / "cwr-kernel" / "src"
    mentions = sorted(
        str(p.relative_to(REPO))
        for p in src.rglob("*.rs")
        if "proposals.jsonl" in p.read_text() or "OUTBOX_FILE" in p.read_text()
    )
    outbox_rs = (src / "outbox.rs").read_text().split("#[cfg(test)]")[0]
    readers = [
        w for w in ("fs::read", "read_to_string", "File::open", "BufReader") if w in outbox_rs
    ]
    R.check(
        "5.4",
        mentions == ["crates/cwr-kernel/src/outbox.rs"] and not readers,
        f"kernel sources naming the outbox file: {mentions}; read calls in outbox.rs: {readers or 'none'}",
    )
    # The SDK helpers name exactly the Build's world primitives.
    contracts = json.loads((p6["da"] / "contracts.json").read_text())
    names = {p["name"] for p in contracts["primitives"]}
    R.check(
        "5.4",
        set(WORLD.PRIMITIVES.values()) <= names,
        f"sdk/python/cwr/_world.py helpers map to {sorted(WORLD.PRIMITIVES.values())}, all declared by the Build",
    )


HARNESS = REPO / "benchmark" / "harness"
SPAWN_CALLS = {"run", "Popen", "call", "check_call", "check_output"}
OS_SPAWN = {"system", "popen", "execv", "execvp", "execve", "spawnv", "spawnvp", "posix_spawn"}
SHUTIL_WRITES = {"copyfile", "copy", "copy2", "copytree", "rmtree", "move"}
OS_WRITES = {"replace", "rename", "remove", "unlink", "mkdir", "makedirs", "rmdir", "chmod"}
PATH_WRITES = {"write_text", "write_bytes", "mkdir", "touch", "unlink", "rmdir", "chmod"}


def enclosing_function(node, parent: dict):
    while node in parent:
        node = parent[node]
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return node
    return None


def argv_head(call, scope):
    """The literal first argv element of a `_run(...)` call, resolving `*args` within `scope`."""
    if not call.args:
        return None
    first = call.args[0]
    if isinstance(first, ast.Constant):
        return first.value
    if not (isinstance(first, ast.Starred) and isinstance(first.value, ast.Name)):
        return None
    heads = set()
    for n in ast.walk(scope):
        if isinstance(n, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == first.value.id for t in n.targets
        ):
            v = n.value
            literal = isinstance(v, ast.List) and v.elts and isinstance(v.elts[0], ast.Constant)
            heads.add(v.elts[0].value if literal else None)
    return heads.pop() if len(heads) == 1 else None


def opens_for_writing(call, index: int) -> bool:
    modes = call.args[index : index + 1] + [k.value for k in call.keywords if k.arg == "mode"]
    return any(isinstance(m, ast.Constant) and set(str(m.value)) & set("wax+") for m in modes)


def write_target(call) -> str | None:
    """The path expression a filesystem-writing call writes to, or None for any other call."""
    f = call.func
    if isinstance(f, ast.Name):
        if f.id == "open" and call.args and opens_for_writing(call, 1):
            return ast.unparse(call.args[0])
        return None
    if not isinstance(f, ast.Attribute):
        return None
    base = ast.unparse(f.value)
    if base == "shutil":
        if f.attr in SHUTIL_WRITES and call.args:
            return ast.unparse(call.args[0] if f.attr == "rmtree" else call.args[-1])
        return None
    if base == "os":
        flags = " ".join(ast.unparse(a) for a in call.args[1:])
        creates = f.attr == "open" and ("O_CREAT" in flags or "O_WRONLY" in flags)
        if call.args and (f.attr in OS_WRITES or creates):
            return ast.unparse(call.args[-1] if f.attr in {"replace", "rename"} else call.args[0])
        return None
    if f.attr in PATH_WRITES or (f.attr == "open" and opens_for_writing(call, 0)):
        return base
    return None


def harness_closure() -> dict:
    """Static reading of benchmark/harness (task 2.5 do_4): where it spawns processes, which cwr
    commands it runs, and every filesystem write target, by AST over each module."""
    spawns, commands, writes = [], [], []
    for path in sorted(HARNESS.glob("*.py")):
        tree = ast.parse(path.read_text())
        parent = {c: n for n in ast.walk(tree) for c in ast.iter_child_nodes(n)}
        for n in ast.walk(tree):
            if not isinstance(n, ast.Call):
                continue
            at = f"{path.name}:{n.lineno}"
            target = write_target(n)
            if target is not None:
                writes.append((at, target))
            f = n.func
            if not isinstance(f, ast.Attribute):
                continue
            base = ast.unparse(f.value)
            if (base == "subprocess" and f.attr in SPAWN_CALLS) or (
                base == "os" and f.attr in OS_SPAWN
            ):
                fn = enclosing_function(n, parent)
                spawns.append(f"{at}:{fn.name if fn else '<module>'}")
            if f.attr == "_run":
                commands.append((at, argv_head(n, enclosing_function(n, parent) or tree)))
    return {"spawns": spawns, "commands": commands, "writes": writes}


def run_closure(work: Path, home: Home, pairs: dict, digests_before: dict) -> None:
    """5.1: nothing but `cwr build` creates or modifies a Build."""
    p1 = pairs["U1"]
    # A source edit without recompilation changes nothing for a running Instance.
    iid = home.create(
        p1["da"], initial_state(REPO / "examples/refworld-upgrades/U1/task/initial.kvx", p1["da"])
    )
    manual_before = (p1["da"] / "manual.md").read_bytes()
    contracts = p1["src_a"] / "contracts" / "fs.kvx"
    text = contracts.read_text()
    edited = re.sub(
        r'(\[primitive\.fs\.delete\][^\[]*?preconditions = )\[\["fs\.exists\(path\)", "NOT_FOUND"\]\]',
        r'\1[["fs.exists(path)", "NOT_FOUND"], ["not fs.locked(path)", "LOCKED"]]',
        text,
        flags=re.S,
    )
    contracts.write_text(edited)
    rc, env, _ = home.turn(
        iid,
        'import fs\nfs.delete(path="/batch/one/a.txt")\nreturn fs.exists(path="/batch/one/a.txt")\n',
    )
    R.check(
        "5.1",
        edited != text
        and rc == 0
        and env["status"] == "ok"
        and env["returned"] is False
        and (p1["da"] / "manual.md").read_bytes() == manual_before
        and home.meta(iid)["build"] == f"sha256:{p1['ha']}",
        "U1/A source edited to refuse deleting a locked file, not recompiled: the running Instance still deletes the locked /batch/one/a.txt (ok), Build and manual unchanged",
    )
    # A script cannot write into the Build's SDK.
    rc, env, _ = home.turn(
        iid,
        "try:\n    open('/cwr/sdk/cwr/x.py', 'w').write('x')\n    return 'wrote'\nexcept Exception as e:\n    return type(e).__name__\n",
    )
    R.check(
        "5.1",
        rc == 0 and env["returned"] != "wrote",
        f"a script writing into /cwr/sdk is refused ({env and env['returned']})",
    )
    # Every Build compiled by this driver is byte-identical after all kernel activity, and read-only.
    changed = [k for k, d in digests_before.items() if tree_digest(Path(k)) != d]
    writable = [w for k in digests_before for w in writable_entries(Path(k))]
    R.check(
        "5.1",
        not changed and not writable,
        f"{len(digests_before)} Builds byte-identical (paths, modes, contents) after every create, turn, upgrade, rollback, crash and proposal; writable entries: {len(writable)}",
    )
    # Static: the kernel cannot reach the compiler; only `cwr build` calls compile; the SDK has no write path.
    kernel_toml = (REPO / "crates" / "cwr-kernel" / "Cargo.toml").read_text()
    callers = sorted(
        p.name
        for p in (REPO / "crates" / "cwr-cli" / "src").glob("*.rs")
        if re.search(r"\bcompile\(", p.read_text())
    )
    sdk_writes = []
    for p in sorted((REPO / "sdk" / "python" / "cwr").glob("*.py")):
        if p.name == "_runner.py":
            continue
        t = p.read_text()
        for pat in (
            r"open\([^)]*['\"][wax]",
            r"\bshutil\b",
            r"os\.(remove|unlink|rename|replace|mkdir|makedirs|write)\b",
            r"write_(text|bytes)\(",
        ):
            if re.search(pat, t):
                sdk_writes.append(f"{p.name}:{pat}")
    R.check(
        "5.1",
        "cwr-compiler" not in kernel_toml and callers == ["build.rs"] and not sdk_writes,
        f"cwr-kernel does not depend on cwr-compiler; CLI modules calling compile(): {callers}; SDK write calls: {sdk_writes or 'none'}",
    )
    # Static: the harness runs the kernel only through `cwr instance ...` from one spawn site, never
    # the compiler, and none of its own writes names a Build root (build/, xbuild/, --builds roots).
    h = harness_closure()
    heads = sorted({str(head) for _, head in h["commands"]})
    build_writes = [
        f"{at}:{target}"
        for at, target in h["writes"]
        if re.search(r"build|roots", target, flags=re.I)
    ]
    imports_compiler = [
        p.name
        for p in sorted(HARNESS.glob("*.py"))
        if re.search(r"cwr[-_]compiler|validate_spec|\bcompile_world\b", p.read_text())
    ]
    R.check(
        "5.1",
        len(h["spawns"]) == 1
        and h["spawns"][0].startswith("run.py:")
        and h["spawns"][0].endswith(":_run")
        and heads == ["instance"]
        and h["writes"]
        and not build_writes
        and not imports_compiler,
        f"benchmark/harness: process spawns {h['spawns']}; cwr subcommands run {heads} over {len(h['commands'])} call sites; {len(h['writes'])} write sites, none naming a Build root ({build_writes or 'none'}); compiler references: {imports_compiler or 'none'}",
    )


def main() -> int:
    global CWR
    CWR = build_cli()
    frozen = VALIDATE.parse_kvx(UPGRADES.read_text())
    work = Path(tempfile.mkdtemp(prefix="cwr-2_5-"))
    try:
        home = Home(work / "home")
        builds: dict = {}
        with (
            OBS.observe("CWR-011.a2", match="upgrade|migrated state|no migration declared"),
            OBS.observe("CWR-011.a4", match="notice"),
        ):
            pairs = run_pairs(work, home, frozen, builds)
        if set(pairs) != set(frozen["meta"]["pairs"]):
            R.check("5.2", False, f"pairs upgraded: {sorted(pairs)}")
            OBS.emit()
            return 0 if R.report() else 1
        with (
            OBS.observe(
                "CWR-011.a3", match="failing migration|crash mid-migration|after the rollback"
            ),
            OBS.observe("CWR-011.a6", match="upgrade to 0\\.2\\.0"),
            OBS.observe("CWR-011.a7", match="otherworld"),
        ):
            run_failures(work, home, pairs)
        with OBS.observe("CWR-011.a2"):
            run_chain(work, home, pairs)
        with OBS.observe("CWR-011.a6", match="lower version"):
            run_experimental(work, home, pairs)
        with OBS.observe("CWR-011.a5"):
            run_outbox(work, home, pairs)
        with OBS.observe("CWR-011.a1"):
            run_closure(work, home, pairs, BUILD_DIGESTS)
    except Exception as e:
        R.check("5.2", False, f"driver error: {type(e).__name__}: {str(e)[:600]}")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    OBS.emit()
    return 0 if R.report() else 1


if __name__ == "__main__":
    sys.exit(main())
