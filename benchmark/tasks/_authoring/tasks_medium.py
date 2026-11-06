# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Medium bin: reference solutions of 6 to 25 primitives (t021 to t040)."""

import common as c
from model import Task, ok_times, script, wait_until


# t021 ------------------------------------------------------------------------------------------
def t021_success(state, ledger):
    golf = c.file_lines(state, "/reports/golf.md")
    tennis = c.file_lines(state, "/reports/tennis.md")
    sports = c.file_lines(state, "/reports/sports.md")
    body = c.file_text(state, "/reports/sports.md")
    post = {"key": "sports-1", "title": "Sports roundup", "body": body}
    return (
        golf is not None
        and tennis is not None
        and 1 <= len(golf) <= 3
        and 1 <= len(tennis) <= 3
        and sports == golf + tennis
        and c.is_locked(state, "/reports/sports.md")
        and c.posted_ok(state, ledger, "sports-1")
        and c.kv_get(state, "receipts", "sports-1") == c.receipt_id(state, "sports-1")
        and len(c.called_with(ledger, "search.query", {"q": "golf", "limit": 3})) >= 1
        and len(c.called_with(ledger, "search.query", {"q": "tennis", "limit": 3})) >= 1
        and body is not None
        and len(c.called_with(ledger, "publish.post", post)) >= 1
    )


def t021_turns():
    yield script("""
        import fs, kv, search, publish
        golf = [h["title"] for h in search.query(q="golf", limit=3)]
        tennis = [h["title"] for h in search.query(q="tennis", limit=3)]
        fs.write(path="/reports/golf.md", content=("\\n".join(golf) + "\\n").encode())
        fs.write(path="/reports/tennis.md", content=("\\n".join(tennis) + "\\n").encode())
        body = "\\n".join(golf + tennis) + "\\n"
        fs.write(path="/reports/sports.md", content=body.encode())
        fs.lock(path="/reports/sports.md")
        receipt = publish.post(key="sports-1", title="Sports roundup", body=body)
        kv.set(ns="receipts", key="sports-1", value=receipt["id"].encode())
    """)


def t021_body_without_newline():
    yield script("""
        import fs, kv, search, publish
        golf = [h["title"] for h in search.query(q="golf", limit=3)]
        tennis = [h["title"] for h in search.query(q="tennis", limit=3)]
        fs.write(path="/reports/golf.md", content=("\\n".join(golf) + "\\n").encode())
        fs.write(path="/reports/tennis.md", content=("\\n".join(tennis) + "\\n").encode())
        body = "\\n".join(golf + tennis) + "\\n"
        fs.write(path="/reports/sports.md", content=body.encode())
        fs.lock(path="/reports/sports.md")
        receipt = publish.post(key="sports-1", title="Sports roundup", body=body.strip())
        kv.set(ns="receipts", key="sports-1", value=receipt["id"].encode())
    """)


# t022 ------------------------------------------------------------------------------------------
def t022_success(state, ledger):
    for name in ["notice-v1", "notice-v2", "notice-v3"]:
        body = c.file_text(state, "/posts/" + name + ".txt")
        post = {"key": name, "title": "Maintenance notice", "body": body}
        if body is None or not c.posted_ok(state, ledger, name):
            return False
        if c.kv_get(state, "receipts", name) != c.receipt_id(state, name):
            return False
        if len(c.called_with(ledger, "publish.post", post)) < 1:
            return False
    return c.ok_count(ledger, "publish.delete") == 0


def t022_turns():
    yield script("""
        import fs, kv, publish
        for v in ["v1", "v2", "v3"]:
            body = fs.read(path="/posts/notice-" + v + ".txt").decode()
            receipt = publish.post(key="notice-" + v, title="Maintenance notice", body=body)
            kv.set(ns="receipts", key="notice-" + v, value=receipt["id"].encode())
    """)


def t022_other_keys():
    yield script("""
        import fs, kv, publish
        for v in ["v1", "v2", "v3"]:
            body = fs.read(path="/posts/notice-" + v + ".txt").decode()
            receipt = publish.post(key="notice-" + v + "-x", title="Maintenance notice", body=body)
            kv.set(ns="receipts", key="notice-" + v, value=receipt["id"].encode())
    """)


# t023 ------------------------------------------------------------------------------------------
def t023_success(state, ledger):
    expected = {n: f"inbox {n}, current\n" for n in "abcdef"}
    return c.files_under(state, "/inbox/") == [] and all(
        c.file_text(state, f"/archive/{n}.txt") == t for n, t in expected.items()
    )


