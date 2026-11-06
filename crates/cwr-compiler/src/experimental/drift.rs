// Copyright Sidiora Labs Inc. CWR® 2026-2027
// SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0

//! `--x-drift <file>`: validating and embedding a drift schedule (compiler.md section 10; clause
//! 10.2; task 3.2).
//!
//! The drift file is validated with the same checks as a contract by compiling it as part of the
//! World Source: the source is copied into a private staging directory beside the experimental
//! root with the drift file at [`STAGED_PATH`], and the ordinary pipeline runs over that copy. The
//! parse stage then refuses a malformed file, an unknown primitive or a kind a running table cannot
//! take (exit 2); resolve and lint refuse an expression outside the precondition language or one
//! that calls a primitive that is not read_only (CWR-V002, CWR-V001, CWR-F001). On top of those,
//! [`Staged::check`] applies every entry, in schedule order, to the compiled contract table with
//! the kernel's own [`cwr_kernel::drift_injector::apply`], so a packaged schedule always applies
//! at run time; a refusal there is a parse failure of the drift file (exit 2).
//!
//! Diagnostics are reported against the drift file's own path. The staged copy is removed when
//! the build ends. The schedule bytes are byte-copied to
//! [`cwr_kernel::drift_injector::SCHEDULE_PATH`]; drift files are not part of any other Build
//! file, so `contracts.json`, the manual and the SDK equal the control Build's.

use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::path::{Path, PathBuf};

use cwr_contract::precondition as pexpr;
use cwr_contract::{Code, TypeExpr};
use cwr_kernel::contract_check::{Contract, ContractTable, Records};
use cwr_kernel::drift_injector::{apply_through, Schedule, SCHEDULE_PATH};
use serde_json::{json, Value};

use super::diagnostic;
use crate::{Context, Diagnostic, Stage, StageRun};

/// Where the drift file sits inside the staged World Source.
pub const STAGED_PATH: &str = "drift/x-drift.kvx";

/// A private copy of a World Source with the drift file added, removed on drop.
#[derive(Debug)]
pub struct Staged {
    /// The staged source directory.
    pub dir: PathBuf,
    /// The drift file as given on the command line, for diagnostics.
    pub given: String,
    /// The drift file bytes, embedded unchanged in the Build.
    pub schedule: Vec<u8>,
}

fn copy_source(from: &Path, to: &Path) -> Result<(), String> {
    fs::create_dir_all(to).map_err(|e| format!("create {}: {e}", to.display()))?;
    let mut entries: Vec<_> = fs::read_dir(from)
        .map_err(|e| format!("read {}: {e}", from.display()))?
        .filter_map(|e| e.ok())
        .collect();
    entries.sort_by_key(|e| e.file_name());
    for e in entries {
        let ft = e
            .file_type()
            .map_err(|err| format!("stat {}: {err}", e.path().display()))?;
        let dst = to.join(e.file_name());
        if ft.is_symlink() {
            let target = fs::read_link(e.path())
                .map_err(|err| format!("readlink {}: {err}", e.path().display()))?;
            std::os::unix::fs::symlink(&target, &dst)
                .map_err(|err| format!("symlink {}: {err}", dst.display()))?;
        } else if ft.is_dir() {
            copy_source(&e.path(), &dst)?;
        } else {
            fs::copy(e.path(), &dst)
                .map_err(|err| format!("copy {}: {err}", e.path().display()))?;
        }
    }
    Ok(())
}

impl Staged {
    /// Stages `root` plus the drift file `file` under `xroot`. Refuses, with a parse-stage
    /// diagnostic located at the drift file, a drift file that cannot be read and a source that
    /// already has a file at [`STAGED_PATH`].
    pub fn new(root: &Path, file: &Path, xroot: &Path) -> Result<Staged, Box<Diagnostic>> {
        let given = file.display().to_string();
        let fail = |m: String| Box::new(diagnostic(Stage::Parse, "drift", &given, m));
        let schedule =
            fs::read(file).map_err(|e| fail(format!("cannot read the drift file: {e}")))?;
        if root.join(STAGED_PATH).exists() {
            return Err(fail(format!(
                "the World Source already has {STAGED_PATH}, which --x-drift reserves"
            )));
        }
        let nanos = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_nanos())
            .unwrap_or(0);
        let dir = xroot.join(format!(".stage-{}-{nanos}", std::process::id()));
        let staged = Staged {
            dir,
            given: given.clone(),
            schedule,
        };
        copy_source(root, &staged.dir)
            .map_err(|m| fail(format!("cannot stage the source: {m}")))?;
        let target = staged.dir.join(STAGED_PATH);
        fs::create_dir_all(staged.dir.join("drift"))
            .and_then(|_| fs::write(&target, &staged.schedule))
            .map_err(|e| fail(format!("cannot stage the drift file: {e}")))?;
        Ok(staged)
    }

    /// Reports diagnostics located in the staged drift file against the given path.
    pub fn relocate(&self, diags: &mut [Diagnostic]) {
        for d in diags.iter_mut() {
            if d.file == STAGED_PATH {
                d.file = self.given.clone();
            }
            for r in &mut d.related {
                if r.file == STAGED_PATH {
                    r.file = self.given.clone();
                }
            }
        }
    }

    /// Applies every schedule entry to the compiled contract table of the parsed source; run
    /// right after a passing parse stage. A source the typed reader did not accept is left to
    /// the stage its errors belong to.
    pub fn check(&self, cx: &Context) -> StageRun {
        let Some(parsed) = cx.parsed.as_ref().filter(|p| p.model.is_some()) else {
            return StageRun::Passed;
        };
        let result = crate::manifest::contracts_value(parsed)
            .and_then(|contracts| check_schedule(&self.schedule, &contracts));
        match result {
            Ok(_) => StageRun::Passed,
            Err(m) => StageRun::Failed(vec![diagnostic(Stage::Parse, "drift", &self.given, m)]),
        }
    }
}

