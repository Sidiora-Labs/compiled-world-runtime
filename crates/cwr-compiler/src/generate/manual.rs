// Copyright Sidiora Labs Inc. CWR® 2026-2027
// SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0

//! Manual generator (model-surface.md section 2; clauses 2.4, 2.5, 4.6 and 9.2; task 1.3).
//!
//! `manual.md` is a pure function of `contracts.json` and the `manual/` prose files: no clock,
//! host, path or environment value is read, iteration is in declared order (modules in
//! `world.modules` order, primitives in contract order, releases newest first) or sorted bytewise
//! (the index), line endings are `\n` and the file ends with exactly one newline.
//!
//! The ten sections, in order: 1 What this world is, 2 How a turn works, 3 The failure envelope,
//! 4 Limits and clock, 5 Modules overview, 6 Primitive reference, 7 Adapters, 8 Changes and
//! versions, 9 Proposals, 10 Index. Prose is merged at fixed positions: `intro.md` in section 1,
//! `conventions.md` and `examples.md` in section 2, `types.md` in section 5; its headings are
//! demoted below the section heading. Any other prose file is appended to section 2 in name order.
//!
//! Each primitive and adapter has exactly one entry: a `####` heading with its qualified name (the
//! index anchor) and a fenced `text` block in the rendered entry format:
//!
//! ```text
//! fs.delete(path: Path) -> None                                   [mutating]
//!   Remove the file at path. Refused when the path does not exist or is locked.
//!   Requires   1. fs.exists(path)              else NOT_FOUND
//!              2. not fs.locked(path)          else LOCKED
//!   Ensures    not fs.exists(path)
//! ```
//!
//! The Build hash cannot be printed inside a file the hash covers; section 1 prints the sha256 of
//! `contracts.json` and states that the Build hash is `world.build` in every result envelope.

use std::collections::BTreeMap;
use std::fmt::Write as _;

use serde_json::Value;

use super::sdk::python_name;

/// Bumped whenever the fixed wording or layout below changes; enters the compiler revision.
pub const GENERATOR_REVISION: &str = "manual-2";

/// Column at which the side-effect tag of an entry's signature line starts.
const TAG_COLUMN: usize = 64;

/// The three pillars the manual states, as `(title, text)` in rendering order.
pub const PILLARS: [(&str, &str); 3] = [
    (
        "Stability",
        "The rules of this Build are fixed for its whole life. The same primitive called with the same parameters under the same visible conditions produces the same outcome, every turn.",
    ),
    (
        "Direct action",
        "You act by writing one Python script per turn. Every primitive is a function you call directly, and one script can check, act, loop and recover in a single turn.",
    ),
    (
        "Controlled change",
        "Only the human owner changes this world, by recompiling it into a new version with a written changelog. You are told what changed on your first turn after the change.",
    ),
];

/// The determinism promise the manual states verbatim.
pub const DETERMINISM: &str = "The same primitive under the same visible conditions always produces the same outcome. Outcome means status, shape and side-effect class; it does not mean content.";

/// The label the manual attaches to adapters whose content is not deterministic.
pub const CONTENT_LABEL: &str = "content not guaranteed";

fn s<'a>(v: &'a Value, k: &str) -> &'a str {
    v.get(k).and_then(Value::as_str).unwrap_or("")
}

fn arr<'a>(v: &'a Value, k: &str) -> &'a [Value] {
    v.get(k)
        .and_then(Value::as_array)
        .map(Vec::as_slice)
        .unwrap_or(&[])
}

/// GitHub-style heading anchor of a qualified name: lowercase, keep alphanumerics, `-` and `_`.
pub fn anchor(name: &str) -> String {
    name.chars()
        .filter(|c| c.is_ascii_alphanumeric() || *c == '-' || *c == '_')
        .map(|c| c.to_ascii_lowercase())
        .collect()
}

/// Whether `world.seed` is part of this Build's surface (kernel.md section 6).
pub fn visible(contracts: &Value, name: &str) -> bool {
    !(name == "world.seed" && contracts["rng"]["seed_visible"] == Value::Bool(false))
}

/// Primitives and adapters of the surface in manual order.
fn entries<'a>(contracts: &'a Value, list: &str) -> Vec<&'a Value> {
    arr(contracts, list)
        .iter()
        .filter(|p| visible(contracts, s(p, "name")))
        .collect()
}

/// `name(p: T, ...) -> R`.
pub fn signature(p: &Value) -> String {
    let params: Vec<String> = arr(p, "params")
        .iter()
        .map(|pair| {
            format!(
                "{}: {}",
                pair[0].as_str().unwrap_or(""),
                pair[1].as_str().unwrap_or("")
            )
        })
        .collect();
    format!(
        "{}({}) -> {}",
        s(p, "name"),
        params.join(", "),
        s(p, "returns")
    )
}

