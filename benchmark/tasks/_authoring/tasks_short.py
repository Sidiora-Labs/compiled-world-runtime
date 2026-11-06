# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Short bin: reference solutions of at most 5 primitives (t001 to t020)."""

import common as c
from model import Task, script, wait_until


# t001 ------------------------------------------------------------------------------------------
def t001_success(state, ledger):
    original = "CWR refworld readme\nVersion 0.2\nKeep this file.\n"
    return (
        c.file_text(state, "/backup/readme.txt") == original
        and c.file_text(state, "/docs/readme.txt") == original
    )


def t001_turns():
    yield script("""
        import fs
        fs.write(path="/backup/readme.txt", content=fs.read(path="/docs/readme.txt"))
    """)


# t002 ------------------------------------------------------------------------------------------
def t002_success(state, ledger):
    return (
        not c.file_exists(state, "/tmp/old.log")
        and c.file_text(state, "/tmp/keep.log") == "keep me\n"
        and c.file_text(state, "/tmp/session.log") == "session 42\n"
    )


def t002_turns():
    yield script("""
        import fs
        fs.unlock(path="/tmp/old.log")
        fs.delete(path="/tmp/old.log")
    """)


# t003 ------------------------------------------------------------------------------------------
def t003_success(state, ledger):
    lines = c.file_lines(state, "/notes/tennis.md")
    searches = c.called_with(ledger, "search.query", {"q": "tennis", "limit": 1})
    write_seq = c.last_seq(ledger, "fs.write")
    return (
        lines is not None
        and len(lines) == 1
        and 0 < len(lines[0]) <= 1000
        and len(searches) >= 1
        and write_seq is not None
        and searches[0]["seq"] < write_seq
    )


def t003_turns():
    yield script("""
        import fs, search
        hits = search.query(q="tennis", limit=1)
        fs.write(path="/notes/tennis.md", content=(hits[0]["title"] + "\\n").encode())
    """)


def t003_wrong_query():
    yield script("""
        import fs, search
        hits = search.query(q="golf", limit=1)
        fs.write(path="/notes/tennis.md", content=(hits[0]["title"] + "\\n").encode())
    """)


def t003_no_search():
    yield script("""
        import fs
        fs.write(path="/notes/tennis.md", content=b"Tennis\\n")
    """)


# t004 ------------------------------------------------------------------------------------------
def t004_success(state, ledger):
    return (
        c.kv_get(state, "prefs", "color") == "teal"
        and not c.kv_has(state, "prefs", "colour")
        and c.kv_get(state, "prefs", "font") == "serif"
        and c.kv_keys(state, "prefs") == ["color", "font"]
    )


def t004_turns():
    yield script("""
        import kv
        value = kv.get(ns="prefs", key="colour")
        kv.set(ns="prefs", key="color", value=value)
        kv.del_(ns="prefs", key="colour")
    """)


# t005 ------------------------------------------------------------------------------------------
def t005_success(state, ledger):
    return c.file_text(
        state, "/archive/report.txt"
    ) == "Q3 report, version 2\n" and not c.file_exists(state, "/inbox/report.txt")


def t005_turns():
    yield script("""
        import fs
        fs.delete(path="/archive/report.txt")
        fs.move(src="/inbox/report.txt", dst="/archive/report.txt")
    """)


# t006 ------------------------------------------------------------------------------------------
def t006_success(state, ledger):
    value = c.kv_get(state, "results", "thumbnail")
    for job_id in c.jobs_of_kind(state, "thumbnail"):
        if (
            c.job_status(state, job_id) == "done"
            and c.job_result(state, job_id) == value
            and state["jobs"]["jobs"][job_id]["n"] == 3
        ):
            return True
    return False


def t006_turns():
    env = yield script("""
        import jobs
        return jobs.submit(kind="thumbnail", n=3)
    """)
    job = env["returned"]
    env = yield script(
        """
        import jobs, kv
        kv.set(ns="results", key="thumbnail", value=jobs.result(id=JOB).encode())
    """,
        JOB=job,
    )


# t007 ------------------------------------------------------------------------------------------
def t007_success(state, ledger):
    return (
        c.is_locked(state, "/contracts/final.txt")
        and not c.is_locked(state, "/contracts/draft.txt")
        and c.file_text(state, "/contracts/final.txt") == "Final terms.\n"
        and c.file_text(state, "/contracts/draft.txt") == "Draft terms.\n"
    )


def t007_turns():
    yield script("""
        import fs
        fs.lock(path="/contracts/final.txt")
        fs.unlock(path="/contracts/draft.txt")
    """)