def t023_turns():
    yield script("""
        import fs
        names = fs.list(prefix="/inbox/")
        fs.delete(path="/archive/b.txt")
        fs.delete(path="/archive/e.txt")
        fs.unlock(path="/inbox/d.txt")
        for path in names:
            fs.move(src=path, dst="/archive/" + path[len("/inbox/"):])
    """)


# t024 ------------------------------------------------------------------------------------------
def t024_success(state, ledger):
    for n in [5, 12, 20]:
        value = c.kv_get(state, "results", f"render-{n}")
        matches = [
            j
            for j in c.jobs_of_kind(state, "render")
            if state["jobs"]["jobs"][j]["n"] == n and c.job_result(state, j) == value
        ]
        if value is None or not matches:
            return False
    return True


def t024_turns():
    env = yield script("""
        import jobs
        return [jobs.submit(kind="render", n=n) for n in [5, 12, 20]]
    """)
    ids = env["returned"]
    env = yield "pass"
    env = yield script(
        """
        import jobs, kv
        for n, job in zip([5, 12, 20], IDS):
            kv.set(ns="results", key="render-" + str(n), value=jobs.result(id=job).encode())
    """,
        IDS=ids,
    )


# t025 ------------------------------------------------------------------------------------------
def t025_success(state, ledger):
    expected = {f"k{i}": f"legacy value {i}" for i in range(1, 7)}
    return (
        c.kv_keys(state, "legacy") == []
        and c.kv_keys(state, "current") == sorted(expected)
        and all(c.kv_get(state, "current", k) == v for k, v in expected.items())
    )


def t025_turns():
    yield script("""
        import kv
        kv_del = kv.del_
        for key in kv.keys(ns="legacy"):
            kv.set(ns="current", key=key, value=kv.get(ns="legacy", key=key))
            kv_del(ns="legacy", key=key)
    """)


# t026 ------------------------------------------------------------------------------------------
def t026_success(state, ledger):
    keys = c.kv_keys(state, "cache")
    fresh = [f"n{i}.txt" for i in range(1, 7)]
    if not all(c.kv_get(state, "cache", k) == ((f"{i}") * 1000) for i, k in enumerate(fresh, 1)):
        return False
    return (
        all(k in fresh or k == "pinned" or k.startswith("stale-") for k in keys)
        and c.kv_get(state, "cache", "pinned") == "keep"
    )


def t026_turns():
    yield script("""
        import fs, kv
        paths = fs.list(prefix="/incoming/")
        for i, path in enumerate(paths):
            if i == 5:
                kv.del_(ns="cache", key="stale-00")
            kv.set(ns="cache", key=path[len("/incoming/"):],
                   value=fs.read(path=path))
    """)


# t027 ------------------------------------------------------------------------------------------
def t027_success(state, ledger):
    for i in range(1, 6):
        key = f"a{i}"
        if not c.posted_ok(state, ledger, key):
            return False
        if c.kv_get(state, "receipts", key) != c.receipt_id(state, key):
            return False
    return c.rate_limited_beyond_first(ledger, "publish.post") == 0


def t027_turns():
    env = yield script("""
        import fs, kv, publish
        paths = fs.list(prefix="/posts/")
        texts = {p: fs.read(path=p).decode() for p in paths}
        for p in paths[:3]:
            key = p[len("/posts/"):-len(".txt")]
            receipt = publish.post(key=key, title=texts[p].splitlines()[0], body=texts[p])
            kv.set(ns="receipts", key=key, value=receipt["id"].encode())
        return {p: texts[p] for p in paths[3:]}
    """)
    rest = env["returned"]
    env = yield from wait_until(env, max(ok_times(env, "publish.post")) + 101)
    env = yield script(
        """
        import kv, publish
        for p, text in sorted(REST.items()):
            key = p[len("/posts/"):-len(".txt")]
            receipt = publish.post(key=key, title=text.splitlines()[0], body=text)
            kv.set(ns="receipts", key=key, value=receipt["id"].encode())
    """,
        REST=rest,
    )


# t028 ------------------------------------------------------------------------------------------
def t028_success(state, ledger):
    keep = {
        "/photos/p1.jpg": "A",
        "/photos/p2.jpg": "B",
        "/photos/p4.jpg": "C",
        "/photos/p6.jpg": "D",
        "/photos/p8.jpg": "E",
    }
    return c.files_under(state, "/photos/") == sorted(keep) and all(
        c.file_text(state, p) == "image bytes " + v + "\n" for p, v in keep.items()
    )


