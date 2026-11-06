#!/usr/bin/env python3
# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Generate and check the CWR task suite (task 4.2). Authoring tool; its PASS is not evidence.

    python3 benchmark/tasks/_authoring/build_suite.py          # regenerate files, then check
    python3 benchmark/tasks/_authoring/build_suite.py --check  # check that files on disk are current
    python3 benchmark/tasks/_authoring/build_suite.py --kernel-counts FILE  # import kernel counts

With _authoring/kernel_counts.json present (written from `tests/cwr/cases/4_2.py --write-counts`,
imported with --kernel-counts), primitive_count, turns and reuse_positions in suite.kvx and the
solutions come from kernel replay ledgers and count_source is "kernel-ledger".

It writes, for every task defined in tasks_short.py, tasks_medium.py and tasks_long.py:

    benchmark/tasks/<id>/task.md, initial.kvx, success.py, bin, rules, task.kvx
    benchmark/solutions/<id>.py
    benchmark/suite.kvx

and checks with the authoring model in refsim.py (not the kernel) that every reference solution
ends every turn ok with no failing call, satisfies its predicate twice with equal results, falls in
its declared bin, keeps within 200 turns and the per-turn budgets, and that every predicate is false
on a world where nothing was done. Rule-reuse tasks must place the reused primitive in the first
quarter, nowhere in the middle half, and again in the last quarter, at least ten calls apart.
Every generated .kvx file is parsed with parse_kvx from spec/cwr/tools/validate_spec.py.
"""

from __future__ import annotations

import argparse
import importlib.util
import inspect
import json
import re
import sys
import textwrap
from pathlib import Path

HERE = Path(__file__).resolve().parent
TASKS_ROOT = HERE.parent
BENCH = TASKS_ROOT.parent
REPO = BENCH.parent
SOLUTIONS = BENCH / "solutions"
SUITE = BENCH / "suite.kvx"
KERNEL_COUNTS = HERE / "kernel_counts.json"
COMMON = BENCH / "predicates" / "common.py"
VALIDATOR = REPO / "spec" / "cwr" / "tools" / "validate_spec.py"
# The Sidiora source header (CLAUDE.md, tools/header.py) that opens every generated Python file.
HEADER = (
    "# Copyright Sidiora Labs Inc. CWR\u00ae 2026-2027\n"
    "# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0\n"
)

sys.path.insert(0, str(BENCH / "predicates"))
sys.path.insert(0, str(HERE))

import common  # noqa: E402 - importable only after the sys.path entries above
import refsim  # noqa: E402 - importable only after the sys.path entries above
from model import PREAMBLE  # noqa: E402 - importable only after the sys.path entries above

BINS = {"short": (0, 5), "medium": (6, 25), "long": (26, 120)}
PER_BIN = 20
MIN_REUSE = 12
MAX_TURNS = 200


def load_validator():
    spec = importlib.util.spec_from_file_location("cwr_validate_spec", VALIDATOR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.parse_kvx


def all_tasks():
    tasks = []
    for name in ("tasks_short", "tasks_medium", "tasks_long"):
        if (HERE / f"{name}.py").exists():
            tasks.extend(importlib.import_module(name).TASKS)
    return tasks


def extract(fn, new_name: str) -> str:
    source = textwrap.dedent(inspect.getsource(fn))
    source, count = re.subn(rf"^def {fn.__name__}\(", f"def {new_name}(", source, count=1)
    if count != 1:
        raise ValueError(f"cannot rename {fn.__name__}")
    return source.rstrip() + "\n"


def kv_line(key, value) -> str:
    return f"{key} = {json.dumps(value, sort_keys=True, ensure_ascii=True)}"


def initial_kvx(task) -> str:
    init = task.initial
    fs = init.get("fs", {})
    lines = [
        *HEADER.splitlines(),
        f"# Initial Instance state for {task.id}. Loaded by cwr instance create; identical for",
        "# every arm and seed. Sections follow the state view in benchmark/predicates/common.py.",
        "",
        "[meta]",
        kv_line("format", "cwr.initial-state"),
        kv_line("schema_version", 1),
        kv_line("task", task.id),
        "",
        "[fs]",
        kv_line("files", fs.get("files", {})),
        kv_line("locks", sorted(fs.get("locks", []))),
        "",
        "[kv]",
        kv_line("namespaces", init.get("kv", {}).get("namespaces", {})),
        "",
        "[jobs]",
        kv_line("jobs", init.get("jobs", {}).get("jobs", {})),
        "",
    ]
    return "\n".join(lines)


def success_py(task) -> str:
    return (
        HEADER + f'"""Success predicate for {task.id}: {task.title}.\n\n'
        "Pure over the final state view and the ledger; imports only "
        'benchmark/predicates/common.py.\n"""\n\nimport common as c\n\n\n'
        + extract(task.success, "success")
    )