# t008 ------------------------------------------------------------------------------------------
def t008_success(state, ledger):
    return c.posted_ok(state, ledger, "rel-1.0") and c.kv_get(
        state, "receipts", "rel-1.0"
    ) == c.receipt_id(state, "rel-1.0")


def t008_turns():
    yield script("""
        import fs, kv, publish
        body = fs.read(path="/posts/release.txt").decode()
        receipt = publish.post(key="rel-1.0", title="Release 1.0", body=body)
        kv.set(ns="receipts", key="rel-1.0", value=receipt["id"].encode())
    """)


# t009 ------------------------------------------------------------------------------------------
def t009_success(state, ledger):
    return (
        c.job_status(state, "job-reindex") == "failed"
        and c.job_status(state, "job-cleanup") == "failed"
        and c.job_status(state, "job-backup") == "done"
        and c.job_result(state, "job-backup") is not None
    )


def t009_turns():
    yield script("""
        import jobs
        jobs.cancel(id="job-reindex")
        jobs.cancel(id="job-cleanup")
    """)


# t010 ------------------------------------------------------------------------------------------
def t010_success(state, ledger):
    return c.posted_ok(state, ledger, "essay-1")


def t010_turns():
    yield script("""
        import fs, publish
        text = fs.read(path="/posts/essay.txt").decode()
        publish.post(key="essay-1", title=text.splitlines()[0][:200], body=text)
    """)


# t011 ------------------------------------------------------------------------------------------
def t011_success(state, ledger):
    expected = "".join(
        f"blob line {i:03d}: the quick brown fox jumps over the lazy dog\n" for i in range(30)
    )
    parts, index = [], 0
    while c.kv_has(state, "blobs", f"blob.{index}"):
        parts.append(c.kv_get(state, "blobs", f"blob.{index}"))
        index += 1
    others = [k for k in c.kv_keys(state, "blobs") if k not in [f"blob.{i}" for i in range(index)]]
    return index > 0 and "".join(parts) == expected and not others


def t011_turns():
    yield script("""
        import fs, kv
        text = fs.read(path="/data/blob.txt").decode()
        kv.set(ns="blobs", key="blob.0", value=text[:1024].encode())
        kv.set(ns="blobs", key="blob.1", value=text[1024:].encode())
    """)


# t012 ------------------------------------------------------------------------------------------
def t012_success(state, ledger):
    return (
        c.file_lines(state, "/config/STATUS") == ["missing"]
        and not c.file_exists(state, "/config/app.ini.bak")
        and not c.file_exists(state, "/config/app.ini")
    )


def t012_turns():
    yield script("""
        import fs
        if fs.exists(path="/config/app.ini"):
            fs.write(path="/config/app.ini.bak", content=fs.read(path="/config/app.ini"))
        else:
            fs.write(path="/config/STATUS", content=b"missing\\n")
    """)


# t013 ------------------------------------------------------------------------------------------
def t013_success(state, ledger):
    return (
        not any(c.file_exists(state, p) for p in ["/cache/a.tmp", "/cache/b.tmp", "/cache/c.tmp"])
        and c.file_text(state, "/cache/keep.dat") == "persistent\n"
        and c.files_under(state, "/cache/") == ["/cache/keep.dat"]
    )


def t013_turns():
    yield script("""
        import fs
        fs.delete(path="/cache/a.tmp")
        fs.delete(path="/cache/c.tmp")
    """)


# t014 ------------------------------------------------------------------------------------------
def t014_success(state, ledger):
    value = c.kv_get(state, "results", "report")
    for job_id in c.jobs_of_kind(state, "report"):
        if state["jobs"]["jobs"][job_id]["n"] == 2 and c.job_result(state, job_id) == value:
            return value is not None
    return False


def t014_turns():
    env = yield "pass"
    env = yield from wait_until(env, 25)
    env = yield script("""
        import jobs
        return jobs.submit(kind="report", n=2)
    """)
    job = env["returned"]
    env = yield script(
        """
        import jobs, kv
        kv.set(ns="results", key="report", value=jobs.result(id=JOB).encode())
    """,
        JOB=job,
    )


# t015 ------------------------------------------------------------------------------------------
def t015_success(state, ledger):
    return (
        c.kv_keys(state, "session") == []
        and c.kv_get(state, "users", "u1") == "ada"
        and c.kv_get(state, "users", "u2") == "grace"
    )


def t015_turns():
    yield script("""
        import kv
        for key in kv.keys(ns="session"):
            kv.del_(ns="session", key=key)
    """)