def t028_turns():
    yield script("""
        import fs
        seen = set()
        for path in fs.list(prefix="/photos/"):
            content = fs.read(path=path)
            if content in seen:
                if path == "/photos/p5.jpg":
                    fs.unlock(path=path)
                fs.delete(path=path)
            seen.add(content)
    """)


# t029 ------------------------------------------------------------------------------------------
def t029_success(state, ledger):
    topics = ["chess openings", "tide tables", "sourdough", "bike repair", "jazz", "orbits"]
    names = [t.replace(" ", "-") + ".md" for t in topics]
    for topic, name in zip(topics, names, strict=True):
        lines = c.file_lines(state, "/digest/" + name)
        if lines is None or not 1 <= len(lines) <= 5:
            return False
        if len(c.called_with(ledger, "search.query", {"q": topic, "limit": 5})) < 1:
            return False
    return c.file_lines(state, "/digest/index.md") == names


def t029_turns():
    yield script("""
        import fs, search
        topics = [t for t in fs.read(path="/topics.txt").decode().splitlines() if t]
        names = []
        for topic in topics:
            hits = search.query(q=topic, limit=5)
            name = topic.replace(" ", "-") + ".md"
            fs.write(path="/digest/" + name,
                     content=("\\n".join(h["title"] for h in hits) + "\\n").encode())
            names.append(name)
        fs.unlock(path="/digest/index.md")
        fs.write(path="/digest/index.md", content=("\\n".join(names) + "\\n").encode())
    """)


# t030 ------------------------------------------------------------------------------------------
def t030_success(state, ledger):
    queries = [
        "mountain huts",
        "rust borrow checker",
        "fermentation",
        "kite surfing",
        "lichens",
        "tram networks",
        "ceramics glazes",
        "solar sails",
        "origami",
        "beekeeping",
        "glacier retreat",
        "typesetting",
    ]
    lines = c.file_lines(state, "/sweep/top.md")
    searched = all(
        len(c.called_with(ledger, "search.query", {"q": q, "limit": 2})) >= 1 for q in queries
    )
    return (
        lines is not None
        and len(lines) == 12
        and searched
        and c.rate_limited_beyond_first(ledger, "search.query") == 0
    )


def t030_turns():
    env = yield script("""
        import fs, search
        queries = [q for q in fs.read(path="/queries.txt").decode().splitlines() if q]
        titles = [search.query(q=q, limit=2)[0]["title"] for q in queries[:10]]
        return {"titles": titles, "rest": queries[10:]}
    """)
    done = env["returned"]
    env = yield from wait_until(env, max(ok_times(env, "search.query")) + 51)
    env = yield script(
        """
        import fs, search
        titles = TITLES + [search.query(q=q, limit=2)[0]["title"] for q in REST]
        fs.write(path="/sweep/top.md", content=("\\n".join(titles) + "\\n").encode())
    """,
        TITLES=done["titles"],
        REST=done["rest"],
    )


# t031 ------------------------------------------------------------------------------------------
def t031_success(state, ledger):
    if any(c.job_status(state, j) != "failed" for j in ["enc-a", "enc-b", "enc-c"]):
        return False
    good = {
        c.job_result(state, j)
        for j in c.jobs_of_kind(state, "encode")
        if state["jobs"]["jobs"][j]["n"] == 4 and c.job_result(state, j) is not None
    }
    values = [c.kv_get(state, "results", k) for k in ["enc-a", "enc-b", "enc-c"]]
    return all(v in good for v in values) and len(set(values)) == 3


def t031_turns():
    env = yield script("""
        import jobs
        for job in ["enc-a", "enc-b", "enc-c"]:
            jobs.cancel(id=job)
        return [jobs.submit(kind="encode", n=4) for _ in range(3)]
    """)
    ids = env["returned"]
    env = yield script(
        """
        import jobs, kv
        for name, job in zip(["enc-a", "enc-b", "enc-c"], IDS):
            kv.set(ns="results", key=name, value=jobs.result(id=job).encode())
    """,
        IDS=ids,
    )


# t032 ------------------------------------------------------------------------------------------
def t032_success(state, ledger):
    for log in ["app", "db"]:
        base = f"/logs/{log}.log"
        if c.file_text(state, base) != "" or not c.is_locked(state, base):
            return False
        if c.file_text(state, base + ".1") != f"{log} live\n":
            return False
        if c.file_text(state, base + ".2") != f"{log} gen 1\n":
            return False
        if c.file_text(state, base + ".3") != f"{log} gen 2\n":
            return False
    return True


