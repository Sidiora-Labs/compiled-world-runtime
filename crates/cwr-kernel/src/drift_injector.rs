// Copyright Sidiora Labs Inc. CWR® 2026-2027
// SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0

//! The drift injector of an `--x-drift` Build (compiler.md section 10; clauses 10.1 and 10.2;
//! task 3.2, case CWR-013).
//!
//! An `--x-drift` Build carries its drift schedule byte-copied at [`SCHEDULE_PATH`] inside the
//! Build hash; `contracts.json`, the manual and the SDK are those of the control Build. Before a
//! turn opens, [`prepare`] applies every schedule entry whose `turn` is at or before that turn to
//! the turn's running enforcement table, in (turn, entry number) order; once the turn is open and
//! before its script starts, [`Injection::record`] appends one ledger record of kind `drift` for
//! each entry whose `turn` is this turn. The table is rebuilt from `contracts.json` every turn, so
//! an injected rule applies from its first turn onwards and never touches the Build.
//!
//! How each change kind alters one contract ([`apply`]); a drift entry carries one expression and
//! one envelope code, so every kind is a precondition-level change except `effect_changed`:
//!
//! | `change` | Effect on the named primitive or adapter |
//! |---|---|
//! | `precondition_added` | `[expr, failure]` is appended after the declared preconditions |
//! | `limit_changed` | as `precondition_added`; `failure` must be `LIMIT` |
//! | `adapter_changed` | as `precondition_added`; the target must be an adapter |
//! | `precondition_removed` | the precondition whose expression is `expr` and whose code is `failure` is removed |
//! | `precondition_changed` | the first precondition whose code is `failure` is replaced by `expr` |
//! | `failure_changed` | the precondition whose expression is `expr` raises `failure` instead |
//! | `effect_changed` | the declared effects become `[expr]` (asserted under `--debug-effects`) |
//!
//! Expressions are compared by parse tree, so spacing does not matter. An entry that cannot be
//! applied is refused; the compiler runs the same [`apply`] over the whole schedule before it
//! packages the Build, so a packaged schedule always applies.
//!
//! The drift grammar has no visibility key, so every drift record is written with
//! `visible = false`: the tail and `world.history` omit it, and the model's first evidence of a
//! drift is a failure the manual does not predict (model-surface.md section 8).

use std::fs;

use cwr_contract::kvx::{self, Json};
use cwr_contract::precondition::{self as pexpr, Expr};
use cwr_contract::{ChangeKind, Code};
use serde_json::json;

use crate::contract_check::{Contract, ContractTable};
use crate::ledger::{Kind, RecordDraft};
use crate::state::{Build, Instance};
use crate::{sha256_hex, KernelError, Result};

/// The experimental flag name an `--x-drift` Build records in `manifest.kvx` and every ledger
/// record.
pub const FLAG: &str = "x-drift";

/// Where an `--x-drift` Build carries its drift schedule, relative to the Build directory.
pub const SCHEDULE_PATH: &str = "fixtures/x-drift.kvx";

/// One `[drift.<n>]` entry as the injector applies it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DriftEntry {
    /// The entry number `n`.
    pub n: u64,
    /// First turn at which the injected rule applies.
    pub turn: u64,
    /// The primitive or adapter whose rule changes.
    pub primitive: String,
    /// The change kind; always one that applies to a running table.
    pub change: ChangeKind,
    /// The expression source.
    pub expr: String,
    /// The expression's parse tree.
    pub ast: Expr,
    /// The envelope code the injected rule raises.
    pub failure: Code,
}

/// A parsed drift schedule, entries sorted by (turn, n).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Schedule {
    /// The entries in application order.
    pub entries: Vec<DriftEntry>,
    /// `sha256:<hex>` of the schedule file bytes.
    pub digest: String,
}

fn str_key(section: &kvx::Section, key: &str) -> std::result::Result<String, String> {
    match section.get(key).map(|e| &e.value.value) {
        Some(Json::Str(s)) => Ok(s.clone()),
        Some(_) => Err(format!("[{}] {key} must be a string", section.name)),
        None => Err(format!("[{}] lacks {key}", section.name)),
    }
}

