// Copyright Sidiora Labs Inc. CWR® 2026-2027
// SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0

//! `cwr instance`: the Instance store, ledger, clock, RNG and snapshots (task 2.1;
//! spec/cwr/kernel.md sections 2, 6, 8 and 9; model-surface.md section 6).
//!
//! ```text
//! cwr instance create (--build <dir> | <build-hash|dir>) [--seed N] [--tail K|full] [--arm L]
//!                     [--model M] [--from-snapshot S] [--initial <state.json>] [--allow-experimental]
//!                     [--cassette-digest sha256:<hex>]
//!                                             prints the new Instance id; 0, 2 hash mismatch,
//!                                             3 experimental Build without the flag
//! cwr instance snapshot <id>                  prints the snapshot hash sha256:<hex>
//! cwr instance restore <id> <snapshot>        prints the recovery record; 0, 2 hash mismatch
//! cwr instance destroy <id>                   seals and retains the ledger, releases state
//! cwr instance show <id>                      instance metadata as JSON
//! cwr instance state <id>                     world state as canonical JSON
//! cwr instance keys <id>                      the gateway's key idempotency store (Instance state
//!                                             outside the Build): one `done` entry per stored key
//! cwr instance ledger <id>                    strict ledger check (seq, chain, truncation)
//! cwr instance tail <id>                      the model-facing ledger tail, K lines
//! cwr instance probe <id> <call>...           one kernel-direct turn over world.now, world.version,
//!                                             world.seed, world.limits, world.random and
//!                                             world.history; a call is `name` or `name:<json params>`;
//!                                             prints one JSON line per call once its ledger record
//!                                             is durable, then the turn's result envelope. A probe
//!                                             is a real turn: it advances the turn counter and
//!                                             world time, draws from the random stream and appears
//!                                             in the model's ledger tail
//! cwr instance turn|replay|recover|compare    see turn.rs and replay.rs
//! cwr instance context                        see context.rs
//! cwr instance upgrade|notice|outbox          see upgrade.rs
//! ```
//!
//! Global options: `--home <dir>` (default `$CWR_HOME`, else `.cwr`) holds `instances/`;
//! `--builds <dir>` (repeatable; default `build` and `xbuild`) is where a Build hash resolves.
//! Exit 1 on usage and any other error.

use std::path::PathBuf;

use cwr_kernel::{Build, CreateOptions, Instance, KernelError, Ledger, ProbeCall, TailSize};
use serde_json::{json, Value};

use crate::cli::{self, Spec};

/// One-line help text for `cwr instance`, shown by `cwr help`.
pub const SUMMARY: &str =
    "create Instances, run turns, replay, snapshot, restore, recover, upgrade, inspect and destroy them";

const USAGE: &str = "usage:
  cwr instance create (--build <dir> | <build-hash|dir>) [--seed N] [--tail K|full] [--arm L] [--model M]
                      [--from-snapshot S] [--initial <state.json>] [--allow-experimental]
                      [--cassette-digest sha256:<hex>]
  cwr instance snapshot <id>
  cwr instance restore <id> <snapshot>
  cwr instance destroy <id>
  cwr instance show|state|keys|ledger|tail <id>
  cwr instance probe <id> <call>...
  cwr instance turn <id> [--script <file> | --message <file> | < input] [--debug-effects]
                    [--gateway live|record|replay] [--cassette <file>] [--cassette-label <label>]
                    [--stub NAME=URL]... [--diagnostic-no-language-guard]
  cwr instance replay (<id> | <ledger.jsonl>) [--into <new-id>] [--build <dir>] [--cassette <file>]
                      [--scripts <dir>] [--initial <state.json>] [--into-home <dir>] [--debug-effects]
  cwr instance recover <id>
  cwr instance compare <ledger-a> <ledger-b>
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
    parsed: cli::Parsed,
}

const SPEC: Spec = Spec {
    values: &[
        "--home",
        "--build",
        "--seed",
        "--tail",
        "--arm",
        "--model",
        "--from-snapshot",
        "--initial",
        "--cassette-digest",
    ],
    repeatable: &["--builds"],
    flags: &["--allow-experimental"],
};

/// Parses the arguments of an Instance command; the error is the usage message.
fn parse(args: &[String]) -> Result<Args, String> {
    let p = cli::parse(args, &SPEC)?;
    let mut builds: Vec<PathBuf> = p
        .values("--builds")
        .into_iter()
        .map(PathBuf::from)
        .collect();
    if builds.is_empty() {
        builds = vec![PathBuf::from("build"), PathBuf::from("xbuild")];
    }
    Ok(Args {
        home: p.home(),
        builds,
        positional: p.positional.clone(),
        parsed: p,
    })
}

impl Args {
    fn opt(&self, k: &str) -> Option<&str> {
        self.parsed.value(k)
    }
    fn flag(&self, k: &str) -> bool {
        self.parsed.flag(k)
    }
    /// The first option outside `allowed` (`--home` and `--builds` always apply).
    fn only(&self, allowed: &[&str]) -> Result<(), String> {
        let mut all = vec!["--home", "--builds"];
        all.extend_from_slice(allowed);
        self.parsed.only(&all)
    }
}