/// The side-effect tag of an entry.
pub fn tag(p: &Value) -> String {
    if s(p, "kind") == "adapter" {
        format!(
            "[{}, idempotency: {}, timeout {} ms]",
            s(p, "side_effect"),
            s(p, "idempotency"),
            p["timeout_ms"].as_u64().unwrap_or(0)
        )
    } else {
        format!("[{}]", s(p, "side_effect"))
    }
}

/// One entry in the rendered entry format (without the surrounding fence).
pub fn entry(p: &Value) -> String {
    let mut out = String::new();
    let sig = signature(p);
    let pad = if sig.len() + 2 <= TAG_COLUMN {
        TAG_COLUMN - sig.len()
    } else {
        2
    };
    let _ = writeln!(out, "{sig}{}{}", " ".repeat(pad), tag(p));
    let _ = writeln!(out, "  {}", s(p, "doc"));
    let name = s(p, "name");
    let short = name.split_once('.').map(|x| x.1).unwrap_or(name);
    let py = python_name(short);
    if py != short {
        let module = name.split('.').next().unwrap_or("");
        let _ = writeln!(
            out,
            "  Python     called as {module}.{py}(...) because {short} is a Python keyword"
        );
    }
    let pre = arr(p, "preconditions");
    if pre.is_empty() {
        let _ = writeln!(out, "  Requires   nothing beyond the parameter types");
    } else {
        let width = pre
            .iter()
            .map(|x| x[0].as_str().unwrap_or("").len())
            .max()
            .unwrap_or(0);
        for (i, x) in pre.iter().enumerate() {
            let lead = if i == 0 {
                "  Requires   "
            } else {
                "             "
            };
            let e = x[0].as_str().unwrap_or("");
            let _ = writeln!(
                out,
                "{lead}{}. {e}{}  else {}",
                i + 1,
                " ".repeat(width - e.len()),
                x[1].as_str().unwrap_or("")
            );
        }
    }
    let effects = arr(p, "effects");
    if s(p, "kind") == "adapter" {
        let failures: Vec<&str> = arr(p, "failures")
            .iter()
            .filter_map(Value::as_str)
            .collect();
        let _ = writeln!(out, "  May fail   {}", failures.join(", "));
        let idem = match s(p, "idempotency") {
            "key" => "Idempotent by key: repeating the identical call with the same key returns the stored outcome; a different payload under the same key is DUPLICATE_KEY.",
            "natural" => "Naturally idempotent: equal requests are expected to have equal effects; repeating is safe.",
            _ => "Not idempotent: after TIMEOUT the call may or may not have had its effect.",
        };
        let _ = writeln!(out, "  {idem}");
        if p["content_guaranteed"] != Value::Bool(true) {
            let _ = writeln!(
                out,
                "  Content is not guaranteed: an ok result guarantees the {} shape and that one external call",
                s(p, "returns")
            );
            let _ = writeln!(
                out,
                "  was attempted, not what the service returned or stored."
            );
        }
    } else if effects.is_empty() {
        let _ = writeln!(out, "  Ensures    world state unchanged");
    } else {
        for (i, e) in effects.iter().enumerate() {
            let lead = if i == 0 {
                "  Ensures    "
            } else {
                "             "
            };
            let _ = writeln!(out, "{lead}{}", e.as_str().unwrap_or(""));
        }
    }
    out
}

/// Prose with every Markdown heading outside code fences demoted by `by` levels; trailing
/// whitespace of the file trimmed.
fn demote(text: &str, by: usize) -> String {
    let mut out = String::new();
    let mut fence = false;
    for line in text.trim_end().lines() {
        let t = line.trim_end();
        if t.starts_with("```") {
            fence = !fence;
        }
        if !fence && t.starts_with('#') && t.trim_start_matches('#').starts_with(' ') {
            out.push_str(&"#".repeat(by));
        }
        out.push_str(t);
        out.push('\n');
    }
    out
}

fn limit_text(name: &str) -> &'static str {
    match name {
        "cpu_ms" => "CPU milliseconds per turn script",
        "wall_ms" => "host wall-clock milliseconds per turn script",
        "memory_mb" => "resident memory of the turn script in MiB",
        "primitive_calls" => "primitive calls per turn, counted at the RPC boundary",
        "adapter_calls" => "external (adapter) calls per turn, counted at the gateway",
        _ => "declared limit",
    }
}