impl Schedule {
    /// Parses drift schedule bytes (`meta.format = "cwr.drift"`, `[drift.<n>]` sections with
    /// `turn`, `primitive`, `change`, `expr`, `failure` and `doc`). Refuses anything the grammar
    /// of compiler.md section 10 does not allow, with a message naming the section and key.
    pub fn parse(bytes: &[u8]) -> std::result::Result<Schedule, String> {
        let text = std::str::from_utf8(bytes).map_err(|_| "the schedule is not UTF-8")?;
        let doc = kvx::parse(SCHEDULE_PATH, text).map_err(|errs| {
            errs.iter()
                .map(|e| e.to_string())
                .collect::<Vec<_>>()
                .join("; ")
        })?;
        let format = doc.section("meta").map(|m| str_key(m, "format"));
        if !matches!(format, Some(Ok(ref f)) if f == "cwr.drift") {
            return Err("[meta] format must be \"cwr.drift\"".into());
        }
        let mut entries = Vec::new();
        for section in doc.sections.iter().filter(|s| s.name != "meta") {
            let n = section
                .name
                .strip_prefix("drift.")
                .and_then(|s| s.parse::<u64>().ok())
                .filter(|n| *n > 0)
                .ok_or_else(|| format!("unknown section [{}]", section.name))?;
            let turn = match section.get("turn").map(|e| &e.value.value) {
                Some(Json::Int(t)) if *t > 0 => *t as u64,
                _ => {
                    return Err(format!(
                        "[{}] turn must be a positive integer",
                        section.name
                    ))
                }
            };
            let change_text = str_key(section, "change")?;
            let change = ChangeKind::parse(&change_text)
                .filter(|k| k.drift_applicable())
                .ok_or_else(|| {
                    format!(
                        "[{}] change {change_text:?} cannot be applied to a running table",
                        section.name
                    )
                })?;
            let expr = str_key(section, "expr")?;
            let ast = pexpr::parse(&expr)
                .map_err(|e| format!("[{}] expr {expr:?}: {e}", section.name))?;
            let failure_text = str_key(section, "failure")?;
            let failure = failure_text.parse::<Code>().map_err(|_| {
                format!(
                    "[{}] failure {failure_text:?} is outside the envelope",
                    section.name
                )
            })?;
            str_key(section, "doc")?;
            entries.push(DriftEntry {
                n,
                turn,
                primitive: str_key(section, "primitive")?,
                change,
                expr,
                ast,
                failure,
            });
        }
        entries.sort_by_key(|e| (e.turn, e.n));
        Ok(Schedule {
            entries,
            digest: format!("sha256:{}", sha256_hex(bytes)),
        })
    }

    /// The schedule of a loaded Build: `None` for a Build that is not an `--x-drift` Build; an
    /// error when an `--x-drift` Build lacks a schedule or carries one that does not parse.
    pub fn of_build(build: &Build) -> Result<Option<Schedule>> {
        if build.experimental.as_deref() != Some(FLAG) {
            return Ok(None);
        }
        let path = build.dir.join(SCHEDULE_PATH);
        let bytes = fs::read(&path).map_err(|e| {
            KernelError::Refused(format!(
                "Build {} is an {FLAG} Build without {SCHEDULE_PATH}: {e}",
                build.hash
            ))
        })?;
        Schedule::parse(&bytes).map(Some).map_err(|m| {
            KernelError::Refused(format!("Build {}: {SCHEDULE_PATH}: {m}", build.hash))
        })
    }
}

fn position(contract: &Contract, f: impl Fn(&(String, Expr, Code)) -> bool) -> Option<usize> {
    contract.preconditions.iter().position(f)
}

