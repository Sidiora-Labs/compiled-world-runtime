# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Authoring model of refworld for task-suite authoring (task 4.2). NOT the kernel, NOT evidence.

This module executes reference-solution turn scripts against a pure-Python model of the refworld
primitive catalogue in spec/cwr/reference-world.md section 2 and the turn, clock, ledger and
envelope rules in spec/cwr/kernel.md and spec/cwr/model-surface.md. It exists only so that the
suite author can check, before any kernel or Build exists, that every reference solution satisfies
its predicate, that primitive counts fall in their bins, that rule-reuse tasks have the required
shape and that predicates are false on the untouched initial state.

What it is not: it has no sandbox (scripts are exec'd in-process with a restricted import hook), it
does not compute blake3 parameter hashes (``params_hash`` carries a labelled sha256 instead), its
search hits, receipts and job results are invented content, and its stub rate limits use sliding
windows. CWR-015 requires the reference solutions to be re-run in replay mode on the real kernel;
counts recorded from this model are labelled ``count_source = "authoring-model"`` in suite.kvx.
"""

from __future__ import annotations

import copy
import hashlib
import json
import keyword
import random
import textwrap

TICK_PER_PRIMITIVE = 1
TICK_PER_TURN = 10
LIMITS = {
    "cpu_ms": 5000,
    "wall_ms": 30000,
    "memory_mb": 256,
    "primitive_calls": 500,
    "adapter_calls": 50,
}
PUBLISH_WINDOW, PUBLISH_MAX = 100, 3
SEARCH_WINDOW, SEARCH_MAX = 50, 10
MODULES = ("fs", "kv", "jobs", "search", "publish", "world")


def _h(label: str, value) -> str:
    data = json.dumps(value, sort_keys=True, default=repr).encode()
    return f"{label}:" + hashlib.sha256(data).hexdigest()


class WorldError(Exception):
    """Typed failure raised into the script; carries the envelope fields of kernel.md section 5."""

    def __init__(
        self, code, primitive, precondition_index=None, retry_after=None, detail="", limit=None
    ):
        super().__init__(f"{code} on {primitive}: {detail}")
        self.code = code
        self.primitive = primitive
        self.precondition_index = precondition_index
        self.retry_after = retry_after
        self.detail = detail
        self.limit = limit


class _ExternalError(Exception):
    """A simulated adapter failure raised inside a primitive body; call() records it."""

    def __init__(self, code, retry_after=None, detail=""):
        super().__init__(code)
        self.code, self.retry_after, self.detail = code, retry_after, detail


class World:
    def __init__(
        self,
        initial: dict,
        seed: int = 1,
        instance: str = "sim-instance",
        arm: str = "authoring",
        model: str = "reference-solution",
    ):
        fs = initial.get("fs", {})
        self.files = {
            p: c.encode() if isinstance(c, str) else bytes(c)
            for p, c in (fs.get("files") or {}).items()
        }
        self.locks = set(fs.get("locks") or [])
        self.kv = {
            ns: {k: v.encode() if isinstance(v, str) else bytes(v) for k, v in values.items()}
            for ns, values in ((initial.get("kv") or {}).get("namespaces") or {}).items()
        }
        self.jobs = copy.deepcopy((initial.get("jobs") or {}).get("jobs") or {})
        self.seed = seed
        self.rng = random.Random(seed)
        self.time = 0
        self.turn = 0
        self.instance = instance
        self.build = "sha256:" + "0" * 64
        self.idem: dict = {}
        self.posts: dict = {}
        self.publish_accepts: list = []
        self.search_accepts: list = []
        self.next_job = 1
        self.ledger: list = []
        self.proposals: list = []
        # (primitive, params) of every well-formed call, in order; build_suite re-evaluates them on
        # the starting state to show that each precondition rule of a task can fire.
        self.calls_made: list = []
        self._append(
            {
                "kind": "header",
                "primitive": None,
                "params_hash": None,
                "preconditions": [],
                "outcome": None,
                "seed": seed,
                "tail_size": 20,
                "arm": arm,
                "model": model,
                "created_from": None,
            }
        )

    # ---------------------------------------------------------------- ledger
    def _append(self, fields: dict) -> dict:
        record = {
            "seq": len(self.ledger),
            "instance": self.instance,
            "build": self.build,
            "turn": self.turn,
            "world_time": self.time,
            "kind": None,
            "primitive": None,
            "params_hash": None,
            "preconditions": [],
            "outcome": None,
            "duration_us": 0,
            "host_ts": "1970-01-01T00:00:00Z",
            "experimental": None,
        }
        record.update(fields)
        self.ledger.append(record)
        return record

    # ---------------------------------------------------------------- read-only helpers
    def _job_status(self, job_id, at=None):
        job = self.jobs[job_id]
        t = self.time if at is None else at
        if job.get("cancelled_at") is not None and t >= job["cancelled_at"]:
            return "failed"
        fail_at = job.get("fail_at")
        if fail_at is not None and fail_at <= job["submitted_at"] + job["n"] and t >= fail_at:
            return "failed"
        start = job["submitted_at"]
        if t < start + 1:
            return "queued"
        if t < start + job["n"]:
            return "running"
        return "done"

    def _active(self):
        return sum(1 for j in self.jobs if self._job_status(j) in ("queued", "running"))

    def _used(self, ns):
        return sum(len(v) for v in self.kv.get(ns, {}).values())

    # ---------------------------------------------------------------- catalogue
    def catalogue(self):
        def exists(p):
            return p in self.files

        def locked(p):
            return p in self.locks

        def known(j):
            return j in self.jobs

        cat = {}

        def prim(name, params, side, returns, pre, run, kind="primitive"):
            cat[name] = dict(params=params, side=side, returns=returns, pre=pre, run=run, kind=kind)

        # fs
        prim("fs.exists", {"path": str}, "read_only", "bool", [], lambda a: exists(a["path"]))
        prim("fs.locked", {"path": str}, "read_only", "bool", [], lambda a: locked(a["path"]))
        prim(
            "fs.size",
            {"path": str},
            "read_only",
            "int",
            [("fs.exists(path)", "NOT_FOUND", lambda a: exists(a["path"]))],
            lambda a: len(self.files[a["path"]]),
        )
        prim(
            "fs.list",
            {"prefix": str},
            "read_only",
            "list[Path]",
            [],
            lambda a: sorted(p for p in self.files if p.startswith(a["prefix"])),
        )
        prim(
            "fs.read",
            {"path": str},
            "read_only",
            "bytes",
            [("fs.exists(path)", "NOT_FOUND", lambda a: exists(a["path"]))],
            lambda a: self.files[a["path"]],
        )

        def fs_write(a):
            self.files[a["path"]] = a["content"]

        prim(
            "fs.write",
            {"path": str, "content": bytes},
            "mutating",
            "None",
            [
                ("not fs.locked(path)", "LOCKED", lambda a: not locked(a["path"])),
                ("len(content) <= 1048576", "LIMIT", lambda a: len(a["content"]) <= 1048576),
            ],
            fs_write,
        )

        def fs_delete(a):
            del self.files[a["path"]]

        prim(
            "fs.delete",
            {"path": str},
            "mutating",
            "None",
            [
                ("fs.exists(path)", "NOT_FOUND", lambda a: exists(a["path"])),
                ("not fs.locked(path)", "LOCKED", lambda a: not locked(a["path"])),
            ],
            fs_delete,
        )
        prim(
            "fs.lock",
            {"path": str},
            "mutating",
            "None",
            [
                ("fs.exists(path)", "NOT_FOUND", lambda a: exists(a["path"])),
                ("not fs.locked(path)", "CONFLICT", lambda a: not locked(a["path"])),
            ],
            lambda a: self.locks.add(a["path"]) or None,
        )
        prim(
            "fs.unlock",
            {"path": str},
            "mutating",
            "None",
            [("fs.locked(path)", "NOT_FOUND", lambda a: locked(a["path"]))],
            lambda a: self.locks.discard(a["path"]) or None,
        )

        def fs_move(a):
            self.files[a["dst"]] = self.files.pop(a["src"])

        prim(
            "fs.move",
            {"src": str, "dst": str},
            "mutating",
            "None",
            [
                ("fs.exists(src)", "NOT_FOUND", lambda a: exists(a["src"])),
                ("not fs.locked(src)", "LOCKED", lambda a: not locked(a["src"])),
                ("not fs.exists(dst)", "CONFLICT", lambda a: not exists(a["dst"])),
            ],
            fs_move,
        )

        # kv
        def has(a):
            return a["key"] in self.kv.get(a["ns"], {})

        prim("kv.has", {"ns": str, "key": str}, "read_only", "bool", [], has)
        prim(
            "kv.get",
            {"ns": str, "key": str},
            "read_only",
            "bytes",
            [("kv.has(ns, key)", "NOT_FOUND", has)],
            lambda a: self.kv[a["ns"]][a["key"]],
        )
        prim(
            "kv.keys",
            {"ns": str},
            "read_only",
            "list[str]",
            [],
            lambda a: sorted(self.kv.get(a["ns"], {})),
        )
        prim("kv.used", {"ns": str}, "read_only", "int", [], lambda a: self._used(a["ns"]))

        def kv_set(a):
            self.kv.setdefault(a["ns"], {})[a["key"]] = bytes(a["value"])

        prim(
            "kv.set",
            {"ns": str, "key": str, "value": bytes},
            "mutating",
            "None",
            [
                ("len(value) <= 1024", "INVALID", lambda a: len(a["value"]) <= 1024),
                (
                    "kv.used(ns) + len(value) <= 65536",
                    "LIMIT",
                    lambda a: self._used(a["ns"]) + len(a["value"]) <= 65536,
                ),
            ],
            kv_set,
        )

        def kv_del(a):
            del self.kv[a["ns"]][a["key"]]
            if not self.kv[a["ns"]]:  # lib/kv.py never stores an empty namespace
                del self.kv[a["ns"]]

        prim(
            "kv.del",
            {"ns": str, "key": str},
            "mutating",
            "None",
            [("kv.has(ns, key)", "NOT_FOUND", has)],
            kv_del,
        )

        # jobs
        prim("jobs.known", {"id": str}, "read_only", "bool", [], lambda a: known(a["id"]))
        prim(
            "jobs.status",
            {"id": str},
            "read_only",
            "str",
            [("jobs.known(id)", "NOT_FOUND", lambda a: known(a["id"]))],
            lambda a: self._job_status(a["id"]),
        )
        prim("jobs.active", {}, "read_only", "int", [], lambda a: self._active())

        def submit(a):
            job_id = f"j{self.next_job}"
            self.next_job += 1
            self.jobs[job_id] = {"kind": a["kind"], "n": a["n"], "submitted_at": self.time}
            return job_id

        prim(
            "jobs.submit",
            {"kind": str, "n": int},
            "mutating",
            "str",
            [
                ("n >= 1 and n <= 1000", "INVALID", lambda a: 1 <= a["n"] <= 1000),
                ("jobs.active() < 8", "LIMIT", lambda a: self._active() < 8),
            ],
            submit,
        )

        def result(a):
            job = self.jobs[a["id"]]
            return f"{job['kind']}:{a['id']}:{job['n']}"

        prim(
            "jobs.result",
            {"id": str},
            "read_only",
            "str",
            [
                ("jobs.known(id)", "NOT_FOUND", lambda a: known(a["id"])),
                (
                    'jobs.status(id) == "done"',
                    "CONFLICT",
                    lambda a: self._job_status(a["id"]) == "done",
                ),
            ],
            result,
        )

        def cancel(a):
            self.jobs[a["id"]]["cancelled_at"] = self.time

        prim(
            "jobs.cancel",
            {"id": str},
            "mutating",
            "None",
            [
                ("jobs.known(id)", "NOT_FOUND", lambda a: known(a["id"])),
                (
                    'jobs.status(id) in ["queued", "running"]',
                    "CONFLICT",
                    lambda a: self._job_status(a["id"]) in ("queued", "running"),
                ),
            ],
            cancel,
        )

        # search (adapter)
        def query(a):
            recent = [t for t in self.search_accepts if t > self.time - SEARCH_WINDOW]
            if len(recent) >= SEARCH_MAX:
                raise _ExternalError(
                    "RATE_LIMITED", retry_after=recent[0] + SEARCH_WINDOW - self.time
                )
            self.search_accepts.append(self.time)
            tag = hashlib.sha256(f"{self.seed}:{a['q']}".encode()).hexdigest()[:6]
            count = min(a["limit"], 3 + int(tag, 16) % 5)
            return [
                {
                    "title": f"{a['q'].title()} - result {i + 1} [{tag}]",
                    "snippet": f"Snippet {i + 1} about {a['q']}.",
                    "url": f"stub://search/{tag}/{i}",
                }
                for i in range(max(count, 0))
            ]

        prim(
            "search.query",
            {"q": str, "limit": int},
            "external",
            "list[SearchHit]",
            [
                ("len(q) > 0", "INVALID", lambda a: len(a["q"]) > 0),
                ("limit <= 50", "LIMIT", lambda a: a["limit"] <= 50),
            ],
            query,
            kind="adapter",
        )

        # publish (adapter, key idempotency at the gateway, rate limit at the stub)
        def post(a):
            payload = _h("sim-sha256", [a["title"], a["body"]])
            stored = self.idem.setdefault("publish.post", {}).get(a["key"])
            if stored is not None:
                if stored["payload_hash"] == payload:
                    return stored["receipt"]
                raise _ExternalError("DUPLICATE_KEY", detail=json.dumps(stored["receipt"]))
            recent = [t for t in self.publish_accepts if t > self.time - PUBLISH_WINDOW]
            if len(recent) >= PUBLISH_MAX:
                raise _ExternalError(
                    "RATE_LIMITED", retry_after=recent[0] + PUBLISH_WINDOW - self.time
                )
            self.publish_accepts.append(self.time)
            post_id = "p_" + hashlib.sha256(f"{self.seed}:{a['key']}".encode()).hexdigest()[:6]
            receipt = {"id": post_id, "key": a["key"]}
            self.posts[post_id] = {"status": "published"}
            self.idem["publish.post"][a["key"]] = {
                "receipt": receipt,
                "payload_hash": payload,
                "seq": len(self.ledger),
            }
            return receipt

        prim(
            "publish.post",
            {"key": str, "title": str, "body": str},
            "external",
            "PostReceipt",
            [("len(title) <= 200", "INVALID", lambda a: len(a["title"]) <= 200)],
            post,
            kind="adapter",
        )

        def pstatus(a):
            if a["post_id"] not in self.posts:
                raise _ExternalError("NOT_FOUND")
            return {"id": a["post_id"], "status": self.posts[a["post_id"]]["status"]}

        prim(
            "publish.status",
            {"post_id": str},
            "external",
            "PostStatus",
            [],
            pstatus,
            kind="adapter",
        )

        def pdelete(a):
            if a["post_id"] not in self.posts or self.posts[a["post_id"]]["status"] == "deleted":
                raise _ExternalError("NOT_FOUND")
            self.posts[a["post_id"]]["status"] = "deleted"

        prim("publish.delete", {"post_id": str}, "external", "None", [], pdelete, kind="adapter")

        # world
        prim("world.now", {}, "read_only", "int", [], lambda a: self.time)
        prim("world.version", {}, "read_only", "str", [], lambda a: "0.2.0")
        prim("world.seed", {}, "read_only", "int", [], lambda a: self.seed)
        prim("world.limits", {}, "read_only", "Limits", [], lambda a: dict(self._remaining()))
        prim("world.random", {}, "mutating", "int", [], lambda a: self.rng.getrandbits(32))
        prim(
            "world.history",
            {"n": int},
            "read_only",
            "list[LedgerRecord]",
            [("n >= 0 and n <= 500", "INVALID", lambda a: 0 <= a["n"] <= 500)],
            lambda a: [
                {k: v for k, v in r.items() if k not in ("host_ts", "duration_us")}
                for r in (self.ledger[-a["n"] :] if a["n"] else [])
            ],
        )

        def propose(a):
            self.proposals.append(a["text"])
            return f"prop-{len(self.proposals)}"

        prim(
            "world.propose",
            {"text": str},
            "mutating",
            "ProposalId",
            [("len(text) <= 4096", "INVALID", lambda a: len(a["text"]) <= 4096)],
            propose,
        )
        return cat

    def _remaining(self):
        rem = {
            "primitive_calls": LIMITS["primitive_calls"] - self._turn_calls,
            "adapter_calls": LIMITS["adapter_calls"] - self._turn_adapter,
        }
        return {**LIMITS, "remaining": rem}

    # ---------------------------------------------------------------- one primitive call
    def call(self, name, args, kwargs):
        spec = self._cat[name]
        names = list(spec["params"])
        if len(args) > len(names):
            return self._refuse(name, spec, "INVALID", None, "too many arguments", {})
        params = dict(zip(names, args, strict=False))
        for key, value in kwargs.items():
            if key not in spec["params"] or key in params:
                return self._refuse(name, spec, "INVALID", None, f"bad argument {key}", params)
            params[key] = value
        if set(params) != set(names):
            return self._refuse(name, spec, "INVALID", None, "missing argument", params)
        for key, typ in spec["params"].items():
            if not isinstance(params[key], typ) or (typ is int and isinstance(params[key], bool)):
                return self._refuse(
                    name, spec, "INVALID", None, f"{key} must be {typ.__name__}", params
                )
        self.calls_made.append((name, dict(params)))
        self._turn_calls += 1
        if self._turn_calls > LIMITS["primitive_calls"]:
            return self._refuse(
                name, spec, "LIMIT", None, "primitive_calls", params, limit="primitive_calls"
            )
        if spec["side"] == "external":
            self._turn_adapter += 1
            if self._turn_adapter > LIMITS["adapter_calls"]:
                return self._refuse(
                    name, spec, "LIMIT", None, "adapter_calls", params, limit="adapter_calls"
                )
        evaluated = []
        for index, (expr, code, check) in enumerate(spec["pre"], 1):
            passed = bool(check(params))
            evaluated.append(
                {"index": index, "expr_hash": _h("sim-sha256", expr), "passed": passed}
            )
            if not passed:
                return self._refuse(
                    name, spec, code, index, f"{expr} was false", params, evaluated=evaluated
                )
        try:
            value = spec["run"](params)
        except _ExternalError as failure:
            self.time += TICK_PER_PRIMITIVE
            self._record(name, spec, params, evaluated, "error", failure.code)
            raise WorldError(
                failure.code, name, None, failure.retry_after, failure.detail
            ) from failure
        self.time += TICK_PER_PRIMITIVE
        self._record(name, spec, params, evaluated, "ok", None)
        if spec["side"] in ("mutating", "external"):
            self._turn_mutated = True
        return copy.deepcopy(value)

    def _record(self, name, spec, params, evaluated, status, code):
        gateway = {}
        if spec["kind"] == "adapter":
            # The kernel gateway's request hash (common.request_hash); adapter params are JSON.
            text = json.dumps(
                {"adapter": name, "params": params},
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            )
            gateway["gateway"] = {
                "request_hash": "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "via": "service",
            }
        self._append(
            {
                **gateway,
                "kind": spec["kind"],
                "primitive": name,
                "params_hash": _h(
                    "sim-sha256",
                    {
                        k: v.decode("latin-1") if isinstance(v, bytes) else v
                        for k, v in params.items()
                    },
                ),
                "preconditions": evaluated,
                "outcome": {
                    "status": status,
                    "code": code,
                    "shape": spec["returns"],
                    "side_effect": spec["side"],
                },
            }
        )
        self._trace.append(
            {"primitive": name, "status": status, "code": code, "world_time": self.time}
        )

    def _refuse(self, name, spec, code, index, detail, params, evaluated=None, limit=None):
        self._record(name, spec, params, evaluated or [], "error", code)
        raise WorldError(code, name, index, None, detail, limit)

    # ---------------------------------------------------------------- one turn
    def run_turn(self, script: str) -> dict:
        self.turn += 1
        self._cat = self.catalogue()
        self._trace, self._turn_calls, self._turn_adapter = [], 0, 0
        self._turn_mutated = False
        modules = {m: _Module(self, m) for m in MODULES}

        def only_sdk(name, globals_=None, locals_=None, fromlist=(), level=0):
            if name in modules and level == 0:
                return modules[name]
            raise ImportError(f"{name} is not part of the Build SDK")

        import builtins

        safe = {
            k: getattr(builtins, k)
            for k in dir(builtins)
            if k
            not in (
                "open",
                "exec",
                "eval",
                "compile",
                "input",
                "breakpoint",
                "exit",
                "quit",
                "help",
                "__import__",
                "globals",
                "locals",
                "vars",
            )
        }
        safe["__import__"] = only_sdk
        body = (
            "def __turn__():\n"
            + textwrap.indent(textwrap.dedent(script).strip("\n") or "pass", "    ")
            + "\n"
        )
        namespace = {"__builtins__": safe}
        error, returned = None, None
        try:
            exec(compile(body, f"<turn {self.turn}>", "exec"), namespace)
            returned = namespace["__turn__"]()
            json.dumps(returned)
        except WorldError as failure:
            error = {
                "code": failure.code,
                "primitive": failure.primitive,
                "precondition_index": failure.precondition_index,
                "retry_after": failure.retry_after,
                "detail": failure.detail,
            }
        except Exception as failure:  # script bug or unserialisable return: INVALID
            error = {
                "code": "INVALID",
                "primitive": "turn.script",
                "precondition_index": None,
                "retry_after": None,
                "detail": f"{type(failure).__name__}: {failure}",
            }
            returned = None
        status = "ok" if error is None else ("partial" if self._turn_mutated else "error")
        self.time += TICK_PER_TURN
        self._append(
            {
                "kind": "turn",
                "status": status,
                "returned_hash": _h("sim-sha256", returned),
                "resources": {
                    "primitive_calls": self._turn_calls,
                    "adapter_calls": self._turn_adapter,
                },
                "calls": len(self._trace),
                "outcome": {
                    "status": status,
                    "code": error and error["code"],
                    "shape": None,
                    "side_effect": None,
                },
            }
        )
        return {
            "status": status,
            "returned": returned,
            "error": error,
            "trace": list(self._trace),
            "world": {
                "version": "0.2.0",
                "build": self.build,
                "time": self.time,
                "turn": self.turn,
            },
            "resources": {
                "cpu_ms": 0,
                "wall_ms": 0,
                "primitive_calls": self._turn_calls,
                "adapter_calls": self._turn_adapter,
                "remaining": self._remaining()["remaining"],
            },
        }

    # ---------------------------------------------------------------- final state view
    def state_view(self) -> dict:
        def enc(data: bytes):
            try:
                return data.decode("utf-8")
            except UnicodeDecodeError:
                import base64

                return {"base64": base64.b64encode(data).decode()}

        jobs = {}
        for job_id, job in sorted(self.jobs.items()):
            view = {k: v for k, v in job.items() if k != "result"}
            view["status"] = self._job_status(job_id)
            if view["status"] == "done":
                view["result"] = f"{job['kind']}:{job_id}:{job['n']}"
            jobs[job_id] = view
        return {
            "fs": {
                "files": {p: enc(c) for p, c in sorted(self.files.items())},
                "locks": sorted(self.locks),
            },
            "kv": {
                "namespaces": {
                    ns: {k: enc(v) for k, v in sorted(values.items())}
                    for ns, values in sorted(self.kv.items())
                }
            },
            "jobs": {"jobs": jobs},
            "world": {"time": self.time, "turn": self.turn},
            "gateway": {"idempotency": copy.deepcopy(self.idem)},
        }


class _Module:
    def __init__(self, world, name):
        object.__setattr__(self, "_world", world)
        object.__setattr__(self, "_name", name)

    def __getattr__(self, attr):
        # The generated SDK names a primitive whose name is a Python keyword with a trailing
        # underscore (kv.del is kv.del_; crates/cwr-compiler/src/generate/sdk.rs python_name) and
        # has no attribute under the bare keyword, so getattr(kv, "del") fails as it does there.
        if keyword.iskeyword(attr):
            raise AttributeError(f"module {self._name!r} has no attribute {attr!r}")
        if attr.endswith("_") and keyword.iskeyword(attr[:-1]):
            attr = attr[:-1]
        full = f"{self._name}.{attr}"
        if full not in self._world._cat:
            raise WorldError("INVALID", full, None, None, "not enumerated by the Build")
        return lambda *a, **k: self._world.call(full, a, k)


def run_solution(initial: dict, turns_factory, seed: int = 1, max_turns: int = 200):
    """Drive a reference solution generator; return (world, envelopes)."""
    world = World(initial, seed=seed)
    gen = turns_factory()
    envelopes = []
    try:
        script = next(gen)
        while True:
            if len(envelopes) >= max_turns:
                raise RuntimeError("reference solution exceeded the turn limit")
            env = world.run_turn(script)
            envelopes.append(env)
            script = gen.send(env)
    except StopIteration:
        pass
    return world, envelopes