def solution_py(task, count=None, turns=None, source="authoring-model") -> str:
    origin = {
        "authoring-model": "recorded by the authoring model; CWR-015 recounts on the kernel",
        "kernel-ledger": "measured from kernel replay ledgers by tests/cwr/cases/4_2.py",
    }[source]
    stats = "" if count is None else f"PRIMITIVES = {count}  # {origin}\nTURNS = {turns}\n"
    return (
        HEADER + f'"""Reference solution for {task.id} ({task.bin}). Never shown to the model.\n\n'
        "Protocol: turns() is a generator. The driver calls next() for the first turn script "
        "and\nsend(envelope) with each result envelope afterwards. Every yielded string is the "
        "body of\nexactly one fenced python block; the driver wraps it in a ```python fence and "
        "submits it\nwith `cwr instance turn`. The helpers below run on the driver side only.\n"
        '"""\n\nimport textwrap\n\n'
        f'TASK = "{task.id}"\n' + stats + "\n\n" + PREAMBLE + "\n\n" + extract(task.turns, "turns")
    )


def task_md(task) -> str:
    # The source header in Markdown comment syntax; the instruction the model sees is task.md as is.
    header = "".join(f"<!-- {line.removeprefix('# ')} -->\n" for line in HEADER.splitlines())
    return f"{header}\n# {task.title}\n\n{textwrap.fill(task.instruction, 100)}\n"


def task_kvx(task) -> str:
    return "\n".join(
        [
            *HEADER.splitlines(),
            f"# Manifest for {task.id}. The instruction given to the model is task.md; this file is",
            "# harness metadata and is never rendered to the model.",
            "",
            "[meta]",
            kv_line("format", "cwr.task"),
            kv_line("schema_version", 1),
            "",
            "[task]",
            kv_line("id", task.id),
            kv_line("title", task.title),
            kv_line("bin", task.bin),
            kv_line("rules", task.rules),
            kv_line("rule_reuse", task.reused_rule is not None),
            kv_line("reused_rule", task.reused_rule),
            kv_line("files", ["task.md", "initial.kvx", "success.py", "bin", "rules", "task.kvx"]),
            kv_line("solution", f"benchmark/solutions/{task.id}.py"),
            "",
        ]
    )


def rules_file(task) -> str:
    ordered = list(task.rules)
    if task.reused_rule:
        ordered.remove(task.reused_rule)
        ordered.insert(0, task.reused_rule)
    return "\n".join(ordered) + "\n"


def render(task, count=None, turns=None, source="authoring-model") -> dict:
    return {
        TASKS_ROOT / task.id / "task.md": task_md(task),
        TASKS_ROOT / task.id / "initial.kvx": initial_kvx(task),
        TASKS_ROOT / task.id / "success.py": success_py(task),
        TASKS_ROOT / task.id / "bin": task.bin + "\n",
        TASKS_ROOT / task.id / "rules": rules_file(task),
        TASKS_ROOT / task.id / "task.kvx": task_kvx(task),
        SOLUTIONS / f"{task.id}.py": solution_py(task, count, turns, source),
    }