def t032_turns():
    yield script("""
        import fs
        for log in ["app", "db"]:
            base = "/logs/" + log + ".log"
            fs.delete(path=base + ".3")
            fs.move(src=base + ".2", dst=base + ".3")
            fs.move(src=base + ".1", dst=base + ".2")
            fs.unlock(path=base)
            fs.move(src=base, dst=base + ".1")
            fs.write(path=base, content=b"")
            fs.lock(path=base)
    """)


# t033 ------------------------------------------------------------------------------------------
def t033_success(state, ledger):
    paths = c.files_under(state, "/docs/")
    return (
        len(paths) == 10
        and c.kv_keys(state, "index") == paths
        and all(c.kv_get(state, "index", p) == str(len(c.file_bytes(state, p))) for p in paths)
    )


def t033_turns():
    yield script("""
        import fs, kv
        for path in fs.list(prefix="/docs/"):
            kv.set(ns="index", key=path, value=str(fs.size(path=path)).encode())
    """)


# t034 ------------------------------------------------------------------------------------------
def t034_success(state, ledger):
    merged = [
        ("host", "db.internal"),
        ("port", "6543"),
        ("user", "svc"),
        ("pool", "8"),
        ("timeout", "30"),
        ("tls", "on"),
        ("region", "eu-west"),
    ]
    lines = c.file_lines(state, "/config/app.ini")
    return (
        lines == ["{}={}".format(*kv) for kv in merged]
        and c.is_locked(state, "/config/app.ini")
        and c.kv_keys(state, "config") == sorted(k for k, _ in merged)
        and all(c.kv_get(state, "config", k) == v for k, v in merged)
    )


def t034_turns():
    yield script("""
        import fs, kv
        merged = {}
        for path in ["/config/base.ini", "/config/override.ini"]:
            for line in fs.read(path=path).decode().splitlines():
                if "=" in line:
                    key, value = line.split("=", 1)
                    merged[key] = value
        text = "".join(k + "=" + v + "\\n" for k, v in merged.items())
        fs.unlock(path="/config/app.ini")
        fs.write(path="/config/app.ini", content=text.encode())
        fs.lock(path="/config/app.ini")
        for key, value in merged.items():
            kv.set(ns="config", key=key, value=value.encode())
    """)


# t035 ------------------------------------------------------------------------------------------
def t035_success(state, ledger):
    scans = [j for j in c.jobs_of_kind(state, "scan") if state["jobs"]["jobs"][j]["n"] == 10]
    if len(scans) < 4:
        return False
    return all(
        c.kv_get(state, "scans", f"scan-{(i + 1)}") == c.job_result(state, j)
        and c.job_result(state, j) is not None
        for i, j in enumerate(scans[:4])
    )


def t035_turns():
    env = yield script("""
        import jobs
        return [jobs.submit(kind="scan", n=10) for _ in range(2)]
    """)
    first = env["returned"]
    env = yield script(
        """
        import jobs, kv
        for i, job in enumerate(FIRST, 1):
            kv.set(ns="scans", key="scan-" + str(i), value=jobs.result(id=job).encode())
        return [jobs.submit(kind="scan", n=10) for _ in range(2)]
    """,
        FIRST=first,
    )
    second = env["returned"]
    env = yield script(
        """
        import jobs, kv
        for i, job in enumerate(SECOND, 3):
            kv.set(ns="scans", key="scan-" + str(i), value=jobs.result(id=job).encode())
    """,
        SECOND=second,
    )


# t036 ------------------------------------------------------------------------------------------
def t036_success(state, ledger):
    if not all(c.posted_ok(state, ledger, k) for k in ["a", "b", "c"]):
        return False
    ids = {k: c.receipt_id(state, k) for k in ["a", "b", "c"]}
    deleted = {
        k: c.called_with(ledger, "publish.delete", {"post_id": ids[k]})
        for k in ids
        if isinstance(ids[k], str)
    }
    b_seq = c.idempotency_entry_seq(state, "b")
    return (
        len(deleted) == 3
        and len(deleted["b"]) >= 1
        and b_seq is not None
        and deleted["b"][0]["seq"] > b_seq
        and deleted["a"] == []
        and deleted["c"] == []
        and c.kv_get(state, "receipts", "a") == ids["a"]
        and c.kv_get(state, "receipts", "c") == ids["c"]
        and not c.kv_has(state, "receipts", "b")
    )


def t036_turns():
    yield script("""
        import fs, kv, publish
        receipts = {}
        for name in ["a", "b", "c"]:
            body = fs.read(path="/outbox/" + name + ".txt").decode()
            receipts[name] = publish.post(key=name, title=name + ".txt", body=body)["id"]
        publish.delete(post_id=receipts["b"])
        kv.set(ns="receipts", key="a", value=receipts["a"].encode())
        kv.set(ns="receipts", key="c", value=receipts["c"].encode())
    """)