/// Applies one drift entry to one contract of a running enforcement table, by the table in the
/// module documentation. Returns why the entry cannot apply, without changing the contract.
pub fn apply(contract: &mut Contract, entry: &DriftEntry) -> std::result::Result<(), String> {
    let added = (entry.expr.clone(), entry.ast.clone(), entry.failure);
    let target = &entry.primitive;
    match entry.change {
        ChangeKind::PreconditionAdded => contract.preconditions.push(added),
        ChangeKind::LimitChanged if entry.failure == Code::Limit => {
            contract.preconditions.push(added)
        }
        ChangeKind::LimitChanged => {
            return Err(format!(
                "limit_changed on {target} must raise LIMIT, not {}",
                entry.failure.as_str()
            ))
        }
        ChangeKind::AdapterChanged if contract.is_adapter => contract.preconditions.push(added),
        ChangeKind::AdapterChanged => {
            return Err(format!(
                "adapter_changed names {target}, which is not an adapter"
            ))
        }
        ChangeKind::PreconditionRemoved => {
            let i = position(contract, |(_, ast, code)| {
                *ast == entry.ast && *code == entry.failure
            })
            .ok_or_else(|| {
                format!(
                    "precondition_removed: {target} has no precondition [{:?}, {}]",
                    entry.expr,
                    entry.failure.as_str()
                )
            })?;
            contract.preconditions.remove(i);
        }
        ChangeKind::PreconditionChanged => {
            let i = position(contract, |(_, _, code)| *code == entry.failure).ok_or_else(|| {
                format!(
                    "precondition_changed: {target} has no precondition raising {}",
                    entry.failure.as_str()
                )
            })?;
            contract.preconditions[i] = added;
        }
        ChangeKind::FailureChanged => {
            let i = position(contract, |(_, ast, _)| *ast == entry.ast).ok_or_else(|| {
                format!(
                    "failure_changed: {target} has no precondition {:?}",
                    entry.expr
                )
            })?;
            contract.preconditions[i].2 = entry.failure;
        }
        ChangeKind::EffectChanged => {
            contract.effects = vec![(entry.expr.clone(), entry.ast.clone())];
        }
        other => {
            return Err(format!(
                "change kind {} cannot be applied to a running table",
                other.as_str()
            ))
        }
    }
    Ok(())
}

/// Applies every entry of `schedule` whose first turn is at or before `turn` to `table`.
/// Returns the entries whose first turn is exactly `turn` (the injections of this turn).
pub fn apply_through<'s>(
    table: &mut ContractTable,
    schedule: &'s Schedule,
    turn: u64,
) -> std::result::Result<Vec<&'s DriftEntry>, String> {
    let mut fired = Vec::new();
    for e in schedule.entries.iter().filter(|e| e.turn <= turn) {
        let contract = table.contracts.get_mut(&e.primitive).ok_or_else(|| {
            format!(
                "drift {} names {}, which this Build does not enumerate",
                e.n, e.primitive
            )
        })?;
        apply(contract, e).map_err(|m| format!("drift {}: {m}", e.n))?;
        if e.turn == turn {
            fired.push(e);
        }
    }
    Ok(fired)
}

/// The injections of one turn, computed before the turn opens and recorded once it has.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Injection {
    /// `sha256:<hex>` of the schedule, when the Build is an `--x-drift` Build.
    pub schedule: Option<String>,
    /// The entries whose first turn is this turn.
    pub fired: Vec<DriftEntry>,
}

/// First half of the injector, run before turn `turn` of an Instance on `build` opens: applies
/// the schedule to `table` and returns this turn's injections. A no-op returning an empty
/// [`Injection`] for a Build that is not an `--x-drift` Build. Nothing is written, so a refusal
/// leaves the Instance untouched.
pub fn prepare(build: &Build, table: &mut ContractTable, turn: u64) -> Result<Injection> {
    let Some(schedule) = Schedule::of_build(build)? else {
        return Ok(Injection::default());
    };
    let fired = apply_through(table, &schedule, turn)
        .map_err(|m| KernelError::Refused(format!("Build {}: {m}", build.hash)))?
        .into_iter()
        .cloned()
        .collect();
    Ok(Injection {
        schedule: Some(schedule.digest),
        fired,
    })
}

