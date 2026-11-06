# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Long bin: reference solutions of 26 to 120 primitives (t041 to t060).

t041 to t054 are rule-reuse tasks: the reused rule's primitive is needed in the first quarter of
the reference solution, not at all in the middle half, and again in the last quarter. Their
instructions fix the phase order, and their predicates check it from the ledger with
common.brackets(reused primitive, a primitive used only in the middle phase).
"""

import common as c
from model import Task, ok_times, script, wait_until


# t041 jobs.result[2]:CONFLICT ------------------------------------------------------------------
def t041_success(state, ledger):
    notes = {
        f"/notes/n{i:02d}.txt": (f"note {i:02d}: follow up on item {i}\n").upper()
        for i in range(1, 13)
    }

    def stored(key, n):
        value = c.kv_get(state, "results", key)
        return value is not None and any(
            state["jobs"]["jobs"][j]["n"] == n and c.job_result(state, j) == value
            for j in c.jobs_of_kind(state, "index")
        )

    return (
        stored("index-1", 6)
        and stored("index-2", 9)
        and all(c.file_text(state, p) == t for p, t in notes.items())
        and c.brackets(ledger, "jobs.result", "fs.write")
    )


def t041_turns():
    env = yield script("""
        import jobs
        return jobs.submit(kind="index", n=6)
    """)
    first = env["returned"]
    env = yield script(
        """
        import fs, jobs, kv
        kv.set(ns="results", key="index-1", value=jobs.result(id=FIRST).encode())
        for path in fs.list(prefix="/notes/"):
            fs.write(path=path, content=fs.read(path=path).upper())
        return jobs.submit(kind="index", n=9)
    """,
        FIRST=first,
    )
    second = env["returned"]
    env = yield from wait_until(env, max(ok_times(env, "jobs.submit")) + 9)
    env = yield script(
        """
        import jobs, kv
        kv.set(ns="results", key="index-2", value=jobs.result(id=SECOND).encode())
    """,
        SECOND=second,
    )


# t042 fs.delete[2]:LOCKED ----------------------------------------------------------------------
def t042_success(state, ledger):
    for i in range(1, 13):
        raw = "".join(f"row {i} col {k}   \n" for k in range(3))
        clean = "".join(f"row {i} col {k}\n" for k in range(3))
        if c.file_text(state, f"/reports/raw/r{i:02d}.txt") != raw:
            return False
        if c.file_text(state, f"/reports/clean/r{i:02d}.txt") != clean:
            return False
    return c.files_under(state, "/scratch/") == ["/scratch/keep.txt"] and c.brackets(
        ledger, "fs.delete", "fs.write"
    )


def t042_turns():
    yield script("""
        import fs
        fs.unlock(path="/scratch/session.tmp")
        fs.delete(path="/scratch/session.tmp")
        for path in fs.list(prefix="/reports/raw/"):
            text = fs.read(path=path).decode()
            clean = "".join(line.rstrip(" ") + "\\n" for line in text.splitlines())
            fs.write(path="/reports/clean/" + path[len("/reports/raw/"):], content=clean.encode())
        for name in ["build.tmp", "cache.tmp"]:
            fs.unlock(path="/scratch/" + name)
            fs.delete(path="/scratch/" + name)
    """)


# t043 fs.move[3]:CONFLICT ----------------------------------------------------------------------
def t043_success(state, ledger):
    for q in ["q1", "q2", "q3"]:
        if c.file_text(state, f"/archive/{q}.txt") != f"{q} figures, final\n":
            return False
    for i in range(1, 13):
        name = f"d{i:02d}.txt"
        if c.file_text(state, "/summary/" + name) != f"# {name}\nmeasurement {(i * 3)}\n":
            return False
    return c.files_under(state, "/inbox/") == [] and c.brackets(ledger, "fs.move", "fs.write")


def t043_turns():
    yield script("""
        import fs
        fs.delete(path="/archive/q1.txt")
        fs.move(src="/inbox/q1.txt", dst="/archive/q1.txt")
        for path in fs.list(prefix="/data/"):
            name = path[len("/data/"):]
            fs.write(path="/summary/" + name, content=("# " + name + "\\n").encode()
                     + fs.read(path=path))
        fs.delete(path="/archive/q2.txt")
        fs.move(src="/inbox/q2.txt", dst="/archive/q2.txt")
        fs.move(src="/inbox/q3.txt", dst="/archive/q3.txt")
    """)


# t044 kv.set[1]:INVALID ------------------------------------------------------------------------
def t044_success(state, ledger):
    def joined(name):
        parts, i = [], 0
        while c.kv_has(state, "chunks", f"{name}.{i}"):
            parts.append(c.kv_get(state, "chunks", f"{name}.{i}"))
            i += 1
        return "".join(parts)

    a = "".join(f"alpha record {i:04d}\n" for i in range(111))
    b = "".join(f"bravo record {i:04d}\n" for i in range(144))
    docs_ok = all(
        c.file_text(state, f"/docs/doc{i:02d}.txt") == "Document %d About The World\n".lower() % i
        for i in range(1, 13)
    )
    return (
        joined("a.txt") == a
        and joined("b.txt") == b
        and docs_ok
        and c.brackets(ledger, "kv.set", "fs.write")
    )


def t044_turns():
    yield script("""
        import fs, kv
        def store(path):
            text = fs.read(path=path).decode()
            name = path[len("/big/"):]
            for i in range(0, len(text), 1024):
                kv.set(ns="chunks", key=name + "." + str(i // 1024), value=text[i:i + 1024].encode())
        store("/big/a.txt")
        for path in fs.list(prefix="/docs/"):
            fs.write(path=path, content=fs.read(path=path).lower())
        store("/big/b.txt")
    """)


# t045 kv.set[2]:LIMIT --------------------------------------------------------------------------
def t045_success(state, ledger):
    for i in (1, 2, 3):
        if c.kv_get(state, "cache", f"e{i}") != (f"e{i}") * 500:
            return False
    keys = c.kv_keys(state, "cache")
    letters = all(
        c.file_text(state, f"/letters/l{i:02d}.txt") == f"Dear team,\nletter {i} body\n"
        for i in range(1, 13)
    )
    return (
        c.kv_get(state, "cache", "pinned") == "p" * 800
        and all(k in ("e1", "e2", "e3", "pinned") or k.startswith("stale-") for k in keys)
        and letters
        and c.brackets(ledger, "kv.set", "fs.write")
    )


def t045_turns():
    yield script("""
        import fs, kv
        kv_del = kv.del_
        kv_del(ns="cache", key="stale-00")
        kv.set(ns="cache", key="e1", value=fs.read(path="/in/e1.txt"))
        for path in fs.list(prefix="/letters/"):
            fs.write(path=path, content=b"Dear team,\\n" + fs.read(path=path))
        e2 = fs.read(path="/in/e2.txt").decode()
        e3 = fs.read(path="/in/e3.txt").decode()
        kv_del(ns="cache", key="stale-01")
        kv.set(ns="cache", key="e2", value=e2.encode())
        kv_del(ns="cache", key="stale-02")
        kv.set(ns="cache", key="e3", value=e3.encode())
    """)


# t046 publish.post[rate]:RATE_LIMITED ----------------------------------------------------------
def t046_success(state, ledger):
    keys = ["e1", "e2", "e3", "e4", "l1", "l2", "l3", "l4"]
    backups = all(
        c.file_text(state, f"/pages/p{i:02d}.txt.bak") == f"page {i}\n" for i in range(1, 11)
    )
    return (
        all(c.posted_ok(state, ledger, k) for k in keys)
        and backups
        and c.rate_limited_beyond_first(ledger, "publish.post") == 0
        and c.brackets(ledger, "publish.post", "fs.write")
    )


def t046_turns():
    env = yield script("""
        import fs, publish
        texts = {p: fs.read(path=p).decode() for p in fs.list(prefix="/early/")}
        for p in sorted(texts)[:3]:
            key = p[len("/early/"):-len(".txt")]
            publish.post(key=key, title=texts[p].splitlines()[0], body=texts[p])
        return {p: texts[p] for p in sorted(texts)[3:]}
    """)
    rest, last = env["returned"], max(ok_times(env, "publish.post"))
    env = yield from wait_until(env, last + 101)
    env = yield script(
        """
        import fs, publish
        for p, text in sorted(REST.items()):
            publish.post(key=p[len("/early/"):-len(".txt")], title=text.splitlines()[0], body=text)
        for path in fs.list(prefix="/pages/"):
            fs.write(path=path + ".bak", content=fs.read(path=path))
        return {p: fs.read(path=p).decode() for p in ["/late/l1.txt", "/late/l2.txt",
                                                      "/late/l3.txt", "/late/l4.txt"]}
    """,
        REST=rest,
    )
    late, last = env["returned"], max(ok_times(env, "publish.post"))
    for group in (sorted(late)[:3], sorted(late)[3:]):
        env = yield from wait_until(env, last + 101)
        env = yield script(
            """
            import publish
            for p, text in sorted(GROUP.items()):
                publish.post(key=p[len("/late/"):-len(".txt")], title=text.splitlines()[0],
                             body=text)
        """,
            GROUP={p: late[p] for p in group},
        )
        last = max(ok_times(env, "publish.post"))


# t047 publish.post[key]:DUPLICATE_KEY ----------------------------------------------------------
def t047_success(state, ledger):
    by_id = {c.receipt_id(state, k): k for k in c.idempotency_keys(state)}
    for doc in ["plan", "budget"]:
        v1 = c.kv_get(state, "receipts", doc + "-v1")
        v2 = c.kv_get(state, "receipts", doc + "-v2")
        if v1 is None or v1 != c.receipt_id(state, doc) or not c.posted_ok(state, ledger, doc):
            return False
        if v2 not in by_id or by_id[v2] == doc or not c.posted_ok(state, ledger, by_id[v2]):
            return False
    copies = all(
        c.file_text(state, f"/minutes-archive/m{i:02d}.txt") == f"minutes {i}\n"
        for i in range(1, 11)
    )
    return copies and c.brackets(ledger, "publish.post", "fs.write")


def t047_turns():
    env = yield script("""
        import fs, kv, publish
        for v, key in [("v1", "plan"), ("v2", "plan-v2")]:
            body = fs.read(path="/drafts/plan-" + v + ".txt").decode()
            receipt = publish.post(key=key, title="Plan", body=body)
            kv.set(ns="receipts", key="plan-" + v, value=receipt["id"].encode())
        for path in fs.list(prefix="/minutes/"):
            fs.write(path="/minutes-archive/" + path[len("/minutes/"):], content=fs.read(path=path))
        return [fs.read(path="/drafts/budget-" + v + ".txt").decode() for v in ["v1", "v2"]]
    """)
    budget, last = env["returned"], max(ok_times(env, "publish.post"))
    env = yield from wait_until(env, last + 101)
    env = yield script(
        """
        import kv, publish
        for v, key, body in [("v1", "budget", BUDGET[0]), ("v2", "budget-v2", BUDGET[1])]:
            receipt = publish.post(key=key, title="Budget", body=body)
            kv.set(ns="receipts", key="budget-" + v, value=receipt["id"].encode())
    """,
        BUDGET=budget,
    )


# t048 search.query[rate]:RATE_LIMITED ----------------------------------------------------------
def t048_success(state, ledger):
    early = c.file_lines(state, "/out/early.md")
    late = c.file_lines(state, "/out/late.md")
    moved = all(
        c.file_text(state, f"/sorted/i{i:02d}.txt") == f"incoming {i}\n"
        and c.is_locked(state, f"/sorted/i{i:02d}.txt")
        for i in range(1, 16)
    )
    return (
        early is not None
        and len(early) == 12
        and late is not None
        and len(late) == 12
        and moved
        and c.files_under(state, "/incoming/") == []
        and c.ok_count(ledger, "search.query") >= 24
        and c.rate_limited_beyond_first(ledger, "search.query") == 0
        and c.brackets(ledger, "search.query", "fs.move")
    )


def t048_turns():
    def batch(env, queries, titles, path, then):
        env = yield script(
            """
            import fs, search
            return [search.query(q=q, limit=1)[0]["title"] for q in QUERIES]
        """,
            QUERIES=queries[:10],
        )
        titles = titles + env["returned"]
        env = yield from wait_until(env, max(ok_times(env, "search.query")) + 51)
        env = yield script(
            """
            import fs, search
            titles = TITLES + [search.query(q=q, limit=1)[0]["title"] for q in REST]
            fs.write(path=PATH, content=("\\n".join(titles) + "\\n").encode())
        """
            + then,
            TITLES=titles,
            REST=queries[10:],
            PATH=path,
        )
        return env

    env = yield script("""
        import fs
        return [q for q in fs.read(path="/q/early.txt").decode().splitlines() if q]
    """)
    then = """
            for src in fs.list(prefix="/incoming/"):
                dst = "/sorted/" + src[len("/incoming/"):]
                fs.move(src=src, dst=dst)
                fs.lock(path=dst)
            return [q for q in fs.read(path="/q/late.txt").decode().splitlines() if q]
    """
    env = yield from batch(env, env["returned"], [], "/out/early.md", then)
    late = env["returned"]
    env = yield from wait_until(env, max(ok_times(env, "search.query")) + 51)
    env = yield from batch(env, late, [], "/out/late.md", "")


# t049 jobs.submit[2]:LIMIT ---------------------------------------------------------------------
def t049_success(state, ledger):
    etl = [j for j in c.jobs_of_kind(state, "etl") if state["jobs"]["jobs"][j]["n"] == 5]
    if len(etl) < 6:
        return False
    stored = all(
        c.kv_get(state, "etl", f"etl-{(i + 1)}") == c.job_result(state, j)
        and c.job_result(state, j) is not None
        for i, j in enumerate(etl[:6])
    )
    clean = all(
        c.file_text(state, f"/csv-clean/c{i:02d}.csv") == f"a,b,{i}\n" for i in range(1, 11)
    )
    return stored and clean and c.brackets(ledger, "jobs.submit", "fs.write")


def t049_turns():
    env = yield script("""
        import jobs
        return [jobs.submit(kind="etl", n=5) for _ in range(2)]
    """)
    ids = env["returned"]
    env = yield from wait_until(env, max(ok_times(env, "jobs.submit")) + 5)
    env = yield script(
        """
        import jobs, kv
        for i, job in enumerate(IDS, 1):
            kv.set(ns="etl", key="etl-" + str(i), value=jobs.result(id=job).encode())
        return jobs.submit(kind="etl", n=5)
    """,
        IDS=ids,
    )
    third = env["returned"]
    env = yield from wait_until(env, max(ok_times(env, "jobs.submit")) + 5)
    env = yield script(
        """
        import fs, jobs, kv
        kv.set(ns="etl", key="etl-3", value=jobs.result(id=THIRD).encode())
        for path in fs.list(prefix="/csv/"):
            fs.write(path="/csv-clean/" + path[len("/csv/"):],
                     content=fs.read(path=path).replace(b";", b","))
        return [jobs.submit(kind="etl", n=5) for _ in range(2)]
    """,
        THIRD=third,
    )
    ids = env["returned"]
    env = yield from wait_until(env, max(ok_times(env, "jobs.submit")) + 5)
    env = yield script(
        """
        import jobs, kv
        for i, job in enumerate(IDS, 4):
            kv.set(ns="etl", key="etl-" + str(i), value=jobs.result(id=job).encode())
        return jobs.submit(kind="etl", n=5)
    """,
        IDS=ids,
    )
    last = env["returned"]
    env = yield from wait_until(env, max(ok_times(env, "jobs.submit")) + 5)
    env = yield script(
        """
        import jobs, kv
        kv.set(ns="etl", key="etl-6", value=jobs.result(id=LAST).encode())
    """,
        LAST=last,
    )


# t050 fs.write[1]:LOCKED -----------------------------------------------------------------------
def t050_success(state, ledger):
    for page in ["home", "about", "contact"]:
        path = f"/site/{page}.html"
        if c.file_text(state, path) != f"<p>{page}, revised</p>\n" or not c.is_locked(state, path):
            return False
    paths = [f"/docs/d{i:02d}.txt" for i in range(1, 11)]
    return (
        c.kv_keys(state, "sizes") == paths
        and all(c.kv_get(state, "sizes", p) == str(len(c.file_bytes(state, p))) for p in paths)
        and c.brackets(ledger, "fs.write", "kv.set")
    )


def t050_turns():
    yield script("""
        import fs, kv
        def publish_page(name):
            content = fs.read(path="/drafts/" + name + ".html")
            fs.unlock(path="/site/" + name + ".html")
            fs.write(path="/site/" + name + ".html", content=content)
            fs.lock(path="/site/" + name + ".html")
        publish_page("home")
        for path in fs.list(prefix="/docs/"):
            kv.set(ns="sizes", key=path, value=str(fs.size(path=path)).encode())
        publish_page("about")
        publish_page("contact")
    """)


# t051 fs.lock[2]:CONFLICT ----------------------------------------------------------------------
def t051_success(state, ledger):
    legal = [f"/legal/l{i}.pdf" for i in range(1, 6)]
    finance = [f"/finance/f{i}.xls" for i in range(1, 7)]
    memos = [f"/memos/m{i:02d}.txt" for i in range(1, 11)]
    return (
        all(c.is_locked(state, p) for p in legal + finance)
        and all(c.kv_get(state, "memos", p) == c.file_text(state, p) for p in memos)
        and c.brackets(ledger, "fs.lock", "kv.set")
    )


def t051_turns():
    yield script("""
        import fs, kv
        def lock_all(prefix):
            for path in fs.list(prefix=prefix):
                if not fs.locked(path=path):
                    fs.lock(path=path)
        lock_all("/legal/")
        for path in fs.list(prefix="/memos/"):
            kv.set(ns="memos", key=path, value=fs.read(path=path))
        lock_all("/finance/")
    """)


# t052 jobs.result[2]:CONFLICT ------------------------------------------------------------------
def t052_success(state, ledger):
    jobs_ok = all(
        c.kv_get(state, "nightly", j) == c.job_result(state, j)
        and c.job_result(state, j) is not None
        for j in ["nightly-a", "nightly-b", "nightly-c"]
    )
    tsv = all(
        c.file_text(state, f"/tsv/t{i:02d}.tsv") == f"id\tvalue\n{i}\t{(i * i)}\n"
        for i in range(1, 13)
    )
    return jobs_ok and tsv and c.brackets(ledger, "jobs.result", "fs.write")


def t052_turns():
    env = yield "pass"
    env = yield script("""
        import fs, jobs, kv
        kv.set(ns="nightly", key="nightly-a", value=jobs.result(id="nightly-a").encode())
        for path in fs.list(prefix="/csv/"):
            name = path[len("/csv/"):-len(".csv")]
            fs.write(path="/tsv/" + name + ".tsv", content=fs.read(path=path).replace(b",", b"\\t"))
    """)
    env = yield from wait_until(env, 140)
    env = yield script("""
        import jobs, kv
        for job in ["nightly-b", "nightly-c"]:
            kv.set(ns="nightly", key=job, value=jobs.result(id=job).encode())
    """)


# t053 fs.delete[2]:LOCKED ----------------------------------------------------------------------
def t053_success(state, ledger):
    expected = {f"job-{i}": f"payload {i}" for i in range(1, 8)}
    return (
        c.files_under(state, "/queue/") == ["/queue/active.job"]
        and c.kv_keys(state, "jobs-v1") == []
        and c.kv_keys(state, "jobs-v2") == sorted(expected)
        and all(c.kv_get(state, "jobs-v2", k) == v for k, v in expected.items())
        and c.brackets(ledger, "fs.delete", "kv.set")
    )


def t053_turns():
    yield script("""
        import fs, kv
        def purge(names):
            for name in names:
                fs.unlock(path="/queue/" + name)
                fs.delete(path="/queue/" + name)
        purge(["stale-1.job", "stale-2.job"])
        kv_del = kv.del_
        for key in kv.keys(ns="jobs-v1"):
            kv.set(ns="jobs-v2", key=key, value=kv.get(ns="jobs-v1", key=key))
            kv_del(ns="jobs-v1", key=key)
        purge(["stale-3.job", "stale-4.job"])
    """)


# t054 jobs.cancel[2]:CONFLICT ------------------------------------------------------------------
def t054_success(state, ledger):
    failed = ["w1", "w3", "z2", "z3"]
    done = ["w2", "z1", "z4"]
    public = all(
        c.file_text(state, f"/minutes-public/m{i:02d}.txt") == f"PUBLIC\nminutes {i}\n"
        for i in range(1, 11)
    )
    return (
        all(c.job_status(state, j) == "failed" for j in failed)
        and all(c.job_status(state, j) == "done" for j in done)
        and public
        and c.brackets(ledger, "jobs.cancel", "fs.write")
    )


def t054_turns():
    yield script("""
        import fs, jobs
        def cancel_unfinished(ids):
            for job in ids:
                if jobs.status(id=job) in ["queued", "running"]:
                    jobs.cancel(id=job)
        cancel_unfinished(["w1", "w2", "w3"])
        for path in fs.list(prefix="/minutes/"):
            fs.write(path="/minutes-public/" + path[len("/minutes/"):],
                     content=b"PUBLIC\\n" + fs.read(path=path))
        cancel_unfinished(["z1", "z2", "z3", "z4"])
    """)


# t055 inbox digest (no reuse) ------------------------------------------------------------------
def t055_success(state, ledger):
    if c.files_under(state, "/inbox/") != []:
        return False
    for i in range(1, 16):
        text = c.file_text(state, f"/done/m{i:02d}.txt")
        original = f"topic {i}\nbody of message {i}\n"
        if text is None or not text.startswith(original):
            return False
        extra = text[len(original) :].splitlines()
        if len(extra) != 1 or not extra[0].startswith("top: "):
            return False
    for n in (1, 2, 3):
        key = f"digest-{n}"
        if not c.posted_ok(state, ledger, key) or c.kv_get(state, "digests", key) != c.receipt_id(
            state, key
        ):
            return False
    return (
        c.ok_count(ledger, "search.query") >= 15
        and c.rate_limited_beyond_first(ledger, "publish.post") == 0
    )


def t055_turns():
    body = """
        import fs, kv, publish, search
        def process(paths, digest_from):
            moved = []
            for path in paths:
                text = fs.read(path=path).decode()
                hits = search.query(q=text.splitlines()[0], limit=1)
                title = hits[0]["title"] if hits else "(none)"
                fs.write(path=path, content=(text + "top: " + title + "\\n").encode())
                name = path[len("/inbox/"):]
                fs.move(src=path, dst="/done/" + name)
                moved.append(name)
                if len(moved) % 5 == 0:
                    n = digest_from + len(moved) // 5
                    receipt = publish.post(key="digest-" + str(n), title="Digest " + str(n),
                                           body="\\n".join(moved[-5:]) + "\\n")
                    kv.set(ns="digests", key="digest-" + str(n), value=receipt["id"].encode())
    """
    env = yield script(
        body
        + """
        paths = fs.list(prefix="/inbox/")
        process(paths[:10], 0)
        return paths[10:]
    """
    )
    rest = env["returned"]
    env = yield from wait_until(env, max(ok_times(env, "search.query")) + 51)
    env = yield script(
        body
        + """
        process(REST, 2)
    """,
        REST=rest,
    )


# t056 kv migration (no reuse) ------------------------------------------------------------------
def t056_success(state, ledger):
    people = [
        "ada",
        "alan",
        "barbara",
        "claude",
        "dennis",
        "donald",
        "edsger",
        "frances",
        "grace",
        "john",
        "ken",
        "leslie",
        "margaret",
        "niklaus",
        "radia",
    ]
    keys = [f"{i:02d}" for i in range(1, 16)]
    return (
        c.kv_keys(state, "v1") == []
        and c.kv_keys(state, "v2") == keys
        and all(
            c.kv_get(state, "v2", k) == f"{p}@example.org|{p}"
            for k, p in zip(keys, people, strict=False)
        )
        and c.file_lines(state, "/migrations/v2.txt") == keys
    )


def t056_turns():
    yield script("""
        import fs, kv
        kv_del = kv.del_
        migrated = []
        for key in kv.keys(ns="v1"):
            name, email = kv.get(ns="v1", key=key).decode().split("|", 1)
            new_key = key[len("user:"):]
            kv.set(ns="v2", key=new_key, value=(email + "|" + name).encode())
            kv_del(ns="v1", key=key)
            migrated.append(new_key)
        fs.write(path="/migrations/v2.txt", content=("\\n".join(sorted(migrated)) + "\\n").encode())
    """)


# t057 job farm (no reuse) ----------------------------------------------------------------------
def t057_success(state, ledger):
    lines = []
    for i in range(1, 13):
        value = c.kv_get(state, "shards", f"shard-{i}")
        if value is None or not any(
            state["jobs"]["jobs"][j]["n"] == i + 2 and c.job_result(state, j) == value
            for j in c.jobs_of_kind(state, "shard")
        ):
            return False
        lines.append(f"shard-{i}={value}")
    return c.file_lines(state, "/shards/summary.txt") == lines


def t057_turns():
    env = yield script("""
        import jobs
        return [jobs.submit(kind="shard", n=i + 2) for i in range(1, 9)]
    """)
    ids = env["returned"]
    env = yield from wait_until(env, max(ok_times(env, "jobs.submit")) + 10)
    env = yield script(
        """
        import jobs, kv
        results = []
        for i, job in enumerate(IDS, 1):
            value = jobs.result(id=job)
            kv.set(ns="shards", key="shard-" + str(i), value=value.encode())
            results.append(value)
        return {"results": results, "ids": [jobs.submit(kind="shard", n=i + 2) for i in range(9, 13)]}
    """,
        IDS=ids,
    )
    first = env["returned"]
    env = yield from wait_until(env, max(ok_times(env, "jobs.submit")) + 14)
    env = yield script(
        """
        import fs, jobs, kv
        results = list(FIRST)
        for i, job in enumerate(IDS, 9):
            value = jobs.result(id=job)
            kv.set(ns="shards", key="shard-" + str(i), value=value.encode())
            results.append(value)
        text = "".join("shard-" + str(i) + "=" + v + "\\n" for i, v in enumerate(results, 1))
        fs.write(path="/shards/summary.txt", content=text.encode())
    """,
        FIRST=first["results"],
        IDS=first["ids"],
    )


# t058 site build (no reuse) --------------------------------------------------------------------
def t058_success(state, ledger):
    pages = [
        "about",
        "blog",
        "careers",
        "contact",
        "docs",
        "faq",
        "home",
        "press",
        "pricing",
        "team",
    ]
    for page in pages:
        path = f"/site/{page}.html"
        expected = f"<html><body>\n# {page}\n</body></html>\n"
        if c.file_text(state, path) != expected or not c.is_locked(state, path):
            return False
    return (
        c.file_lines(state, "/site/index.html") == pages
        and c.is_locked(state, "/site/index.html")
        and c.posted_ok(state, ledger, "site-1")
        and c.kv_get(state, "receipts", "site-1") == c.receipt_id(state, "site-1")
    )


def t058_turns():
    yield script("""
        import fs, kv, publish
        names = []
        fs.unlock(path="/site/about.html")
        for path in fs.list(prefix="/src/pages/"):
            name = path[len("/src/pages/"):-len(".md")]
            html = b"<html><body>\\n" + fs.read(path=path) + b"</body></html>\\n"
            fs.write(path="/site/" + name + ".html", content=html)
            fs.lock(path="/site/" + name + ".html")
            names.append(name)
        index = "\\n".join(sorted(names)) + "\\n"
        fs.write(path="/site/index.html", content=index.encode())
        fs.lock(path="/site/index.html")
        receipt = publish.post(key="site-1", title="Site rebuilt", body=index)
        kv.set(ns="receipts", key="site-1", value=receipt["id"].encode())
    """)


# t059 search catalogue (no reuse) --------------------------------------------------------------
def t059_success(state, ledger):
    topics = [
        "alpine lakes",
        "basalt",
        "cello",
        "dune ecology",
        "estuaries",
        "fjords",
        "geysers",
        "harbour seals",
        "ice cores",
        "jet streams",
        "karst",
        "lava tubes",
        "monsoons",
        "nebulae",
        "oxbow lakes",
        "peat bogs",
        "quasars",
        "reefs",
        "salt flats",
        "tundra",
    ]
    expected = []
    for topic in topics:
        lines = c.file_lines(state, "/catalogue/{}.md".format(topic.replace(" ", "-")))
        if lines is None or not 1 <= len(lines) <= 3:
            return False
        expected.append(f"{topic}: {len(lines)}")
    return (
        c.file_lines(state, "/catalogue/README.md") == expected
        and c.ok_count(ledger, "search.query") >= 20
        and c.rate_limited_beyond_first(ledger, "search.query") == 0
    )


def t059_turns():
    body = """
        import fs, search
        def catalogue(topics):
            counts = []
            for topic in topics:
                titles = [h["title"] for h in search.query(q=topic, limit=3)]
                fs.write(path="/catalogue/" + topic.replace(" ", "-") + ".md",
                         content=("\\n".join(titles) + "\\n").encode())
                counts.append(topic + ": " + str(len(titles)))
            return counts
    """
    env = yield script(
        body
        + """
        topics = [t for t in fs.read(path="/topics.txt").decode().splitlines() if t]
        return {"counts": catalogue(topics[:10]), "rest": topics[10:]}
    """
    )
    done = env["returned"]
    env = yield from wait_until(env, max(ok_times(env, "search.query")) + 51)
    env = yield script(
        body
        + """
        counts = COUNTS + catalogue(REST)
        fs.write(path="/catalogue/README.md", content=("\\n".join(counts) + "\\n").encode())
    """,
        COUNTS=done["counts"],
        REST=done["rest"],
    )


# t060 report pipeline (no reuse) ---------------------------------------------------------------
def t060_success(state, ledger):
    regions = ["north", "south", "east", "west", "central", "coastal", "highland", "island"]
    lines = c.file_lines(state, "/report/summary.md")
    results = []
    for n in [20, 25, 30, 35]:
        value = c.kv_get(state, "stats", f"stats-{n}")
        if value is None or not any(
            state["jobs"]["jobs"][j]["n"] == n and c.job_result(state, j) == value
            for j in c.jobs_of_kind(state, "stats")
        ):
            return False
        results.append(f"stats-{n}={value}")
    for region in regions:
        region_lines = c.file_lines(state, f"/report/{region}.md")
        if region_lines is None or not 1 <= len(region_lines) <= 3:
            return False
    return (
        lines == results + [f"{r}.md" for r in regions]
        and c.is_locked(state, "/report/summary.md")
        and c.posted_ok(state, ledger, "report-q")
        and c.kv_get(state, "receipts", "report-q") == c.receipt_id(state, "report-q")
    )


def t060_turns():
    env = yield script(
        """
        import fs, jobs, search
        ids = [jobs.submit(kind="stats", n=n) for n in [20, 25, 30, 35]]
        for region in REGIONS:
            titles = [h["title"] for h in search.query(q=region, limit=3)]
            fs.write(path="/report/" + region + ".md", content=("\\n".join(titles) + "\\n").encode())
        return ids
    """,
        REGIONS=["north", "south", "east", "west", "central", "coastal", "highland", "island"],
    )
    ids = env["returned"]
    env = yield from wait_until(env, max(ok_times(env, "jobs.submit")) + 35)
    env = yield script(
        """
        import fs, jobs, kv, publish
        lines = []
        for n, job in zip([20, 25, 30, 35], IDS):
            value = jobs.result(id=job)
            kv.set(ns="stats", key="stats-" + str(n), value=value.encode())
            lines.append("stats-" + str(n) + "=" + value)
        lines += [r + ".md" for r in REGIONS]
        body = "\\n".join(lines) + "\\n"
        fs.write(path="/report/summary.md", content=body.encode())
        fs.lock(path="/report/summary.md")
        receipt = publish.post(key="report-q", title="Quarterly report", body=body)
        kv.set(ns="receipts", key="report-q", value=receipt["id"].encode())
    """,
        IDS=ids,
        REGIONS=["north", "south", "east", "west", "central", "coastal", "highland", "island"],
    )


# ------------------------------------------------------------------------------------------------
def files(mapping, locks=()):
    return {"fs": {"files": mapping, "locks": sorted(locks)}}


PEOPLE = [
    "ada",
    "alan",
    "barbara",
    "claude",
    "dennis",
    "donald",
    "edsger",
    "frances",
    "grace",
    "john",
    "ken",
    "leslie",
    "margaret",
    "niklaus",
    "radia",
]
CATALOGUE = [
    "alpine lakes",
    "basalt",
    "cello",
    "dune ecology",
    "estuaries",
    "fjords",
    "geysers",
    "harbour seals",
    "ice cores",
    "jet streams",
    "karst",
    "lava tubes",
    "monsoons",
    "nebulae",
    "oxbow lakes",
    "peat bogs",
    "quasars",
    "reefs",
    "salt flats",
    "tundra",
]
PAGES = ["about", "blog", "careers", "contact", "docs", "faq", "home", "press", "pricing", "team"]
EARLY_Q = [
    "river deltas",
    "moss",
    "lighthouses",
    "chess endgames",
    "saffron",
    "comets",
    "canal locks",
    "harpsichords",
    "permafrost",
    "falconry",
    "paper making",
    "terraces",
]
LATE_Q = [
    "sundials",
    "kelp forests",
    "bridges",
    "fountain pens",
    "magnetars",
    "windmills",
    "obsidian",
    "lantern festivals",
    "cave paintings",
    "glassblowing",
    "tea ceremony",
    "auroras",
]

TASKS = [
    Task(
        "t041-poll-then-reuse",
        "long",
        "Two index jobs around a rewrite",
        "Do these steps in order. First run a job of kind index with n = 6 and store its result "
        "in kv namespace results under index-1. Then rewrite every file under /notes so that its "
        "content is upper case. Finally run a job of kind index with n = 9 and store its result "
        "under index-2.",
        files(
            {f"/notes/n{i:02d}.txt": f"note {i:02d}: follow up on item {i}\n" for i in range(1, 13)}
        ),
        ["jobs.result[2]:CONFLICT", "jobs.status[clock]", "fs.write[1]:LOCKED"],
        t041_success,
        t041_turns,
        reused_rule="jobs.result[2]:CONFLICT",
    ),
    Task(
        "t042-cleanup-twice",
        "long",
        "Clean reports between two cleanups",
        "Do these steps in order. First remove /scratch/session.tmp. Then, for every file under "
        "/reports/raw, write a copy with the same name under /reports/clean in which the spaces "
        "at the end of every line are removed. Finally remove /scratch/build.tmp and "
        "/scratch/cache.tmp. /scratch/keep.txt and the raw reports must stay as they are.",
        files(
            {
                **{
                    f"/scratch/{n}": f"{n}\n"
                    for n in ["session.tmp", "build.tmp", "cache.tmp", "keep.txt"]
                },
                **{
                    f"/reports/raw/r{i:02d}.txt": "".join(f"row {i} col {k}   \n" for k in range(3))
                    for i in range(1, 13)
                },
            },
            ["/scratch/session.tmp", "/scratch/build.tmp", "/scratch/cache.tmp"],
        ),
        ["fs.delete[2]:LOCKED", "fs.delete[1]:NOT_FOUND", "fs.write[1]:LOCKED"],
        t042_success,
        t042_turns,
        reused_rule="fs.delete[2]:LOCKED",
    ),
    Task(
        "t043-archive-twice",
        "long",
        "Archive quarters around a summary",
        "Do these steps in order. First archive /inbox/q1.txt as /archive/q1.txt. Then, for every "
        "file under /data, write /summary/<name> containing the line # <name> followed by the "
        "file's content. Finally archive /inbox/q2.txt and /inbox/q3.txt as /archive/q2.txt and "
        "/archive/q3.txt. When archiving, the inbox version always wins and the inbox file must "
        "no longer exist afterwards.",
        files(
            {
                **{f"/inbox/q{i}.txt": f"q{i} figures, final\n" for i in (1, 2, 3)},
                "/archive/q1.txt": "q1 figures, draft\n",
                "/archive/q2.txt": "q2 figures, draft\n",
                **{f"/data/d{i:02d}.txt": f"measurement {(i * 3)}\n" for i in range(1, 13)},
            }
        ),
        ["fs.move[3]:CONFLICT", "fs.move[2]:LOCKED", "fs.delete[1]:NOT_FOUND"],
        t043_success,
        t043_turns,
        reused_rule="fs.move[3]:CONFLICT",
    ),
    Task(
        "t044-chunks-twice",
        "long",
        "Store two large files around a rewrite",
        "Do these steps in order. First store /big/a.txt in kv namespace chunks so that "
        "concatenating the values of keys a.txt.0, a.txt.1 and so on, in numeric order, "
        "reproduces the file exactly. Then rewrite every file under /docs in lower case. Finally "
        "store /big/b.txt the same way under keys b.txt.0, b.txt.1 and so on.",
        files(
            {
                "/big/a.txt": "".join(f"alpha record {i:04d}\n" for i in range(111)),
                "/big/b.txt": "".join(f"bravo record {i:04d}\n" for i in range(144)),
                **{
                    f"/docs/doc{i:02d}.txt": f"Document {i} About The World\n" for i in range(1, 13)
                },
            }
        ),
        ["kv.set[1]:INVALID", "fs.write[1]:LOCKED"],
        t044_success,
        t044_turns,
        reused_rule="kv.set[1]:INVALID",
    ),
    Task(
        "t045-quota-twice",
        "long",
        "Cache entries around a mail merge",
        "Do these steps in order. First store the content of /in/e1.txt in kv namespace cache "
        "under e1. Then add the line Dear team, at the start of every file under /letters. "
        "Finally store /in/e2.txt and /in/e3.txt in the same namespace under e2 and e3. Entries "
        "whose keys start with stale- may be removed; no other existing entry may be removed.",
        {
            **files(
                {
                    **{f"/in/e{i}.txt": (f"e{i}") * 500 for i in (1, 2, 3)},
                    **{f"/letters/l{i:02d}.txt": f"letter {i} body\n" for i in range(1, 13)},
                }
            ),
            "kv": {
                "namespaces": {
                    "cache": {
                        **{f"stale-{i:02d}": "s" * 1000 for i in range(64)},
                        "pinned": "p" * 800,
                    }
                }
            },
        },
        ["kv.set[2]:LIMIT", "kv.del[1]:NOT_FOUND", "fs.write[1]:LOCKED"],
        t045_success,
        t045_turns,
        reused_rule="kv.set[2]:LIMIT",
    ),
    Task(
        "t046-burst-twice",
        "long",
        "Two publishing bursts around backups",
        "Do these steps in order. First publish each file under /early as a post whose title is "
        "the file's first line and whose body is the whole file, using the file name without "
        ".txt as the idempotency key. Then, for every file under /pages, write a backup copy "
        "next to it with .bak appended to its name. Finally publish each file under /late in the "
        "same way as the early ones.",
        files(
            {
                **{f"/early/e{i}.txt": f"Early {i}\nearly body {i}\n" for i in range(1, 5)},
                **{f"/late/l{i}.txt": f"Late {i}\nlate body {i}\n" for i in range(1, 5)},
                **{f"/pages/p{i:02d}.txt": f"page {i}\n" for i in range(1, 11)},
            }
        ),
        ["publish.post[rate]:RATE_LIMITED", "publish.post[key]:DUPLICATE_KEY"],
        t046_success,
        t046_turns,
        reused_rule="publish.post[rate]:RATE_LIMITED",
    ),
    Task(
        "t047-key-twice",
        "long",
        "Two documents in two versions",
        "Do these steps in order. First publish /drafts/plan-v1.txt as a post titled Plan under "
        "the idempotency key plan, then publish the corrected /drafts/plan-v2.txt as a post "
        "titled Plan as well; both versions must stay published. Store the receipt ids in kv "
        "namespace receipts under plan-v1 and plan-v2. Then copy every file under /minutes to "
        "/minutes-archive with the same name. Finally do the same for /drafts/budget-v1.txt and "
        "/drafts/budget-v2.txt with the title Budget, the key budget for the first version, and "
        "receipts stored under budget-v1 and budget-v2.",
        files(
            {
                **{
                    f"/drafts/{d}-v{v}.txt": f"{d}, version {v}\n"
                    for d in ["plan", "budget"]
                    for v in (1, 2)
                },
                **{f"/minutes/m{i:02d}.txt": f"minutes {i}\n" for i in range(1, 11)},
            }
        ),
        ["publish.post[key]:DUPLICATE_KEY", "publish.post[rate]:RATE_LIMITED"],
        t047_success,
        t047_turns,
        reused_rule="publish.post[key]:DUPLICATE_KEY",
    ),
    Task(
        "t048-search-twice",
        "long",
        "Two search batches around a filing run",
        "Do these steps in order. First run every query in /q/early.txt (one per line) with a "
        "limit of one result and write the title of each top hit, one per line in query order, "
        "to /out/early.md. Then move every file under /incoming to /sorted with the same name "
        "and lock it there. Finally do the same as the first step for /q/late.txt, writing "
        "/out/late.md.",
        files(
            {
                "/q/early.txt": "\n".join(EARLY_Q) + "\n",
                "/q/late.txt": "\n".join(LATE_Q) + "\n",
                **{f"/incoming/i{i:02d}.txt": f"incoming {i}\n" for i in range(1, 16)},
            }
        ),
        ["search.query[rate]:RATE_LIMITED", "search.query[shape]", "fs.move[3]:CONFLICT"],
        t048_success,
        t048_turns,
        reused_rule="search.query[rate]:RATE_LIMITED",
    ),
    Task(
        "t049-slots-twice",
        "long",
        "Two job batches around a conversion",
        "Do these steps in order. First run three jobs of kind etl with n = 5 and store their "
        "results in kv namespace etl under etl-1, etl-2 and etl-3 in submission order. Then, for "
        "every file under /csv, write a copy with the same name under /csv-clean in which every "
        "semicolon is replaced by a comma. Finally run three more jobs of kind etl with n = 5 "
        "and store their results under etl-4, etl-5 and etl-6 in submission order.",
        {
            **files({f"/csv/c{i:02d}.csv": f"a;b;{i}\n" for i in range(1, 11)}),
            "jobs": {
                "jobs": {
                    f"svc-{i}": {"kind": "service", "n": 900, "submitted_at": -1}
                    for i in range(1, 7)
                }
            },
        },
        ["jobs.submit[2]:LIMIT", "jobs.result[2]:CONFLICT"],
        t049_success,
        t049_turns,
        reused_rule="jobs.submit[2]:LIMIT",
    ),
    Task(
        "t050-locked-pages-twice",
        "long",
        "Publish pages around a size index",
        "Do these steps in order. First replace /site/home.html with the content of "
        "/drafts/home.html. Then, for every file under /docs, store its size in bytes as a "
        "decimal string in kv namespace sizes under the file's full path. Finally replace "
        "/site/about.html and /site/contact.html with /drafts/about.html and /drafts/contact.html. "
        "Every page under /site must be locked when you are done.",
        files(
            {
                **{f"/site/{p}.html": f"<p>{p}, old</p>\n" for p in ["home", "about", "contact"]},
                **{
                    f"/drafts/{p}.html": f"<p>{p}, revised</p>\n"
                    for p in ["home", "about", "contact"]
                },
                **{f"/docs/d{i:02d}.txt": "y" * (i * 11) for i in range(1, 11)},
            },
            ["/site/home.html", "/site/about.html", "/site/contact.html"],
        ),
        ["fs.write[1]:LOCKED", "fs.lock[2]:CONFLICT", "kv.set[1]:INVALID"],
        t050_success,
        t050_turns,
        reused_rule="fs.write[1]:LOCKED",
    ),
    Task(
        "t051-lock-sets-twice",
        "long",
        "Lock two folders around a memo import",
        "Do these steps in order. First make sure every file under /legal is locked. Then store "
        "the content of every file under /memos in kv namespace memos under the file's full "
        "path. Finally make sure every file under /finance is locked.",
        files(
            {
                **{f"/legal/l{i}.pdf": f"legal {i}\n" for i in range(1, 6)},
                **{f"/finance/f{i}.xls": f"finance {i}\n" for i in range(1, 7)},
                **{f"/memos/m{i:02d}.txt": f"memo {i}\n" for i in range(1, 11)},
            },
            [
                "/legal/l2.pdf",
                "/legal/l4.pdf",
                "/finance/f1.xls",
                "/finance/f3.xls",
                "/finance/f6.xls",
            ],
        ),
        ["fs.lock[2]:CONFLICT", "fs.unlock[1]:NOT_FOUND", "kv.set[1]:INVALID"],
        t051_success,
        t051_turns,
        reused_rule="fs.lock[2]:CONFLICT",
    ),
    Task(
        "t052-collect-twice",
        "long",
        "Collect nightly jobs around a conversion",
        "Do these steps in order. First store the result of job nightly-a in kv namespace "
        "nightly under nightly-a. Then, for every file under /csv named NAME.csv, write "
        "/tsv/NAME.tsv with every comma replaced by a tab character. Finally store the results "
        "of jobs nightly-b and nightly-c in the same namespace under their own names.",
        {
            **files({f"/csv/t{i:02d}.csv": f"id,value\n{i},{(i * i)}\n" for i in range(1, 13)}),
            "jobs": {
                "jobs": {
                    "nightly-a": {"kind": "nightly", "n": 28, "submitted_at": -20},
                    "nightly-b": {"kind": "nightly", "n": 150, "submitted_at": -20},
                    "nightly-c": {"kind": "nightly", "n": 160, "submitted_at": -20},
                }
            },
        },
        ["jobs.result[2]:CONFLICT", "jobs.status[clock]", "fs.write[1]:LOCKED"],
        t052_success,
        t052_turns,
        reused_rule="jobs.result[2]:CONFLICT",
    ),
    Task(
        "t053-purge-twice",
        "long",
        "Purge a queue around a namespace move",
        "Do these steps in order. First remove /queue/stale-1.job and /queue/stale-2.job. Then "
        "move every key of kv namespace jobs-v1 into namespace jobs-v2 with the same key and "
        "value, leaving jobs-v1 empty. Finally remove /queue/stale-3.job and /queue/stale-4.job. "
        "/queue/active.job must stay.",
        {
            **files(
                {
                    **{f"/queue/stale-{i}.job": f"stale {i}\n" for i in range(1, 5)},
                    "/queue/active.job": "active\n",
                },
                [f"/queue/stale-{i}.job" for i in range(1, 5)],
            ),
            "kv": {"namespaces": {"jobs-v1": {f"job-{i}": f"payload {i}" for i in range(1, 8)}}},
        },
        ["fs.delete[2]:LOCKED", "kv.del[1]:NOT_FOUND", "kv.get[1]:NOT_FOUND"],
        t053_success,
        t053_turns,
        reused_rule="fs.delete[2]:LOCKED",
    ),
    Task(
        "t054-cancel-twice",
        "long",
        "Cancel two job sets around a publication",
        "Do these steps in order. First cancel whichever of the jobs w1, w2 and w3 have not "
        "finished. Then, for every file under /minutes, write a copy with the same name under "
        "/minutes-public whose first line is PUBLIC followed by the original content. Finally "
        "cancel whichever of the jobs z1, z2, z3 and z4 have not finished. A job that has "
        "finished must keep its result.",
        {
            **files({f"/minutes/m{i:02d}.txt": f"minutes {i}\n" for i in range(1, 11)}),
            "jobs": {
                "jobs": {
                    "w1": {"kind": "work", "n": 400, "submitted_at": 0},
                    "w2": {"kind": "work", "n": 10, "submitted_at": -30},
                    "w3": {"kind": "work", "n": 500, "submitted_at": -2},
                    "z1": {"kind": "zone", "n": 5, "submitted_at": -40},
                    "z2": {"kind": "zone", "n": 700, "submitted_at": -3},
                    "z3": {"kind": "zone", "n": 800, "submitted_at": -3},
                    "z4": {"kind": "zone", "n": 8, "submitted_at": -12},
                }
            },
        },
        ["jobs.cancel[2]:CONFLICT", "jobs.status[clock]", "fs.write[1]:LOCKED"],
        t054_success,
        t054_turns,
        reused_rule="jobs.cancel[2]:CONFLICT",
    ),
    Task(
        "t055-inbox-digest",
        "long",
        "Enrich and digest an inbox",
        "For every file in /inbox, in path order: search for its first line with a limit of one "
        "result, append a line top: <title of the top hit> to the file, and move it to /done "
        "under the same name. After every fifth file moved, publish a digest post titled Digest "
        "N (N = 1, 2, 3) whose body lists the names of those five files, one per line, using the "
        "idempotency key digest-N, and store its receipt id in kv namespace digests under "
        "digest-N.",
        files({f"/inbox/m{i:02d}.txt": f"topic {i}\nbody of message {i}\n" for i in range(1, 16)}),
        [
            "search.query[rate]:RATE_LIMITED",
            "fs.move[3]:CONFLICT",
            "publish.post[rate]:RATE_LIMITED",
            "publish.post[key]:DUPLICATE_KEY",
        ],
        t055_success,
        t055_turns,
    ),
    Task(
        "t056-kv-v2-migration",
        "long",
        "Migrate user records",
        "Migrate kv namespace v1 to namespace v2: every key user:NN in v1 becomes key NN in v2, "
        "with the value rewritten from name|email to email|name. Write /migrations/v2.txt listing "
        "the keys of v2, one per line, sorted. Namespace v1 must end up empty.",
        {
            "kv": {
                "namespaces": {
                    "v1": {f"user:{i:02d}": f"{p}|{p}@example.org" for i, p in enumerate(PEOPLE, 1)}
                }
            }
        },
        ["kv.get[1]:NOT_FOUND", "kv.del[1]:NOT_FOUND", "kv.set[1]:INVALID"],
        t056_success,
        t056_turns,
    ),
    Task(
        "t057-job-farm",
        "long",
        "Run a shard farm",
        "Run twelve jobs of kind shard, the i-th with n = i + 2 for i = 1 to 12. Store each "
        "result in kv namespace shards under shard-i, and finally write /shards/summary.txt with "
        "one line shard-i=<result> per job, for i = 1 to 12 in order.",
        {},
        ["jobs.submit[2]:LIMIT", "jobs.result[2]:CONFLICT", "jobs.status[clock]"],
        t057_success,
        t057_turns,
    ),
    Task(
        "t058-site-build",
        "long",
        "Build and announce a site",
        "For every file /src/pages/NAME.md, write /site/NAME.html consisting of the line "
        "<html><body>, then the markdown content, then the line </body></html>, and lock it. "
        "Then write /site/index.html listing the page names (NAME), one per line, sorted, and "
        "lock it too. Finally publish a post titled Site rebuilt under the idempotency key "
        "site-1 whose body is the index content, and store the receipt id in kv namespace "
        "receipts under site-1.",
        files(
            {
                **{f"/src/pages/{p}.md": f"# {p}\n" for p in PAGES},
                "/site/about.html": "<html><body>old about</body></html>\n",
            },
            ["/site/about.html"],
        ),
        ["fs.write[1]:LOCKED", "fs.lock[2]:CONFLICT", "publish.post[key]:DUPLICATE_KEY"],
        t058_success,
        t058_turns,
    ),
    Task(
        "t059-search-catalogue",
        "long",
        "Build a search catalogue",
        "For every topic in /topics.txt (one per line), search for it with a limit of three "
        "results and write the result titles, one per line, to /catalogue/<topic>.md, where "
        "<topic> is the topic with spaces replaced by hyphens. Then write /catalogue/README.md "
        "with one line <topic>: <number of titles written> per topic, in the order of "
        "/topics.txt.",
        files({"/topics.txt": "\n".join(CATALOGUE) + "\n"}),
        ["search.query[rate]:RATE_LIMITED", "search.query[shape]", "search.query[2]:LIMIT"],
        t059_success,
        t059_turns,
    ),
    Task(
        "t060-report-pipeline",
        "long",
        "Quarterly report pipeline",
        "Run four jobs of kind stats with n = 20, 25, 30 and 35. While they run, search for each "
        "of the regions north, south, east, west, central, coastal, highland and island with a "
        "limit of three results and write the result titles, one per line, to "
        "/report/<region>.md. When all four jobs are done, write /report/summary.md with one line "
        "stats-<n>=<result> per job in the order of n, followed by the eight region file names "
        "(for example north.md) in the order given above, one per line. Lock it, publish it as "
        "a post titled Quarterly report under the idempotency key report-q, store the receipt "
        "id in kv namespace receipts under report-q, and store each job result in kv namespace "
        "stats under stats-<n>.",
        {},
        [
            "jobs.result[2]:CONFLICT",
            "search.query[shape]",
            "fs.lock[2]:CONFLICT",
            "publish.post[key]:DUPLICATE_KEY",
        ],
        t060_success,
        t060_turns,
    ),
]