/// One JSON line on stdout; 0, or the exit code of an unwritable stdout.
fn print_json(v: &Value) -> i32 {
    cli::emit_line(&v.to_string())
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
        "--cassette-digest",
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
        cassette_digest: a.opt("--cassette-digest").map(str::to_string),
    };
    match Instance::create(&a.home, &build_dir, opts) {
        Ok(inst) => cli::emit_line(&inst.meta.id),
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
            Ok(m) => cli::emit_line(&format!("{:#}", m.to_json())),
            Err(e) => fail(e),
        },
        "state" if one(0) => match Instance::open(&a.home, &id) {
            Ok(i) => cli::emit_line(&cwr_kernel::canonical_json(&i.state.to_json())),
            Err(e) => fail(e),
        },
        "keys" if one(0) => match Instance::open(&a.home, &id).and_then(|i| {
            cwr_kernel::gateway::idempotency::KeyStore::read(
                &i.dir.join(cwr_kernel::gateway::GATEWAY_DIR),
            )
            .map_err(KernelError::Refused)
        }) {
            Ok(store) => cli::emit_line(&cwr_kernel::canonical_json(&Value::Array(
                store.done_events(),
            ))),
            Err(e) => fail(e),
        },
        "ledger" if one(0) => {
            match Instance::checked_dir(&a.home, &id)
                .and_then(|dir| Ledger::scan(&dir.join("ledger.jsonl")))
            {
                Ok(s) => print_json(
                    &json!({"ok": true, "records": s.records.len(), "last_seq": s.last_seq(), "last_chain": s.last_chain()}),
                ),
                Err(e) => fail(e),
            }
        }
        "tail" if one(0) => match Instance::open(&a.home, &id) {
            Ok(i) => cli::emit(&i.tail()),
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
            if let Err(e) = cwr_kernel::replay::record_probe(&inst, &calls) {
                return fail(e);
            }
            // A probe line that cannot be written does not stop the turn (its records are already
            // durable), but the command then reports the lost output instead of success.
            let mut lost = 0;
            let outcome = inst.probe(&calls, |v| {
                if lost == 0 {
                    lost = print_json(v);
                }
            });
            match outcome {
                Ok(result) if lost == 0 => print_json(&result),
                Ok(_) => lost,
                Err(e) => fail(e),
            }
        }
        "snapshot" if one(0) => match Instance::open(&a.home, &id).and_then(|mut i| i.snapshot()) {
            Ok(h) => cli::emit_line(&h),
            Err(e) => fail(e),
        },
        "restore" if one(1) => match Instance::restore(&a.home, &id, &rest[0]) {
            Ok((_, rec)) => print_json(&rec),
            Err(e) => fail(e),
        },
        "destroy" if one(0) => match cwr_kernel::recovery::needs_recovery(&a.home, &id)
            .and_then(|need| {
                if need {
                    cwr_kernel::recovery::recover(&a.home, &id).map(|_| ())
                } else {
                    Ok(())
                }
            })
            .and_then(|()| Instance::destroy(&a.home, &id))
        {
            Ok(m) => cli::emit_line(&format!("destroyed {} (ledger sealed and retained)", m.id)),
            Err(e) => fail(e),
        },
        "show" | "state" | "keys" | "ledger" | "tail" | "probe" | "snapshot" | "restore"
        | "destroy" => usage_error(&format!("wrong arguments for {cmd}")),
        other => usage_error(&format!("unknown instance command {other:?}")),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn args(v: &[&str]) -> Vec<String> {
        v.iter().map(|s| s.to_string()).collect()
    }

    #[test]
    fn create_options_parse_and_roots_repeat() {
        let a = parse(&args(&[
            "--build",
            "b",
            "--seed",
            "7",
            "--builds",
            "r1",
            "--builds",
            "r2",
            "--allow-experimental",
        ]))
        .unwrap();
        assert_eq!(a.opt("--build"), Some("b"));
        assert_eq!(a.opt("--seed"), Some("7"));
        assert!(a.flag("--allow-experimental"));
        assert_eq!(a.builds, vec![PathBuf::from("r1"), PathBuf::from("r2")]);
        assert!(a
            .only(&["--build", "--seed", "--allow-experimental"])
            .is_ok());
        assert!(a.only(&["--build"]).is_err());
    }

    #[test]
    fn repeated_options_are_usage_errors() {
        for bad in [
            &["--home", "a", "--home", "b"][..],
            &["--seed", "1", "--seed", "2"],
            &["--allow-experimental", "--allow-experimental"],
            &["--tail"],
            &["--nope"],
        ] {
            assert!(parse(&args(bad)).is_err(), "{bad:?}");
        }
    }

    #[test]
    fn wrong_commands_and_arity_exit_one() {
        assert_eq!(run(&args(&[])), 1);
        assert_eq!(run(&args(&["nope", "id"])), 1);
        assert_eq!(run(&args(&["show"])), 1);
        assert_eq!(run(&args(&["show", "a", "b"])), 1);
        assert_eq!(run(&args(&["restore", "a"])), 1);
        assert_eq!(run(&args(&["show", "a", "--seed", "1"])), 1);
        assert_eq!(run(&args(&["probe", "a", "world.now:{not json"])), 1);
    }

    #[test]
    fn probe_calls_parse_names_and_json_params() {
        let c = parse_call("world.random:{\"n\": 2}").unwrap();
        assert_eq!(c.primitive, "world.random");
        assert_eq!(c.params, json!({"n": 2}));
        assert_eq!(parse_call("world.now").unwrap().params, json!({}));
    }
}