def t036_delete_a():
    yield script("""
        import fs, kv, publish
        receipts = {}
        for name in ["a", "b", "c"]:
            body = fs.read(path="/outbox/" + name + ".txt").decode()
            receipts[name] = publish.post(key=name, title=name + ".txt", body=body)["id"]
        publish.delete(post_id=receipts["a"])
        kv.set(ns="receipts", key="a", value=receipts["a"].encode())
        kv.set(ns="receipts", key="c", value=receipts["c"].encode())
    """)


# t037 ------------------------------------------------------------------------------------------
def t037_success(state, ledger):
    expected = {f"f{i}.txt": f"source file {i}\n" for i in range(1, 9)}
    return (
        all(
            c.file_text(state, "/src/" + n) == t and c.file_text(state, "/dst/" + n) == t
            for n, t in expected.items()
        )
        and len(c.files_under(state, "/src/")) == 8
    )


def t037_turns():
    yield script("""
        import fs
        fs.unlock(path="/dst/f2.txt")
        fs.unlock(path="/dst/f5.txt")
        for path in fs.list(prefix="/src/"):
            fs.write(path="/dst/" + path[len("/src/"):], content=fs.read(path=path))
    """)


# t038 ------------------------------------------------------------------------------------------
def t038_success(state, ledger):
    sizes = {"x.txt": 2500, "y.txt": 1100, "z.txt": 3000}
    for name, size in sizes.items():
        expected = "".join(chr(97 + (i * 7 + len(name)) % 26) for i in range(size))
        parts, i = [], 0
        while c.kv_has(state, "chunks", f"{name}.{i}"):
            parts.append(c.kv_get(state, "chunks", f"{name}.{i}"))
            i += 1
        if "".join(parts) != expected:
            return False
    return True


def t038_turns():
    yield script("""
        import fs, kv
        for path in fs.list(prefix="/blobs/"):
            text = fs.read(path=path).decode()
            name = path[len("/blobs/"):]
            for i in range(0, len(text), 1024):
                kv.set(ns="chunks", key=name + "." + str(i // 1024), value=text[i:i + 1024].encode())
    """)


# t039 ------------------------------------------------------------------------------------------
def t039_success(state, ledger):
    locked = ["/vault/v2.dat", "/vault/v4.dat", "/vault/v5.dat", "/vault/v8.dat"]
    paths = [f"/vault/v{i}.dat" for i in range(1, 10)]
    return (
        c.locked_under(state, "/vault/") == locked
        and c.kv_keys(state, "vault") == paths
        and all(
            c.kv_get(state, "vault", p) == ("locked" if p in locked else "unlocked") for p in paths
        )
    )


def t039_turns():
    yield script("""
        import fs, kv
        for path in fs.list(prefix="/vault/"):
            kv.set(ns="vault", key=path,
                   value=("locked" if fs.locked(path=path) else "unlocked").encode())
    """)


# t040 ------------------------------------------------------------------------------------------
def t040_success(state, ledger):
    reviewers = ["ines", "kofi", "mei", "rafael"]
    tasks = [f"/tasks/task-{i}.txt" for i in range(1, 7)]
    return (
        all(c.kv_get(state, "assign", t) in reviewers for t in tasks)
        and c.ok_count(ledger, "world.random") >= 6
    )


def t040_turns():
    yield script("""
        import fs, kv, world
        names = [n for n in fs.read(path="/reviewers.txt").decode().splitlines() if n]
        for path in fs.list(prefix="/tasks/"):
            r = world.random()
            i = int(r * len(names)) % len(names) if isinstance(r, float) else r % len(names)
            kv.set(ns="assign", key=path, value=names[i].encode())
    """)


# ------------------------------------------------------------------------------------------------
def chars(name, size):
    return "".join(chr(97 + (i * 7 + len(name)) % 26) for i in range(size))


PHOTOS = {"p1": "A", "p2": "B", "p3": "A", "p4": "C", "p5": "B", "p6": "D", "p7": "A", "p8": "E"}
TOPICS = ["chess openings", "tide tables", "sourdough", "bike repair", "jazz", "orbits"]
QUERIES = [
    "mountain huts",
    "rust borrow checker",
    "fermentation",
    "kite surfing",
    "lichens",
    "tram networks",
    "ceramics glazes",
    "solar sails",
    "origami",
    "beekeeping",
    "glacier retreat",
    "typesetting",
]