# t016 ------------------------------------------------------------------------------------------
def t016_success(state, ledger):
    return c.file_text(
        state, "/done/job-17.txt"
    ) == "job 17: resize images\n" and not c.file_exists(state, "/queue/job-17.txt")


def t016_turns():
    yield script("""
        import fs
        fs.unlock(path="/queue/job-17.txt")
        fs.move(src="/queue/job-17.txt", dst="/done/job-17.txt")
    """)


# t017 ------------------------------------------------------------------------------------------
def t017_success(state, ledger):
    entrants = ["ada", "barbara", "edsger", "grace", "john", "ken", "margaret"]
    lines = c.file_lines(state, "/contest/winner.txt")
    random_seq = c.first_seq(ledger, "world.random")
    write_seq = c.last_seq(ledger, "fs.write")
    return (
        lines is not None
        and len(lines) == 1
        and lines[0] in entrants
        and random_seq is not None
        and write_seq is not None
        and random_seq < write_seq
    )


def t017_turns():
    yield script("""
        import fs, world
        names = [n for n in fs.read(path="/contest/entrants.txt").decode().splitlines() if n]
        r = world.random()
        index = int(r * len(names)) % len(names) if isinstance(r, float) else r % len(names)
        fs.write(path="/contest/winner.txt", content=(names[index] + "\\n").encode())
    """)


# t018 ------------------------------------------------------------------------------------------
def t018_success(state, ledger):
    draft = "<html><body>New front page</body></html>\n"
    return (
        c.file_text(state, "/site/index.html") == draft
        and c.is_locked(state, "/site/index.html")
        and c.file_text(state, "/drafts/index.html") == draft
    )


def t018_turns():
    yield script("""
        import fs
        content = fs.read(path="/drafts/index.html")
        fs.unlock(path="/site/index.html")
        fs.write(path="/site/index.html", content=content)
        fs.lock(path="/site/index.html")
    """)


# t019 ------------------------------------------------------------------------------------------
def t019_success(state, ledger):
    return c.kv_get(state, "jobs", "job-import") == "failed"


def t019_turns():
    yield "pass"
    yield script("""
        import jobs, kv
        status = jobs.status(id="job-import")
        kv.set(ns="jobs", key="job-import",
               value=(jobs.result(id="job-import") if status == "done" else "failed").encode())
    """)


# t020 ------------------------------------------------------------------------------------------
def t020_success(state, ledger):
    expected = {f"/shared/{n}.txt": f"shared file {n}\n" for n in "abcde"}
    return c.locked_under(state, "/shared/") == [] and all(
        c.file_text(state, p) == t for p, t in expected.items()
    )


def t020_turns():
    yield script("""
        import fs
        for name in ["a", "c", "e"]:
            fs.unlock(path="/shared/" + name + ".txt")
    """)


# ------------------------------------------------------------------------------------------------
ESSAY_TITLE = (
    "On worlds and bodies: why a harness that stays still under the model's feet "
    "lets the model spend its context on the task instead of on rediscovering which "
    "tools exist today, which ones changed overnight, and which failures mean what"
)
BLOB = "".join(
    f"blob line {i:03d}: the quick brown fox jumps over the lazy dog\n" for i in range(30)
)

