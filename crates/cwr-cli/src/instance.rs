// Copyright Sidiora Labs Inc. CWR® 2026-2027
// SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0

//! `cwr instance`: the Instance store, ledger, clock, RNG and snapshots (task 2.1;
//! spec/cwr/kernel.md sections 2, 6, 8 and 9; model-surface.md section 6).
//!
//! ```text
//! cwr instance create (--build <dir> | <build-hash|dir>) [--seed N] [--tail K|full] [--arm L]
//!                     [--model M] [--from-snapshot S] [--initial <state.json>] [--allow-experimental]
//!                                             prints the new Instance id; 0, 2 hash mismatch,
//!                                             3 experimental Build without the flag
//! cwr instance snapshot <id>                  prints the snapshot hash sha256:<hex>
//! cwr instance restore <id> <snapshot>        prints the recovery record; 0, 2 hash mismatch
//! cwr instance destroy <id>                   seals and retains the ledger, releases state
//! cwr instance show <id>                      instance metadata as JSON
//! cwr instance state <id>                     world state as canonical JSON
//! cwr instance ledger <id>                    strict ledger check (seq, chain, truncation)
//! cwr instance tail <id>                      the model-facing ledger tail, K lines
//! cwr instance probe <id> <call>...           one kernel-direct turn over world.now, world.version,
//!                                             world.seed, world.limits, world.random and
//!                                             world.history; a call is `name` or `name:<json params>`;
//!                                             prints one JSON line per call once its ledger record
//!                                             is durable, then the turn result
//! ```
//!
//! Global options: `--home <dir>` (default `$CWR_HOME`, else `.cwr`) holds `instances/`;
//! `--builds <dir>` (repeatable; default `build` and `xbuild`) is where a Build hash resolves.
//! Exit 1 on usage and any other error.

use std::io::Write;
use std::path::PathBuf;

use cwr_kernel::{Build, CreateOptions, Instance, KernelError, Ledger, ProbeCall, TailSize};
use serde_json::{json, Value};

/// One-line help text for `cwr instance`, shown by `cwr help`.
pub const SUMMARY: &str = "create, snapshot, restore, destroy, inspect and probe Instances";

const USAGE: &str = "usage:
  cwr instance create (--build <dir> | <build-hash|dir>) [--seed N] [--tail K|full] [--arm L] [--model M]
                      [--from-snapshot S] [--initial <state.json>] [--allow-experimental]
  cwr instance snapshot <id>
  cwr instance restore <id> <snapshot>
  cwr instance destroy <id>
  cwr instance show|state|ledger|tail <id>
  cwr instance probe <id> <call>...
  cwr instance context <id> [--tail K|full] [--json]
  cwr instance upgrade <id> (<build-hash|dir> | --build <dir>) [--allow-experimental]
  cwr instance notice|outbox <id>
global options: --home <dir>, --builds <dir>";

fn usage_error(message: &str) -> i32 {
    eprintln!("cwr instance: {message}");
    eprintln!("{USAGE}");
    1
}

fn fail(e: KernelError) -> i32 {
    eprintln!("cwr instance: {e}");
    e.exit_code()
}

struct Args {
    home: PathBuf,
    builds: Vec<PathBuf>,
    positional: Vec<String>,
    options: Vec<(String, Option<String>)>,
}

const VALUE_OPTIONS: [&str; 7] = [
    "--build",
    "--seed",
    "--tail",
    "--arm",
    "--model",
    "--from-snapshot",
    "--initial",
];
const FLAG_OPTIONS: [&str; 1] = ["--allow-experimental"];