TASKS = [
    Task(
        "t021-sports-report",
        "medium",
        "Sports roundup",
        "Search for golf and for tennis, each with a limit of 3 results. Write the golf result "
        "titles to /reports/golf.md and the tennis result titles to /reports/tennis.md, and both "
        "lists, golf first, to /reports/sports.md; in every file each title is followed by a "
        "newline and nothing else is written. Lock /reports/sports.md, publish its exact content "
        "as the body of a post titled Sports roundup under the idempotency key sports-1, and "
        "store the receipt id in kv namespace receipts under the key sports-1.",
        {},
        [
            "search.query[shape]",
            "fs.lock[2]:CONFLICT",
            "publish.post[key]:DUPLICATE_KEY",
        ],
        t021_success,
        t021_turns,
        extra={"near_misses": {"body-without-newline": t021_body_without_newline}},
    ),
    Task(
        "t022-notice-versions",
        "medium",
        "Publish three notice versions",
        "Three versions of a maintenance notice are in /posts/notice-v1.txt, /posts/notice-v2.txt "
        "and /posts/notice-v3.txt. Publish each version as its own post titled Maintenance notice "
        "with the exact file content as the body, using the file name without .txt (notice-v1, "
        "notice-v2, notice-v3) as the idempotency key, and store each receipt id in kv namespace "
        "receipts under notice-v1, notice-v2 and notice-v3. All three posts must remain "
        "published.",
        {
            "fs": {
                "files": {
                    f"/posts/notice-v{i}.txt": f"Maintenance window, revision {i}.\n"
                    for i in range(1, 4)
                },
                "locks": [],
            }
        },
        ["publish.post[key]:DUPLICATE_KEY"],
        t022_success,
        t022_turns,
        extra={"near_misses": {"other-keys": t022_other_keys}},
    ),
    Task(
        "t023-batch-archive",
        "medium",
        "Archive an inbox",
        "Move every file in /inbox to /archive under the same name. Where /archive already has "
        "a file of that name, the inbox version wins. /inbox must end up empty.",
        {
            "fs": {
                "files": {
                    **{f"/inbox/{n}.txt": f"inbox {n}, current\n" for n in "abcdef"},
                    "/archive/b.txt": "archive b, stale\n",
                    "/archive/e.txt": "archive e, stale\n",
                },
                "locks": ["/inbox/d.txt"],
            }
        },
        ["fs.move[3]:CONFLICT", "fs.move[2]:LOCKED"],
        t023_success,
        t023_turns,
    ),
    Task(
        "t024-render-trio",
        "medium",
        "Run three render jobs",
        "Run three jobs of kind render with n = 5, n = 12 and n = 20, and store their results "
        "in kv namespace results under render-5, render-12 and render-20.",
        {},
        ["jobs.result[2]:CONFLICT"],
        t024_success,
        t024_turns,
    ),
    Task(
        "t025-kv-migrate",
        "medium",
        "Move a kv namespace",
        "Move every key of kv namespace legacy into namespace current, keeping keys and values. "
        "Namespace legacy must end up empty.",
        {"kv": {"namespaces": {"legacy": {f"k{i}": f"legacy value {i}" for i in range(1, 7)}}}},
        ["kv.get[1]:NOT_FOUND", "kv.del[1]:NOT_FOUND"],
        t025_success,
        t025_turns,
    ),
    Task(
        "t026-cache-eviction",
        "medium",
        "Fill a cache",
        "Store each file in /incoming in kv namespace cache, keyed by its file name without the "
        "directory (for example n1.txt), with the file content as the value. Entries whose keys "
        "start with stale- may be removed; no other existing entry may be removed.",
        {
            "fs": {
                "files": {f"/incoming/n{i}.txt": (f"{i}") * 1000 for i in range(1, 7)},
                "locks": [],
            },
            "kv": {
                "namespaces": {
                    "cache": {**{f"stale-{i:02d}": "s" * 999 for i in range(60)}, "pinned": "keep"}
                }
            },
        },
        ["kv.set[2]:LIMIT", "kv.del[1]:NOT_FOUND"],
        t026_success,
        t026_turns,
    ),
    Task(
        "t027-publish-burst",
        "medium",
        "Publish a batch of posts",
        "Publish each file in /posts as a post whose title is the file's first line and whose "
        "body is the whole file, using the file name without .txt as the idempotency key (for "
        "example a1). Store each receipt id in kv namespace receipts under the same key.",
        {
            "fs": {
                "files": {
                    f"/posts/a{i}.txt": f"Announcement {i}\nDetails for item {i}.\n"
                    for i in range(1, 6)
                },
                "locks": [],
            }
        },
        ["publish.post[key]:DUPLICATE_KEY"],
        t027_success,
        t027_turns,
    ),
    Task(
        "t028-dedupe-photos",
        "medium",
        "Remove duplicate photos",
        "Several files under /photos have identical content. For every group of identical files, "
        "keep only the one whose path sorts first and delete the others.",
        {
            "fs": {
                "files": {f"/photos/{k}.jpg": f"image bytes {v}\n" for k, v in PHOTOS.items()},
                "locks": ["/photos/p5.jpg"],
            }
        },
        ["fs.delete[2]:LOCKED", "fs.read[1]:NOT_FOUND"],
        t028_success,
        t028_turns,
    ),
    Task(
        "t029-topic-digest",
        "medium",
        "Topic digest",
        "For each topic listed in /topics.txt (one per line), search for it with a limit of 5 "
        "results and write the result titles, one per line, to /digest/<topic>.md, where <topic> "
        "is the topic with spaces replaced by hyphens. Then write /digest/index.md listing those "
        "file names (for example jazz.md), one per line, in the order of /topics.txt.",
        {
            "fs": {
                "files": {
                    "/topics.txt": "\n".join(TOPICS) + "\n",
                    "/digest/index.md": "stale index\n",
                },
                "locks": ["/digest/index.md"],
            }
        },
        ["search.query[shape]", "fs.write[1]:LOCKED"],
        t029_success,
        t029_turns,
    ),
    Task(
        "t030-search-sweep",
        "medium",
        "Search sweep",
        "Run every query in /queries.txt (one per line) with a limit of two results and write "
        "the title of the top hit of each, one per line in the same order as the queries, to "
        "/sweep/top.md.",
        {"fs": {"files": {"/queries.txt": "\n".join(QUERIES) + "\n"}, "locks": []}},
        ["search.query[shape]"],
        t030_success,
        t030_turns,
    ),
    Task(
        "t031-resubmit-jobs",
        "medium",
        "Resubmit mis-sized jobs",
        "Jobs enc-a, enc-b and enc-c were submitted with the wrong size. Cancel them, run three "
        "new jobs of kind encode with n = 4 instead, and store the new results in kv namespace "
        "results under enc-a, enc-b and enc-c respectively.",
        {
            "jobs": {
                "jobs": {f"enc-{x}": {"kind": "encode", "n": 900, "submitted_at": 0} for x in "abc"}
            }
        },
        ["jobs.cancel[2]:CONFLICT", "jobs.result[2]:CONFLICT"],
        t031_success,
        t031_turns,
    ),
    Task(
        "t032-rotate-logs",
        "medium",
        "Rotate logs",
        "Rotate the logs app and db under /logs. For each of them the .3 file is discarded, .2 "
        "becomes .3, .1 becomes .2, the live log (for example /logs/app.log) becomes .1, and a new "
        "empty live log is created. Each new live log must be locked when you are done.",
        {
            "fs": {
                "files": {
                    **{f"/logs/{log}.log": f"{log} live\n" for log in ["app", "db"]},
                    **{
                        f"/logs/{log}.log.{i}": f"{log} gen {i}\n"
                        for log in ["app", "db"]
                        for i in (1, 2, 3)
                    },
                },
                "locks": ["/logs/app.log", "/logs/db.log"],
            }
        },
        ["fs.move[3]:CONFLICT", "fs.move[2]:LOCKED", "fs.lock[2]:CONFLICT"],
        t032_success,
        t032_turns,
    ),
    Task(
        "t033-size-index",
        "medium",
        "Index file sizes",
        "For every file under /docs, store its size in bytes, as a decimal string, in kv "
        "namespace index under the file's full path.",
        {
            "fs": {
                "files": {f"/docs/d{i:02d}.txt": "x" * (i * 37) + "\n" for i in range(1, 11)},
                "locks": [],
            }
        },
        ["fs.size[1]:NOT_FOUND"],
        t033_success,
        t033_turns,
    ),
    Task(
        "t034-config-merge",
        "medium",
        "Merge configuration",
        "Merge /config/base.ini and /config/override.ini, both made of lines key=value, into "
        "/config/app.ini: a key in override.ini replaces the same key in base.ini, and keys appear "
        "in order of first appearance, base.ini first. /config/app.ini must be locked afterwards. "
        "Also store every merged key and value in kv namespace config.",
        {
            "fs": {
                "files": {
                    "/config/base.ini": "host=db.local\nport=5432\nuser=svc\npool=4\ntimeout=30\ntls=on\n",
                    "/config/override.ini": "host=db.internal\nport=6543\npool=8\nregion=eu-west\n",
                    "/config/app.ini": "host=stale\n",
                },
                "locks": ["/config/app.ini"],
            }
        },
        ["fs.write[1]:LOCKED", "fs.lock[2]:CONFLICT"],
        t034_success,
        t034_turns,
    ),
    Task(
        "t035-job-slots",
        "medium",
        "Run four scan jobs",
        "Run four jobs of kind scan with n = 10 and store their results in kv namespace scans "
        "under scan-1, scan-2, scan-3 and scan-4, in the order in which you submitted the jobs.",
        {
            "jobs": {
                "jobs": {
                    f"svc-{i}": {"kind": "service", "n": 900, "submitted_at": -1}
                    for i in range(1, 7)
                }
            }
        },
        ["jobs.submit[2]:LIMIT", "jobs.result[2]:CONFLICT"],
        t035_success,
        t035_turns,
    ),
    Task(
        "t036-retract-post",
        "medium",
        "Publish and retract",
        "Publish /outbox/a.txt, /outbox/b.txt and /outbox/c.txt as posts titled with the file "
        "name (for example a.txt) and with the file content as the body, using the idempotency "
        "keys a, b and c. Then retract the post for b by deleting it from the service. Store the "
        "receipt ids of the posts that remain in kv namespace receipts under a and c, and make "
        "sure there is no entry for b.",
        {
            "fs": {
                "files": {f"/outbox/{n}.txt": f"outbox item {n}\n" for n in "abc"},
                "locks": [],
            }
        },
        ["publish.post[key]:DUPLICATE_KEY", "kv.del[1]:NOT_FOUND"],
        t036_success,
        t036_turns,
        extra={"near_misses": {"delete-a": t036_delete_a}},
    ),
    Task(
        "t037-copy-tree",
        "medium",
        "Copy a tree",
        "Copy every file under /src to /dst with the same name, replacing anything already at "
        "the destination. Files under /src must not change.",
        {
            "fs": {
                "files": {
                    **{f"/src/f{i}.txt": f"source file {i}\n" for i in range(1, 9)},
                    "/dst/f2.txt": "old 2\n",
                    "/dst/f5.txt": "old 5\n",
                },
                "locks": ["/dst/f2.txt", "/dst/f5.txt"],
            }
        },
        ["fs.write[1]:LOCKED", "fs.read[1]:NOT_FOUND"],
        t037_success,
        t037_turns,
    ),
    Task(
        "t038-chunk-files",
        "medium",
        "Store files in kv chunks",
        "Store each file under /blobs in kv namespace chunks so that, for a file named NAME (for "
        "example x.txt), concatenating the values of keys NAME.0, NAME.1, NAME.2 and so on, in "
        "numeric order, reproduces the file exactly.",
        {
            "fs": {
                "files": {
                    "/blobs/" + n: chars(n, s)
                    for n, s in [("x.txt", 2500), ("y.txt", 1100), ("z.txt", 3000)]
                },
                "locks": [],
            }
        },
        ["kv.set[1]:INVALID"],
        t038_success,
        t038_turns,
    ),
    Task(
        "t039-lock-audit",
        "medium",
        "Audit vault locks",
        "For every file under /vault, store in kv namespace vault, under the file's full path, "
        "the word locked or unlocked according to its current state. Do not change any lock.",
        {
            "fs": {
                "files": {f"/vault/v{i}.dat": f"vault {i}\n" for i in range(1, 10)},
                "locks": ["/vault/v2.dat", "/vault/v4.dat", "/vault/v5.dat", "/vault/v8.dat"],
            }
        },
        ["fs.lock[2]:CONFLICT", "fs.unlock[1]:NOT_FOUND"],
        t039_success,
        t039_turns,
    ),
    Task(
        "t040-assign-reviewers",
        "medium",
        "Assign reviewers at random",
        "Assign each file under /tasks a reviewer chosen with the world's random source from "
        "/reviewers.txt (one name per line), and store the reviewer's name in kv namespace "
        "assign under the task file's full path.",
        {
            "fs": {
                "files": {
                    "/reviewers.txt": "ines\nkofi\nmei\nrafael\n",
                    **{f"/tasks/task-{i}.txt": f"task {i}\n" for i in range(1, 7)},
                },
                "locks": [],
            }
        },
        ["world.random[stream]", "fs.read[1]:NOT_FOUND"],
        t040_success,
        t040_turns,
    ),
]