TASKS = [
    Task(
        "t001-copy-readme",
        "short",
        "Copy a file",
        "Copy the file /docs/readme.txt to /backup/readme.txt. The original must stay where it is.",
        {
            "fs": {
                "files": {
                    "/docs/readme.txt": "CWR refworld readme\nVersion 0.2\nKeep this file.\n"
                },
                "locks": [],
            }
        },
        ["fs.read[1]:NOT_FOUND"],
        t001_success,
        t001_turns,
    ),
    Task(
        "t002-remove-old-log",
        "short",
        "Remove a log file",
        "Remove /tmp/old.log. Leave every other file under /tmp untouched.",
        {
            "fs": {
                "files": {
                    "/tmp/old.log": "rotated 2026-10-01\n",
                    "/tmp/keep.log": "keep me\n",
                    "/tmp/session.log": "session 42\n",
                },
                "locks": ["/tmp/old.log"],
            }
        },
        ["fs.delete[2]:LOCKED", "fs.delete[1]:NOT_FOUND", "fs.unlock[1]:NOT_FOUND"],
        t002_success,
        t002_turns,
    ),
    Task(
        "t003-tennis-note",
        "short",
        "Note the top tennis hit",
        'Search for "tennis" with a limit of 1 result and write the title of the top hit, on a '
        "single line, to /notes/tennis.md.",
        {"fs": {"files": {}, "locks": []}},
        ["search.query[shape]"],
        t003_success,
        t003_turns,
        extra={"near_misses": {"wrong-query": t003_wrong_query, "no-search": t003_no_search}},
    ),
    Task(
        "t004-rename-key",
        "short",
        "Rename a kv key",
        "In kv namespace prefs, rename the key colour to color, keeping its value. No other key "
        "may change and colour must no longer exist.",
        {"kv": {"namespaces": {"prefs": {"colour": "teal", "font": "serif"}}}},
        ["kv.get[1]:NOT_FOUND", "kv.del[1]:NOT_FOUND"],
        t004_success,
        t004_turns,
    ),
    Task(
        "t005-archive-report",
        "short",
        "Archive a report",
        "Archive /inbox/report.txt: afterwards /archive/report.txt must hold the inbox version "
        "and /inbox/report.txt must no longer exist.",
        {
            "fs": {
                "files": {
                    "/inbox/report.txt": "Q3 report, version 2\n",
                    "/archive/report.txt": "Q3 report, version 1\n",
                },
                "locks": [],
            }
        },
        ["fs.move[3]:CONFLICT", "fs.delete[1]:NOT_FOUND"],
        t005_success,
        t005_turns,
    ),
    Task(
        "t006-thumbnail-job",
        "short",
        "Run one job",
        "Run a job of kind thumbnail with n = 3 and store its result in kv namespace results "
        "under the key thumbnail.",
        {},
        ["jobs.result[2]:CONFLICT", "jobs.status[clock]"],
        t006_success,
        t006_turns,
    ),
    Task(
        "t007-lock-swap",
        "short",
        "Swap locks",
        "Make /contracts/final.txt locked and /contracts/draft.txt unlocked. Do not change "
        "either file's content.",
        {
            "fs": {
                "files": {
                    "/contracts/final.txt": "Final terms.\n",
                    "/contracts/draft.txt": "Draft terms.\n",
                },
                "locks": ["/contracts/draft.txt"],
            }
        },
        ["fs.lock[2]:CONFLICT", "fs.unlock[1]:NOT_FOUND"],
        t007_success,
        t007_turns,
    ),
    Task(
        "t008-publish-release",
        "short",
        "Publish a release note",
        "Publish a post titled Release 1.0 whose body is the content of /posts/release.txt, "
        "using the idempotency key rel-1.0, and store the receipt id in kv namespace receipts "
        "under the key rel-1.0.",
        {
            "fs": {
                "files": {"/posts/release.txt": "Release 1.0 ships the frozen world.\n"},
                "locks": [],
            }
        },
        ["publish.post[key]:DUPLICATE_KEY"],
        t008_success,
        t008_turns,
    ),
    Task(
        "t009-cancel-unfinished",
        "short",
        "Cancel unfinished jobs",
        "Cancel every one of the jobs job-reindex, job-backup and job-cleanup that has not "
        "finished yet. A job that has finished must keep its result.",
        {
            "jobs": {
                "jobs": {
                    "job-reindex": {"kind": "reindex", "n": 500, "submitted_at": 0},
                    "job-backup": {
                        "kind": "backup",
                        "n": 10,
                        "submitted_at": -50,
                    },
                    "job-cleanup": {"kind": "cleanup", "n": 300, "submitted_at": -5},
                }
            }
        },
        ["jobs.cancel[2]:CONFLICT", "jobs.status[clock]"],
        t009_success,
        t009_turns,
    ),
    Task(
        "t010-essay-post",
        "short",
        "Publish an essay",
        "Publish the essay in /posts/essay.txt under the idempotency key essay-1, titled with "
        "its first line and with the whole file as the body.",
        {
            "fs": {
                "files": {"/posts/essay.txt": ESSAY_TITLE + "\n\nA world is not a body.\n"},
                "locks": [],
            }
        },
        ["publish.post[1]:INVALID"],
        t010_success,
        t010_turns,
    ),
    Task(
        "t011-split-blob",
        "short",
        "Store a large file in kv",
        "Store the content of /data/blob.txt in kv namespace blobs so that concatenating the "
        "values of keys blob.0, blob.1, blob.2 and so on, in numeric order, reproduces the file "
        "exactly. Use no other keys in that namespace.",
        {"fs": {"files": {"/data/blob.txt": BLOB}, "locks": []}},
        ["kv.set[1]:INVALID"],
        t011_success,
        t011_turns,
    ),
    Task(
        "t012-config-fallback",
        "short",
        "Back up a config file if present",
        "If /config/app.ini exists, copy it to /config/app.ini.bak. If it does not exist, write "
        "the single line missing to /config/STATUS instead.",
        {"fs": {"files": {"/config/defaults.ini": "debug=false\n"}, "locks": []}},
        ["fs.read[1]:NOT_FOUND"],
        t012_success,
        t012_turns,
    ),
    Task(
        "t013-clear-cache",
        "short",
        "Clear cache files",
        "Make sure none of /cache/a.tmp, /cache/b.tmp and /cache/c.tmp exist afterwards. Do not "
        "touch any other file.",
        {
            "fs": {
                "files": {
                    "/cache/a.tmp": "a\n",
                    "/cache/c.tmp": "c\n",
                    "/cache/keep.dat": "persistent\n",
                },
                "locks": [],
            }
        },
        ["fs.delete[1]:NOT_FOUND"],
        t013_success,
        t013_turns,
    ),
    Task(
        "t014-report-slot",
        "short",
        "Run a report job",
        "Run a job of kind report with n = 2 and store its result in kv namespace results under "
        "the key report.",
        {
            "jobs": {
                "jobs": dict(
                    [("batch-1", {"kind": "batch", "n": 26, "submitted_at": -1})]
                    + [
                        (f"batch-{i}", {"kind": "batch", "n": 900, "submitted_at": -1})
                        for i in range(2, 9)
                    ]
                )
            }
        },
        ["jobs.submit[2]:LIMIT", "jobs.result[2]:CONFLICT"],
        t014_success,
        t014_turns,
    ),
    Task(
        "t015-clear-session",
        "short",
        "Clear a kv namespace",
        "Delete every key in kv namespace session. Other namespaces must not change.",
        {
            "kv": {
                "namespaces": {
                    "session": {"s-1": "x", "s-2": "y", "s-3": "z"},
                    "users": {"u1": "ada", "u2": "grace"},
                }
            }
        },
        ["kv.del[1]:NOT_FOUND"],
        t015_success,
        t015_turns,
    ),
    Task(
        "t016-finish-queue-item",
        "short",
        "Move a queue item",
        "Move /queue/job-17.txt to /done/job-17.txt.",
        {
            "fs": {
                "files": {"/queue/job-17.txt": "job 17: resize images\n"},
                "locks": ["/queue/job-17.txt"],
            }
        },
        ["fs.move[2]:LOCKED", "fs.unlock[1]:NOT_FOUND"],
        t016_success,
        t016_turns,
    ),
    Task(
        "t017-raffle",
        "short",
        "Draw a raffle winner",
        "Choose a winner from /contest/entrants.txt (one name per line) using the world's "
        "random source, and write the winner's name, alone on one line, to /contest/winner.txt.",
        {
            "fs": {
                "files": {
                    "/contest/entrants.txt": "ada\nbarbara\nedsger\ngrace\njohn\nken\nmargaret\n"
                },
                "locks": [],
            }
        },
        ["world.random[stream]", "fs.read[1]:NOT_FOUND"],
        t017_success,
        t017_turns,
    ),
    Task(
        "t018-replace-site-index",
        "short",
        "Replace the site index",
        "Replace the content of /site/index.html with the content of /drafts/index.html. "
        "/site/index.html must be locked when you are done.",
        {
            "fs": {
                "files": {
                    "/site/index.html": "<html><body>Old page</body></html>\n",
                    "/drafts/index.html": "<html><body>New front page</body></html>\n",
                },
                "locks": ["/site/index.html"],
            }
        },
        ["fs.write[1]:LOCKED", "fs.lock[2]:CONFLICT"],
        t018_success,
        t018_turns,
    ),
    Task(
        "t019-import-outcome",
        "short",
        "Record a job outcome",
        "Record the outcome of job job-import in kv namespace jobs under the key job-import: "
        "its result if it finished successfully, otherwise the word failed. If it is still "
        "running, wait until it is not.",
        {
            "jobs": {
                "jobs": {
                    "job-import": {"kind": "import", "n": 20, "submitted_at": -5, "fail_at": 3}
                }
            }
        },
        ["jobs.result[2]:CONFLICT", "jobs.status[clock]"],
        t019_success,
        t019_turns,
    ),
    Task(
        "t020-unlock-shared",
        "short",
        "Unlock a shared area",
        "Make sure no file under /shared is locked. File contents must not change.",
        {
            "fs": {
                "files": {f"/shared/{n}.txt": f"shared file {n}\n" for n in "abcde"},
                "locks": ["/shared/a.txt", "/shared/c.txt", "/shared/e.txt"],
            }
        },
        ["fs.unlock[1]:NOT_FOUND"],
        t020_success,
        t020_turns,
    ),
]