impl Drop for Staged {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.dir);
    }
}

fn table_of(contracts: &Value) -> Result<ContractTable, String> {
    let mut table = BTreeMap::new();
    for (list, adapter) in [("primitives", false), ("adapters", true)] {
        for c in contracts[list].as_array().into_iter().flatten() {
            let s = |k: &str| c[k].as_str().unwrap_or_default().to_string();
            let name = s("name");
            let parse = |src: &str| {
                pexpr::parse(src).map_err(|e| format!("{name}: expression {src:?}: {e}"))
            };
            let mut preconditions = Vec::new();
            for p in c["preconditions"].as_array().into_iter().flatten() {
                let src = p[0].as_str().unwrap_or_default();
                let code = p[1]
                    .as_str()
                    .unwrap_or_default()
                    .parse::<Code>()
                    .map_err(|_| format!("{name}: precondition code outside the envelope"))?;
                preconditions.push((src.to_string(), parse(src)?, code));
            }
            let mut effects = Vec::new();
            for e in c["effects"].as_array().into_iter().flatten() {
                let src = e.as_str().unwrap_or_default();
                effects.push((src.to_string(), parse(src)?));
            }
            let returns_text = s("returns");
            let returns = returns_text
                .parse::<TypeExpr>()
                .map_err(|e| format!("{name}: returns {returns_text:?}: {e:?}"))?;
            table.insert(
                name.clone(),
                Contract {
                    module: s("module"),
                    is_adapter: adapter,
                    side_effect: s("side_effect"),
                    params: Vec::new(),
                    returns,
                    returns_text,
                    preconditions,
                    effects,
                    failures: BTreeSet::new(),
                    content_guaranteed: !adapter,
                    raw: c.clone(),
                    name,
                },
            );
        }
    }
    Ok(ContractTable {
        contracts: table,
        records: Records(BTreeMap::new()),
        modules: Vec::new(),
        runtime: String::new(),
        lib: BTreeMap::new(),
    })
}

/// Parses a drift schedule and applies all of its entries, in schedule order, to the compiled
/// contracts; the error names the first entry that cannot apply.
pub fn check_schedule(bytes: &[u8], contracts: &Value) -> Result<Schedule, String> {
    let schedule = Schedule::parse(bytes)?;
    let mut table = table_of(contracts)?;
    apply_through(&mut table, &schedule, u64::MAX)?;
    Ok(schedule)
}

/// The attribution entry of an `--x-drift` Build: its embedded schedule.
pub fn entry(build: &Path) -> Result<Value, String> {
    let bytes = fs::read(build.join(SCHEDULE_PATH))
        .map_err(|e| format!("Build {}: {SCHEDULE_PATH}: {e}", build.display()))?;
    let schedule = Schedule::parse(&bytes)?;
    let entries: Vec<Value> = schedule
        .entries
        .iter()
        .map(|e| {
            json!({"n": e.n, "turn": e.turn, "primitive": e.primitive, "change": e.change.as_str(),
                   "expr": e.expr, "failure": e.failure.as_str()})
        })
        .collect();
    Ok(json!({
        "kind": "drift_schedule",
        "target": "drift",
        "field": SCHEDULE_PATH,
        "schedule": schedule.digest,
        "after": entries,
    }))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn contracts() -> Value {
        json!({"primitives": [
            {"name": "fs.delete", "module": "fs", "side_effect": "mutating", "returns": "bool",
             "preconditions": [["fs.exists(path)", "NOT_FOUND"], ["not fs.locked(path)", "LOCKED"]],
             "effects": ["not fs.exists(path)"]}
        ], "adapters": []})
    }

    fn drift(change: &str, prim: &str, expr: &str, failure: &str) -> Vec<u8> {
        format!("[meta]\nformat = \"cwr.drift\"\nschema_version = 1\n\n[drift.1]\nturn = 8\nprimitive = \"{prim}\"\nchange = \"{change}\"\nexpr = \"{expr}\"\nfailure = \"{failure}\"\ndoc = \"d\"\n").into_bytes()
    }

    #[test]
    fn a_schedule_that_applies_is_accepted() {
        let s = check_schedule(
            &drift(
                "precondition_added",
                "fs.delete",
                "fs.size(path) < 4096",
                "LOCKED",
            ),
            &contracts(),
        )
        .unwrap();
        assert_eq!(s.entries.len(), 1);
    }

    #[test]
    fn unknown_primitives_and_entries_that_do_not_apply_are_refused() {
        let c = contracts();
        for bytes in [
            drift(
                "precondition_added",
                "fs.nothing",
                "fs.size(path) < 1",
                "LOCKED",
            ),
            drift(
                "precondition_removed",
                "fs.delete",
                "fs.size(path) < 1",
                "LOCKED",
            ),
            drift("limit_changed", "fs.delete", "fs.size(path) < 1", "LOCKED"),
            drift(
                "adapter_changed",
                "fs.delete",
                "fs.size(path) < 1",
                "UNAVAILABLE",
            ),
        ] {
            assert!(check_schedule(&bytes, &c).is_err());
        }
    }
}