/// Renders `manual.md`.
pub fn render(
    contracts: &Value,
    contracts_sha256: &str,
    prose: &BTreeMap<String, String>,
) -> String {
    let world = &contracts["world"];
    let name = s(world, "name");
    let version = s(world, "version");
    let modules: Vec<&str> = arr(world, "modules")
        .iter()
        .filter_map(Value::as_str)
        .collect();
    let primitives = entries(contracts, "primitives");
    let adapters = entries(contracts, "adapters");
    let mut o = String::new();

    // 1
    let _ = writeln!(o, "# {name} {version}: world manual\n");
    let _ = writeln!(o, "## 1. What this world is\n");
    let _ = writeln!(o, "- World: {name}");
    let _ = writeln!(o, "- Version: {version}");
    let _ = writeln!(o, "- Contracts: sha256:{contracts_sha256}");
    let _ = writeln!(o, "- Build: the Build hash is reported as `world.build` in every result envelope; this manual is part of that hash.\n");
    if let Some(t) = prose.get("intro.md") {
        let _ = writeln!(o, "{}", demote(t, 2));
    }
    let _ = writeln!(o, "### The three pillars\n");
    for (i, (title, text)) in PILLARS.iter().enumerate() {
        let _ = writeln!(o, "{}. **{title}.** {text}", i + 1);
    }
    o.push('\n');

    // 2
    let _ = writeln!(o, "## 2. How a turn works\n");
    for line in [
        "- Each turn you reply with exactly one fenced `python` block. A reply with no python block or with more than one is refused as INVALID: nothing runs and world time does not advance.",
        "- The script runs in a fresh process that can import only this world's modules and the `cwr` package. A top-level `return` value becomes `returned` in the result envelope.",
        "- Every primitive call is atomic: it happens completely or not at all. A script is not atomic: when a call fails after earlier calls succeeded, the earlier effects stand.",
        "- A refused call raises the exception class of its code from `cwr` (for example `cwr.Locked`; `cwr.WorldError` catches every code). You may catch it and continue.",
        "- The turn ends with status `ok` (no uncaught exception), `error` (an uncaught exception or a limit, and nothing changed) or `partial` (an uncaught exception or a limit after at least one mutating or external call succeeded). The trace lists every call in order.",
        "- The per-turn limits in section 4 apply; the call that exceeds a count limit is not executed and fails with LIMIT naming the limit.",
        "- Your context each turn is this manual, a recompile notice when the world was just upgraded, the last ledger records and the last result envelope. `world.history` returns more of your ledger.",
    ] {
        let _ = writeln!(o, "{line}");
    }
    o.push('\n');
    for (file, title) in [
        ("conventions.md", "Conventions"),
        ("examples.md", "Worked examples"),
    ] {
        if let Some(t) = prose.get(file) {
            let _ = writeln!(o, "### {title}\n");
            let _ = writeln!(o, "{}", demote(t, 2));
        }
    }
    for (file, t) in prose {
        if !matches!(
            file.as_str(),
            "intro.md" | "conventions.md" | "examples.md" | "types.md"
        ) {
            let _ = writeln!(o, "### {}\n", file.trim_end_matches(".md"));
            let _ = writeln!(o, "{}", demote(t, 2));
        }
    }

    // 3
    let _ = writeln!(o, "## 3. The failure envelope\n");
    let _ = writeln!(o, "{DETERMINISM}\n");
    let codes = arr(contracts, "envelope");
    let _ = writeln!(o, "Every refusal carries exactly one of these {} codes; no other code exists in this world.\n", codes.len());
    let _ = writeln!(o, "| Code | Meaning | What to do |");
    let _ = writeln!(o, "|---|---|---|");
    for c in codes {
        let _ = writeln!(
            o,
            "| `{}` | {} | {} |",
            s(c, "code"),
            s(c, "meaning"),
            s(c, "guidance")
        );
    }
    o.push('\n');
    let _ = writeln!(o, "A failure names `code`, `primitive`, `precondition_index` (the one-based number of the first failing precondition, or null), `retry_after`, `limit` and `detail`. `retry_after` is present only for `RATE_LIMITED` and is an integer number of ticks to wait before calling the same adapter again. `limit` names the limit for `LIMIT`. `detail` is free text and is content, not a rule.\n");
    let _ = writeln!(o, "`INVARIANT_VIOLATION` is never an ordinary outcome: it means the world itself is defective, and the ledger keeps the full trace.\n");
    let _ = writeln!(o, "Results of external primitives are labelled `content_guaranteed: false` in the result envelope; see section 7.\n");

    // 4
    let _ = writeln!(o, "## 4. Limits and clock\n");
    let _ = writeln!(o, "| Limit | Value | Meaning |");
    let _ = writeln!(o, "|---|---|---|");
    for k in [
        "cpu_ms",
        "wall_ms",
        "memory_mb",
        "primitive_calls",
        "adapter_calls",
    ] {
        let v = &contracts["limits"][k];
        let _ = writeln!(o, "| `{k}` | {} | {} |", v, limit_text(k));
    }
    if let Some(m) = contracts["limits"].as_object() {
        for (k, v) in m {
            if !matches!(
                k.as_str(),
                "cpu_ms" | "wall_ms" | "memory_mb" | "primitive_calls" | "adapter_calls"
            ) {
                let _ = writeln!(o, "| `{k}` | {v} | {} |", limit_text(k));
            }
        }
    }
    o.push('\n');
    let tpp = &contracts["clock"]["tick_per_primitive"];
    let tpt = &contracts["clock"]["tick_per_turn"];
    let _ = writeln!(o, "Time in this world is ticks, never host time. `tick_per_primitive = {tpp}`: every executed primitive advances world time by {tpp}, whether it succeeds or an external call fails; a call refused by its parameters or preconditions does not. `tick_per_turn = {tpt}`: every executed turn adds {tpt} at its end. A refused turn adds nothing.\n");
    if contracts["rng"]["seed_visible"] == Value::Bool(true) {
        let _ = writeln!(o, "The random stream is seeded per Instance and advanced only by `world.random`. The seed is visible through `world.seed`.\n");
    } else {
        let _ = writeln!(o, "The random stream is seeded per Instance and advanced only by `world.random`. The seed is not visible in this Build.\n");
    }

    // 5
    let _ = writeln!(o, "## 5. Modules overview\n");
    for m in &modules {
        let ps: Vec<&Value> = primitives
            .iter()
            .copied()
            .filter(|p| s(p, "module") == *m)
            .collect();
        let ad: Vec<&Value> = adapters
            .iter()
            .copied()
            .filter(|p| s(p, "module") == *m)
            .collect();
        let _ = writeln!(o, "### Module {m}\n");
        let mut para = String::new();
        if !ps.is_empty() {
            let count = |k: &str| ps.iter().filter(|p| s(p, "side_effect") == k).count();
            let names: Vec<&str> = ps.iter().map(|p| s(p, "name")).collect();
            let _ = write!(
                para,
                "{} primitive{} ({} read_only, {} mutating): {}.",
                ps.len(),
                if ps.len() == 1 { "" } else { "s" },
                count("read_only"),
                count("mutating"),
                names.join(", ")
            );
        }
        if !ad.is_empty() {
            let names: Vec<&str> = ad.iter().map(|p| s(p, "name")).collect();
            if !para.is_empty() {
                para.push(' ');
            }
            let _ = write!(
                para,
                "{} external adapter{}, {CONTENT_LABEL}: {}.",
                ad.len(),
                if ad.len() == 1 { "" } else { "s" },
                names.join(", ")
            );
        }
        match contracts["state"].get(*m).and_then(Value::as_str) {
            Some(shape) if !shape.is_empty() => {
                let _ = write!(para, " State shape: `{shape}`.");
            }
            _ if !ad.is_empty() && ps.is_empty() => para.push_str(" It holds no world state; it reaches an external service through the adapter gateway."),
            _ => para.push_str(" It declares no typed state."),
        }
        let _ = writeln!(o, "{}\n", para.trim_start());
    }
    if let Some(t) = prose.get("types.md") {
        let _ = writeln!(o, "### Types\n");
        let _ = writeln!(o, "{}", demote(t, 2));
    }

    // 6
    let _ = writeln!(o, "## 6. Primitive reference\n");
    let _ = writeln!(o, "Preconditions are numbered in evaluation order; the number is the `precondition_index` a failure reports. Every precondition uses only parameters and read_only primitives you can call yourself.\n");
    for m in &modules {
        let ps: Vec<&Value> = primitives
            .iter()
            .copied()
            .filter(|p| s(p, "module") == *m)
            .collect();
        if ps.is_empty() {
            continue;
        }
        let _ = writeln!(o, "### Module {m} primitives\n");
        for p in ps {
            let _ = writeln!(o, "#### {}\n", s(p, "name"));
            let _ = writeln!(o, "```text\n{}```\n", entry(p));
        }
    }

    // 7
    let _ = writeln!(o, "## 7. Adapters\n");
    if adapters.is_empty() {
        let _ = writeln!(o, "This Build has no adapters.\n");
    } else {
        let _ = writeln!(o, "Adapters are external primitives. Their interface (parameters, result shape, declared failures) is a rule; their content is not guaranteed and every such result is labelled `{CONTENT_LABEL}`. The world never retries an external call.\n");
        for m in &modules {
            let ad: Vec<&Value> = adapters
                .iter()
                .copied()
                .filter(|p| s(p, "module") == *m)
                .collect();
            if ad.is_empty() {
                continue;
            }
            let _ = writeln!(o, "### Module {m} adapters\n");
            for p in ad {
                let _ = writeln!(o, "#### {}\n", s(p, "name"));
                let _ = writeln!(o, "```text\n{}```\n", entry(p));
            }
        }
    }

    // 8
    let _ = writeln!(o, "## 8. Changes and versions\n");
    let _ = writeln!(o, "Rules change only by recompilation into a new version: the human owner edits the world, recompiles it and moves you to the new Build, and the changes are listed here and in the recompile notice. Nothing changes the rules of this Build while you act.\n");
    for r in arr(contracts, "changelog") {
        let _ = writeln!(o, "### {} ({})\n", s(r, "version"), s(r, "date"));
        let _ = writeln!(o, "{}\n", s(r, "summary"));
        let changes = arr(r, "changes");
        for c in changes {
            let _ = writeln!(o, "- `{}` {}", s(c, "kind"), s(c, "target"));
            if !s(c, "before").is_empty() {
                let _ = writeln!(o, "  - before: {}", s(c, "before"));
            }
            let _ = writeln!(o, "  - after: {}", s(c, "after"));
            if !s(c, "migration").is_empty() {
                let _ = writeln!(o, "  - state migrated by {}", s(c, "migration"));
            }
        }
        if !changes.is_empty() {
            o.push('\n');
        }
    }

    // 9
    let _ = writeln!(o, "## 9. Proposals\n");
    if let Some(p) = primitives.iter().find(|p| s(p, "name") == "world.propose") {
        let params: Vec<&str> = arr(p, "params")
            .iter()
            .filter_map(|x| x[0].as_str())
            .collect();
        let call = format!(
            "world.propose({})",
            params
                .iter()
                .map(|n| format!("{n}=..."))
                .collect::<Vec<_>>()
                .join(", ")
        );
        let _ = writeln!(o, "To ask the human owner for a change, call `{call}` (entry in section 6). The text is recorded in your ledger and delivered outside the world. A proposal has no effect within this Build: no rule, limit or state changes because of it. A change, if the owner makes one, arrives only as a new version with a recompile notice.\n");
    } else {
        let _ = writeln!(o, "This Build has no `world.propose` primitive; there is no channel to the owner from inside this world.\n");
    }

    // 10
    let _ = writeln!(o, "## 10. Index\n");
    let mut index: Vec<(&str, &str)> = primitives
        .iter()
        .map(|p| (s(p, "name"), "section 6"))
        .chain(
            adapters
                .iter()
                .map(|p| (s(p, "name"), "section 7, adapter")),
        )
        .collect();
    index.sort_by(|a, b| a.0.as_bytes().cmp(b.0.as_bytes()));
    for (n, sec) in index {
        let _ = writeln!(o, "- [{n}](#{}): {sec}", anchor(n));
    }

    // Exactly one final newline.
    let mut out = o.trim_end().to_string();
    out.push('\n');
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn entry_format() {
        let p = json!({"name": "fs.delete", "kind": "primitive", "side_effect": "mutating", "params": [["path", "Path"]], "returns": "None",
            "preconditions": [["fs.exists(path)", "NOT_FOUND"], ["not fs.locked(path)", "LOCKED"]], "effects": ["not fs.exists(path)"],
            "doc": "Remove the file at path."});
        let e = entry(&p);
        let lines: Vec<&str> = e.lines().collect();
        assert!(lines[0].starts_with("fs.delete(path: Path) -> None "));
        assert_eq!(lines[0].find("[mutating]"), Some(TAG_COLUMN));
        assert_eq!(
            lines[2],
            "  Requires   1. fs.exists(path)      else NOT_FOUND"
        );
        assert_eq!(lines[3], "             2. not fs.locked(path)  else LOCKED");
        assert_eq!(lines[4], "  Ensures    not fs.exists(path)");
    }

    #[test]
    fn demote_skips_fences() {
        assert_eq!(
            demote("## A\n```\n# x\n```\n", 2),
            "#### A\n```\n# x\n```\n"
        );
    }

    #[test]
    fn anchors() {
        assert_eq!(anchor("kv.del"), "kvdel");
    }
}