def load_turns(source: str):
    namespace = {"__name__": "solution"}
    exec(compile(source, "<solution>", "exec"), namespace)
    return namespace["turns"]


def reuse_shape(positions, total):
    early = [p for p in positions if p < total / 4]
    middle = [p for p in positions if total / 4 <= p < 3 * total / 4]
    late = [p for p in positions if p >= 3 * total / 4]
    ok = bool(early) and not middle and bool(late) and late[0] - early[-1] >= 10
    return ok, {
        "first": early[0] if early else None,
        "last_early": early[-1] if early else None,
        "first_late": late[0] if late else None,
    }


def check_task(task, problems):
    rendered = render(task)
    for rule in task.rules:
        try:
            common.parse_rule(rule)
        except ValueError as error:
            problems.append(f"{task.id}: {error}")
    if task.reused_rule and task.reused_rule not in task.rules:
        problems.append(f"{task.id}: reused rule not in rules")
    src = rendered[TASKS_ROOT / task.id / "success.py"]
    issues = common.check_predicate_source(src)
    if issues:
        problems.append(f"{task.id}: predicate audit: {issues}")
        return None
    namespace = {"__builtins__": __builtins__}
    predicate_ns = {}
    exec(
        compile(src.replace("import common as c", ""), "<success>", "exec"),
        {"c": common, **namespace},
        predicate_ns,
    )
    predicate = predicate_ns.get("success")
    turns = load_turns(rendered[SOLUTIONS / f"{task.id}.py"])
    world, envelopes = refsim.run_solution(task.initial, turns, seed=1, max_turns=MAX_TURNS)
    state, ledger = world.state_view(), world.ledger
    bad_turns = [e for e in envelopes if e["status"] != "ok"]
    if bad_turns:
        problems.append(f"{task.id}: reference turn not ok: {bad_turns[0]['error']}")
    calls = common.calls(ledger)
    if any(r["outcome"]["status"] != "ok" for r in calls):
        problems.append(f"{task.id}: reference makes a failing call")
    count = len(calls)
    low, high = BINS[task.bin]
    if not low <= count <= high:
        problems.append(f"{task.id}: {count} primitives outside bin {task.bin}")
    if not common.evaluate_twice(predicate, state, ledger):
        problems.append(f"{task.id}: predicate false after the reference solution")
    for seed in (2, 3):
        w2, _ = refsim.run_solution(
            task.initial,
            load_turns(rendered[SOLUTIONS / f"{task.id}.py"]),
            seed=seed,
            max_turns=MAX_TURNS,
        )
        if not common.evaluate_twice(predicate, w2.state_view(), w2.ledger):
            problems.append(f"{task.id}: predicate false for seed {seed}")
        if len(common.calls(w2.ledger)) != count:
            problems.append(f"{task.id}: primitive count differs for seed {seed}")
    idle, _ = refsim.run_solution(task.initial, lambda: (yield "pass"), seed=1)
    if common.evaluate_twice(predicate, idle.state_view(), idle.ledger):
        problems.append(f"{task.id}: predicate true when nothing was done")
    reuse = None
    if task.reused_rule:
        if task.bin != "long":
            problems.append(f"{task.id}: rule-reuse task outside the long bin")
        primitive = common.parse_rule(task.reused_rule)["primitive"]
        positions = [i for i, r in enumerate(calls) if r["primitive"] == primitive]
        ok, reuse = reuse_shape(positions, count)
        if not ok:
            problems.append(f"{task.id}: reuse shape fails for {primitive}: {positions} of {count}")
    return {"count": count, "turns": len(envelopes), "reuse": reuse}


def existing_cassette_digest(parse_kvx):
    """Return the cassette digest frozen in suite.kvx so that regeneration keeps it."""
    if not SUITE.exists():
        return None
    return parse_kvx(SUITE.read_text(encoding="utf-8")).get("suite", {}).get("cassette_digest")