impl Injection {
    /// Second half of the injector, run once the turn is open and before its script starts:
    /// appends one durable `drift` record per injection, at the current world time. Returns the
    /// appended records.
    pub fn record(&self, inst: &mut Instance, turn: u64) -> Result<Vec<serde_json::Value>> {
        let mut records = Vec::new();
        for e in &self.fired {
            let mut draft = RecordDraft::new(Kind::Drift, turn, inst.clock.now())
                .with("drift", json!(e.n))
                .with("change", json!(e.change.as_str()))
                .with("expr", json!(e.expr))
                .with("failure", json!(e.failure.as_str()))
                .with("first_turn", json!(e.turn))
                .with("schedule", json!(self.schedule))
                .with("visible", json!(false));
            draft.primitive = Some(e.primitive.clone());
            records.push(inst.append(draft, None)?);
        }
        Ok(records)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::BTreeSet;

    fn contract(adapter: bool, pre: &[(&str, Code)]) -> Contract {
        Contract {
            name: "fs.delete".into(),
            module: "fs".into(),
            is_adapter: adapter,
            side_effect: "mutating".into(),
            params: Vec::new(),
            returns: "bool".parse().expect("type"),
            returns_text: "bool".into(),
            preconditions: pre
                .iter()
                .map(|(s, c)| (s.to_string(), pexpr::parse(s).expect("expr"), *c))
                .collect(),
            effects: Vec::new(),
            failures: BTreeSet::new(),
            content_guaranteed: true,
            raw: json!({}),
        }
    }

    fn schedule(body: &str) -> Schedule {
        let text = format!("[meta]\nformat = \"cwr.drift\"\nschema_version = 1\n\n{body}");
        Schedule::parse(text.as_bytes()).expect("schedule")
    }

    fn entry(change: &str, expr: &str, failure: &str) -> DriftEntry {
        schedule(&format!("[drift.1]\nturn = 3\nprimitive = \"fs.delete\"\nchange = \"{change}\"\nexpr = \"{expr}\"\nfailure = \"{failure}\"\ndoc = \"d\"\n"))
            .entries
            .remove(0)
    }

    const BASE: [(&str, Code); 2] = [
        ("fs.exists(path)", Code::NotFound),
        ("not fs.locked(path)", Code::Locked),
    ];

    fn codes(c: &Contract) -> Vec<(&str, Code)> {
        c.preconditions
            .iter()
            .map(|(s, _, k)| (s.as_str(), *k))
            .collect()
    }

    #[test]
    fn schedule_sorts_by_turn_then_number_and_digests_the_bytes() {
        let s = schedule("[drift.2]\nturn = 4\nprimitive = \"fs.delete\"\nchange = \"precondition_added\"\nexpr = \"fs.size(path) < 1\"\nfailure = \"LOCKED\"\ndoc = \"\"\n\n[drift.1]\nturn = 9\nprimitive = \"fs.delete\"\nchange = \"precondition_added\"\nexpr = \"fs.size(path) < 2\"\nfailure = \"LOCKED\"\ndoc = \"\"\n");
        assert_eq!(
            s.entries.iter().map(|e| (e.turn, e.n)).collect::<Vec<_>>(),
            vec![(4, 2), (9, 1)]
        );
        assert!(s.digest.starts_with("sha256:") && s.digest.len() == 71);
    }

    #[test]
    fn schedule_refuses_kinds_a_running_table_cannot_take() {
        for bad in ["state_shape", "primitive_added", "doc_only", "nonsense"] {
            let text = format!("[meta]\nformat = \"cwr.drift\"\n\n[drift.1]\nturn = 1\nprimitive = \"fs.delete\"\nchange = \"{bad}\"\nexpr = \"true\"\nfailure = \"LOCKED\"\ndoc = \"\"\n");
            assert!(Schedule::parse(text.as_bytes()).is_err(), "{bad}");
        }
        let zero = "[meta]\nformat = \"cwr.drift\"\n\n[drift.1]\nturn = 0\nprimitive = \"p\"\nchange = \"precondition_added\"\nexpr = \"true\"\nfailure = \"LOCKED\"\ndoc = \"\"\n";
        assert!(Schedule::parse(zero.as_bytes()).is_err());
        let code = zero
            .replace("turn = 0", "turn = 1")
            .replace("LOCKED", "BROKEN");
        assert!(Schedule::parse(code.as_bytes()).is_err());
    }

    #[test]
    fn added_appends_after_the_declared_preconditions() {
        let mut c = contract(false, &BASE);
        apply(
            &mut c,
            &entry("precondition_added", "fs.size(path) < 4096", "LOCKED"),
        )
        .unwrap();
        assert_eq!(codes(&c)[2], ("fs.size(path) < 4096", Code::Locked));
        assert_eq!(c.preconditions.len(), 3);
    }

    #[test]
    fn removed_changed_and_failure_changed_find_their_precondition() {
        let mut c = contract(false, &BASE);
        apply(
            &mut c,
            &entry("failure_changed", "not  fs.locked(path)", "CONFLICT"),
        )
        .unwrap();
        assert_eq!(codes(&c)[1], ("not fs.locked(path)", Code::Conflict));
        apply(
            &mut c,
            &entry("precondition_changed", "fs.size(path) > 0", "NOT_FOUND"),
        )
        .unwrap();
        assert_eq!(codes(&c)[0], ("fs.size(path) > 0", Code::NotFound));
        apply(
            &mut c,
            &entry("precondition_removed", "fs.size(path) > 0", "NOT_FOUND"),
        )
        .unwrap();
        assert_eq!(codes(&c), vec![("not fs.locked(path)", Code::Conflict)]);
    }

    #[test]
    fn an_entry_that_does_not_apply_is_refused_and_changes_nothing() {
        let mut c = contract(false, &BASE);
        let before = codes(&c)
            .into_iter()
            .map(|(s, k)| (s.to_string(), k))
            .collect::<Vec<_>>();
        for e in [
            entry("precondition_removed", "fs.exists(path)", "LOCKED"),
            entry("precondition_changed", "fs.size(path) > 2", "RATE_LIMITED"),
            entry("failure_changed", "fs.size(path) > 1", "LOCKED"),
            entry("limit_changed", "fs.size(path) < 9", "LOCKED"),
            entry("adapter_changed", "fs.size(path) < 9", "UNAVAILABLE"),
        ] {
            assert!(apply(&mut c, &e).is_err(), "{:?}", e.change);
        }
        let after = codes(&c)
            .into_iter()
            .map(|(s, k)| (s.to_string(), k))
            .collect::<Vec<_>>();
        assert_eq!(before, after);
        apply(
            &mut c,
            &entry("limit_changed", "fs.size(path) < 9", "LIMIT"),
        )
        .unwrap();
        let mut a = contract(true, &[]);
        apply(
            &mut a,
            &entry("adapter_changed", "fs.size(path) < 9", "UNAVAILABLE"),
        )
        .unwrap();
        assert_eq!(a.preconditions.len(), 1);
    }

    #[test]
    fn effect_changed_replaces_the_effects() {
        let mut c = contract(false, &BASE);
        apply(
            &mut c,
            &entry("effect_changed", "not fs.exists(path)", "INVALID"),
        )
        .unwrap();
        assert_eq!(c.effects.len(), 1);
        assert_eq!(c.effects[0].0, "not fs.exists(path)");
    }

    #[test]
    fn apply_through_is_cumulative_and_reports_only_this_turns_injections() {
        let s = schedule("[drift.1]\nturn = 2\nprimitive = \"fs.delete\"\nchange = \"precondition_added\"\nexpr = \"fs.size(path) < 4096\"\nfailure = \"LOCKED\"\ndoc = \"\"\n\n[drift.2]\nturn = 5\nprimitive = \"fs.delete\"\nchange = \"failure_changed\"\nexpr = \"fs.exists(path)\"\nfailure = \"CONFLICT\"\ndoc = \"\"\n");
        let table = |s: &Schedule, t: u64| {
            let mut tb = ContractTable {
                contracts: [("fs.delete".to_string(), contract(false, &BASE))].into(),
                records: crate::contract_check::Records(Default::default()),
                modules: vec!["fs".into()],
                runtime: "3.12".into(),
                lib: Default::default(),
            };
            let fired: Vec<u64> = apply_through(&mut tb, s, t)
                .unwrap()
                .iter()
                .map(|e| e.n)
                .collect();
            (codes(&tb.contracts["fs.delete"]).len(), fired, tb)
        };
        assert_eq!((table(&s, 1).0, table(&s, 1).1), (2, vec![]));
        assert_eq!((table(&s, 2).0, table(&s, 2).1), (3, vec![1]));
        assert_eq!((table(&s, 3).0, table(&s, 3).1), (3, vec![]));
        let (n, fired, tb) = table(&s, 5);
        assert_eq!((n, fired), (3, vec![2]));
        assert_eq!(tb.contracts["fs.delete"].preconditions[0].2, Code::Conflict);
        let mut missing = table(&s, 0).2;
        missing.contracts.clear();
        assert!(apply_through(&mut missing, &s, 2).is_err());
    }
}