fn parse(args: &[String]) -> Result<Args, String> {
    let mut home = std::env::var_os("CWR_HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from(".cwr"));
    let mut builds = Vec::new();
    let mut positional = Vec::new();
    let mut options = Vec::new();
    let mut it = args.iter();
    while let Some(a) = it.next() {
        match a.as_str() {
            "--home" => home = PathBuf::from(it.next().ok_or("--home needs a directory")?),
            "--builds" => builds.push(PathBuf::from(
                it.next().ok_or("--builds needs a directory")?,
            )),
            o if VALUE_OPTIONS.contains(&o) => {
                let v = it.next().ok_or_else(|| format!("{o} needs a value"))?;
                if options.iter().any(|(k, _)| k == o) {
                    return Err(format!("{o} given twice"));
                }
                options.push((o.to_string(), Some(v.clone())));
            }
            o if FLAG_OPTIONS.contains(&o) => options.push((o.to_string(), None)),
            o if o.starts_with("--") => return Err(format!("unknown option {o}")),
            _ => positional.push(a.clone()),
        }
    }
    if builds.is_empty() {
        builds = vec![PathBuf::from("build"), PathBuf::from("xbuild")];
    }
    Ok(Args {
        home,
        builds,
        positional,
        options,
    })
}

impl Args {
    fn opt(&self, k: &str) -> Option<&str> {
        self.options
            .iter()
            .find(|(n, _)| n == k)
            .and_then(|(_, v)| v.as_deref())
    }
    fn flag(&self, k: &str) -> bool {
        self.options.iter().any(|(n, _)| n == k)
    }
    fn only(&self, allowed: &[&str]) -> Result<(), String> {
        match self
            .options
            .iter()
            .find(|(n, _)| !allowed.contains(&n.as_str()))
        {
            Some((n, _)) => Err(format!("option {n} does not apply here")),
            None => Ok(()),
        }
    }
}

fn print_json(v: &Value) {
    let mut out = std::io::stdout().lock();
    let _ = writeln!(out, "{v}");
    let _ = out.flush();
}

fn create(a: &Args) -> i32 {
    if let Err(m) = a.only(&[
        "--build",
        "--seed",
        "--tail",
        "--arm",
        "--model",
        "--from-snapshot",
        "--initial",
        "--allow-experimental",
    ]) {
        return usage_error(&m);
    }
    let reference = match (a.opt("--build"), a.positional.as_slice()) {
        (Some(b), []) => b.to_string(),
        (None, [b]) => b.clone(),
        _ => {
            return usage_error("create takes exactly one Build: --build <dir> or <build-hash|dir>")
        }
    };
    let build_dir = match Build::resolve(&reference, &a.builds) {
        Ok(d) => d,
        Err(e) => return fail(e),
    };
    let seed = match a.opt("--seed").map(str::parse::<u64>) {
        None => None,
        Some(Ok(s)) => Some(s),
        Some(Err(_)) => {
            return usage_error("--seed must be an integer from 0 to 18446744073709551615")
        }
    };
    let tail = match a.opt("--tail").map(str::parse::<TailSize>) {
        None => None,
        Some(Ok(t)) => Some(t),
        Some(Err(e)) => return usage_error(&e.to_string()),
    };
    let initial = match a.opt("--initial") {
        None => None,
        Some(p) => match std::fs::read(p)
            .map_err(|e| e.to_string())
            .and_then(|b| serde_json::from_slice::<Value>(&b).map_err(|e| e.to_string()))
        {
            Ok(v) => Some(v),
            Err(m) => return usage_error(&format!("--initial {p}: {m}")),
        },
    };
    let opts = CreateOptions {
        seed,
        tail,
        arm: a.opt("--arm").map(str::to_string),
        model: a.opt("--model").map(str::to_string),
        from_snapshot: a.opt("--from-snapshot").map(str::to_string),
        initial,
        allow_experimental: a.flag("--allow-experimental"),
    };
    match Instance::create(&a.home, &build_dir, opts) {
        Ok(inst) => {
            println!("{}", inst.meta.id);
            0
        }
        Err(e) => fail(e),
    }
}

fn parse_call(s: &str) -> Result<ProbeCall, String> {
    let (name, params) = match s.split_once(':') {
        Some((n, p)) => (
            n,
            serde_json::from_str::<Value>(p)
                .map_err(|e| format!("call {s:?}: params are not JSON: {e}"))?,
        ),
        None => (s, json!({})),
    };
    Ok(ProbeCall {
        primitive: name.to_string(),
        params,
    })
}

/// Runs `cwr instance <command>`; `args` follow `instance`. `turn`, `replay`, `recover` and
/// `compare` are delegated to their modules. Returns 0 on success, the kernel error's exit code
/// on a kernel failure and 1 on a usage error.
pub fn run(args: &[String]) -> i32 {
    let Some(cmd) = args.first().map(String::as_str) else {
        return usage_error("missing instance command");
    };
    if cmd == "turn" {
        return crate::turn::run(&args[1..]);
    }
    if cmd == "context" {
        return crate::context::run(&args[1..]);
    }
    if matches!(cmd, "replay" | "recover" | "compare") {
        return crate::replay::run(cmd, &args[1..]);
    }
    if matches!(cmd, "upgrade" | "notice" | "outbox") {
        return crate::upgrade::run(cmd, &args[1..]);
    }
    let a = match parse(&args[1..]) {
        Ok(a) => a,
        Err(m) => return usage_error(&m),
    };
    if cmd == "create" {
        return create(&a);
    }
    if let Err(m) = a.only(&[]) {
        return usage_error(&m);
    }
    let Some(id) = a.positional.first().cloned() else {
        return usage_error(&format!("{cmd} needs an <id>"));
    };
    let rest = &a.positional[1..];
    let one = |n: usize| rest.len() == n;
    match cmd {
        "show" if one(0) => match Instance::read_meta(&a.home, &id) {
            Ok(m) => {
                println!("{:#}", m.to_json());
                0
            }
            Err(e) => fail(e),
        },
        "state" if one(0) => match Instance::open(&a.home, &id) {
            Ok(i) => {
                println!("{}", cwr_kernel::canonical_json(&i.state.to_json()));
                0
            }
            Err(e) => fail(e),
        },
        "ledger" if one(0) => {
            match Ledger::scan(&Instance::dir_of(&a.home, &id).join("ledger.jsonl")) {
                Ok(s) => {
                    print_json(
                        &json!({"ok": true, "records": s.records.len(), "last_seq": s.last_seq(), "last_chain": s.last_chain()}),
                    );
                    0
                }
                Err(e) => fail(e),
            }
        }
        "tail" if one(0) => match Instance::open(&a.home, &id) {
            Ok(i) => {
                print!("{}", i.tail());
                0
            }
            Err(e) => fail(e),
        },
        "probe" if !rest.is_empty() => {
            let calls: Result<Vec<ProbeCall>, String> =
                rest.iter().map(|s| parse_call(s)).collect();
            let calls = match calls {
                Ok(c) => c,
                Err(m) => return usage_error(&m),
            };
            let mut inst = match Instance::open(&a.home, &id) {
                Ok(i) => i,
                Err(e) => return fail(e),
            };
            match inst.probe(&calls, print_json) {
                Ok(result) => {
                    print_json(&result);
                    0
                }
                Err(e) => fail(e),
            }
        }
        "snapshot" if one(0) => match Instance::open(&a.home, &id).and_then(|mut i| i.snapshot()) {
            Ok(h) => {
                println!("{h}");
                0
            }
            Err(e) => fail(e),
        },
        "restore" if one(1) => match Instance::restore(&a.home, &id, &rest[0]) {
            Ok((_, rec)) => {
                print_json(&rec);
                0
            }
            Err(e) => fail(e),
        },
        "destroy" if one(0) => match Instance::destroy(&a.home, &id) {
            Ok(m) => {
                println!("destroyed {} (ledger sealed and retained)", m.id);
                0
            }
            Err(e) => fail(e),
        },
        "show" | "state" | "ledger" | "tail" | "probe" | "snapshot" | "restore" | "destroy" => {
            usage_error(&format!("wrong arguments for {cmd}"))
        }
        other => usage_error(&format!("unknown instance command {other:?}")),
    }
}