def write_suite(tasks, results, parse_kvx, source="authoring-model"):
    origin = {
        "authoring-model": [
            "# primitive_count values come from the authoring model in _authoring/refsim.py, not",
            "# from a kernel ledger; CWR-015 must recount them from replay-mode ledgers.",
        ],
        "kernel-ledger": [
            "# primitive_count, turns and reuse_positions are measured from the ledgers of replay-mode",
            "# runs of the reference solutions on the real kernel by tests/cwr/cases/4_2.py",
            "# (_authoring/kernel_counts.json); the authoring model is only a pre-check.",
        ],
    }[source]
    status = {
        "authoring-model": "authored; not executed on a kernel; CWR-015 UNRUN",
        "kernel-ledger": "counts measured on the kernel in replay mode; qualification is CWR-015",
    }[source]
    lines = [
        *HEADER.splitlines(),
        "# CWR task suite (task 4.2, clauses 11.2 and 11.3). Generated by",
        "# benchmark/tasks/_authoring/build_suite.py; do not edit by hand.",
        *origin,
        "",
        "[meta]",
        kv_line("format", "cwr.suite"),
        kv_line("schema_version", 1),
        kv_line("status", status),
        kv_line("generated_by", "benchmark/tasks/_authoring/build_suite.py"),
        "",
        "[suite]",
        kv_line("task_count", len(tasks)),
        kv_line(
            "count_basis",
            "ledger records of kind primitive or adapter in one run of the reference solution",
        ),
        kv_line("count_source", source),
        kv_line(
            "rule_reuse_definition",
            "reused primitive first called in the first quarter of "
            "the reference calls, not called in the middle half, called again in the last "
            "quarter at least ten calls later",
        ),
        kv_line(
            "rule_id_grammar", "<module>.<primitive>[<N>|rate|key|shape|clock|stream][:<CODE>]"
        ),
        kv_line("turn_limit", MAX_TURNS),
        kv_line("suite_digest", common.suite_digest(TASKS_ROOT)),
        kv_line(
            "suite_digest_scope",
            "every file of every benchmark/tasks/tNNN-slug directory; "
            "paths relative to benchmark/tasks; layout of spec/cwr/compiler.md section 7",
        ),
        kv_line("solutions_digest", common.tree_digest(SOLUTIONS)),
        kv_line("predicates_common_digest", common.tree_digest_file(COMMON)),
        kv_line("cassette_digest", existing_cassette_digest(parse_kvx)),
        kv_line(
            "cassette_digest_note",
            "frozen by task 4.1 after recording the reference "
            "solutions against the stub servers; null until then",
        ),
        "",
    ]
    for name, (low, high) in BINS.items():
        members = [t.id for t in tasks if t.bin == name]
        lines += [
            f"[bin.{name}]",
            kv_line("min_primitives", low),
            kv_line("max_primitives", high),
            kv_line("count", len(members)),
            kv_line("rule_reuse_tasks", sum(1 for t in tasks if t.bin == name and t.reused_rule)),
            kv_line("tasks", members),
            "",
        ]
    for task in tasks:
        result = results[task.id]
        lines += [
            f"[task.{task.id}]",
            kv_line("bin", task.bin),
            kv_line("primitive_count", result["count"]),
            kv_line("turns", result["turns"]),
            kv_line("rules", task.rules),
            kv_line("rule_reuse", task.reused_rule is not None),
            kv_line("reused_rule", task.reused_rule),
            kv_line("reuse_positions", result["reuse"]),
            kv_line("digest", common.tree_digest(TASKS_ROOT / task.id, TASKS_ROOT)),
            kv_line("solution", f"benchmark/solutions/{task.id}.py"),
            "",
        ]
    text = "\n".join(lines)
    parse_kvx(text)
    return text


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--check",
        action="store_true",
        help="do not write; fail if any generated file differs from disk",
    )
    parser.add_argument(
        "--kernel-counts",
        help="import counts written by `tests/cwr/cases/4_2.py --write-counts` into "
        "_authoring/kernel_counts.json before generating",
    )
    args = parser.parse_args()
    if args.kernel_counts and not args.check:
        imported = json.loads(Path(args.kernel_counts).read_text(encoding="utf-8"))
        KERNEL_COUNTS.write_text(json.dumps(imported, sort_keys=True, indent=1) + "\n")
    kernel = (
        json.loads(KERNEL_COUNTS.read_text(encoding="utf-8")) if KERNEL_COUNTS.exists() else None
    )
    source = "authoring-model" if kernel is None else "kernel-ledger"
    parse_kvx = load_validator()
    tasks = all_tasks()
    problems = []
    ids = [t.id for t in tasks]
    if len(set(ids)) != len(ids):
        problems.append("duplicate task ids")
    for t in tasks:
        if not common.TASK_DIR_RE.match(t.id):
            problems.append(f"{t.id}: id must match tNNN-slug")
    results = {}
    for task in tasks:
        try:
            result = check_task(task, problems)
        except Exception as error:  # report every broken task, not just the first
            problems.append(f"{task.id}: {type(error).__name__}: {error}")
            result = None
        if result and kernel is not None:
            measured = kernel.get(task.id)
            low, high = BINS[task.bin]
            if measured is None:
                problems.append(f"{task.id}: no kernel count in {KERNEL_COUNTS.name}")
                result = None
            elif not low <= measured["count"] <= high:
                problems.append(f"{task.id}: kernel count {measured['count']} outside {task.bin}")
            elif task.reused_rule and measured.get("reuse") is None:
                problems.append(f"{task.id}: no kernel reuse positions")
            else:
                result = {k: measured[k] for k in ("count", "turns", "reuse")}
        if result:
            results[task.id] = result
    counts = {b: sum(1 for t in tasks if t.bin == b) for b in BINS}
    reuse = sum(1 for t in tasks if t.reused_rule and t.bin == "long")
    if problems:
        print("\n".join(problems))
        return 1
    if counts != {b: PER_BIN for b in BINS} or reuse < MIN_REUSE:
        print(f"suite shape incomplete: bins {counts}, long rule-reuse tasks {reuse}")
        shape_ok = False
    else:
        shape_ok = True
    stale = []
    for task in tasks:
        r = results[task.id]
        for path, text in render(task, r["count"], r["turns"], source).items():
            if path.suffix == ".kvx":
                parse_kvx(text)
            if args.check:
                if not path.exists() or path.read_text(encoding="utf-8") != text:
                    stale.append(str(path.relative_to(REPO)))
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
    known = {t.id for t in tasks}
    for directory in common.task_dirs(TASKS_ROOT):
        if directory.name not in known:
            stale.append(f"orphan task directory {directory.name}")
    for loaded in common.task_dirs(TASKS_ROOT):
        predicate = common.load_predicate(loaded)
        if not callable(predicate):
            stale.append(f"{loaded.name}: success not callable")
    suite_text = write_suite(tasks, results, parse_kvx, source)
    if args.check:
        if not SUITE.exists() or SUITE.read_text(encoding="utf-8") != suite_text:
            stale.append("benchmark/suite.kvx")
    else:
        SUITE.write_text(suite_text, encoding="utf-8")
    for kvx in [*sorted(TASKS_ROOT.glob("t*/**/*.kvx")), SUITE]:
        if kvx.exists():
            parse_kvx(kvx.read_text(encoding="utf-8"))
    if stale:
        print("stale or missing generated files:\n  " + "\n  ".join(stale))
        return 1
    for name in BINS:
        members = [results[t.id]["count"] for t in tasks if t.bin == name]
        if members:
            print(f"{name:6s} {len(members):2d} tasks, primitives {min(members)}..{max(members)}")
    print(
        f"rule-reuse long tasks: {reuse}; all checks passed on the authoring model; counts from {source}"
        + ("" if shape_ok else " (suite shape incomplete)")
    )
    return 0 if shape_ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
