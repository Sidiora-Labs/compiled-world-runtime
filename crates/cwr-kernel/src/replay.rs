// Copyright Sidiora Labs Inc. CWR® 2026-2027
// SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0

//! Ledger replay and comparison (kernel.md sections 2 and 9; clauses 6.6, 8.4 and 8.5; task 2.4,
//! case CWR-010).
//!
//! `cwr instance replay` recreates an Instance from the inputs that define it and nothing else:
//! the seed, tail size, arm and model in the original ledger's header, the initial state named by
//! the header's `initial_state` hash, the Build named by the header's `build` hash, the cassette of
//! the original's adapter traffic and the script of every turn as recorded by the kernel when the
//! turn was run ([`record_script`]). It re-runs every turn's script through the real kernel (fresh
//! sandbox, atomic contract check, gateway in replay mode, so no socket is opened) and compares the
//! new ledger with the original record by record, ignoring only host fields: `host_ts`,
//! `duration_us` and `instance` (the new Instance's ULID, whose time component is host time and
//! which the chain and `world.history` already exclude). The first divergent record is a
//! determinism defect: it is reported with both values and the new Instance is sealed.
//!
//! The original ledger is read strictly before anything is created: a truncated record, a seq gap
//! or any record that fails the hash chain is refused, and so is a header whose Build or initial
//! state does not match the inputs. A ledger edited and re-chained passes that check but cannot
//! survive the comparison, because the replay produces what the world actually does.
//!
//! The only Instance inputs replay and comparison read are the ledger and the state (clause 8.5);
//! kernel internals (metadata, envelopes, sandbox output, gateway store, raw payloads) are never
//! consulted.

use std::path::{Path, PathBuf};

use serde_json::{json, Value};

use crate::contract_check::{NoGateway, SEAL_FILE};
use crate::executor::{run_turn, TurnOptions};
use crate::gateway::{AdapterGateway, GatewayConfig, Mode};
use crate::ledger::{Ledger, TailSize};
use crate::recovery::Journaled;
use crate::state::{CreateOptions, Instance, WorldState};
use crate::{canonical_json, write_atomic, KernelError, Result};

/// Directory of recorded turn scripts inside an Instance directory.
pub const SCRIPTS_DIR: &str = "scripts";
/// Record fields a replay comparison ignores (host-derived).
pub const IGNORED_FIELDS: [&str; 3] = ["host_ts", "duration_us", "instance"];
/// Record kinds a replay can reproduce: those a turn produces.
pub const REPLAYABLE_KINDS: [&str; 5] = ["header", "primitive", "adapter", "proposal", "turn"];

/// Path of the recorded script of `turn` in Instance directory `dir`.
pub fn script_path(dir: &Path, turn: u64) -> PathBuf {
    script_file(&dir.join(SCRIPTS_DIR), turn)
}

/// Path of the script of `turn` in a scripts directory.
pub fn script_file(scripts_dir: &Path, turn: u64) -> PathBuf {
    scripts_dir.join(format!("turn-{turn:06}.py"))
}

/// Records `script` as the script of the next turn of `inst`, durably, before the turn runs.
pub fn record_script(inst: &Instance, script: &str) -> Result<PathBuf> {
    let dir = inst.dir.join(SCRIPTS_DIR);
    std::fs::create_dir_all(&dir).map_err(|e| crate::io_err("create", &dir, e))?;
    let p = script_file(&dir, inst.meta.turn + 1);
    write_atomic(&p, script.as_bytes())?;
    Ok(p)
}

/// The first difference between two ledgers.
#[derive(Debug, Clone, PartialEq)]
pub struct Divergence {
    /// Seq of the first record that differs.
    pub seq: u64,
    /// The first differing field in sorted order (`chain` last), or `<record>` for a missing record.
    pub field: String,
    /// The field's value in the original ledger, or `"<absent>"`.
    pub original: Value,
    /// The field's value in the replayed ledger, or `"<absent>"`.
    pub replayed: Value,
}

impl Divergence {
    /// The divergence as `{seq, field, original, replayed}`.
    pub fn to_json(&self) -> Value {
        json!({"seq": self.seq, "field": self.field, "original": self.original, "replayed": self.replayed})
    }
}

impl std::fmt::Display for Divergence {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(
            f,
            "first divergent record seq {} field {}: original {} replayed {}",
            self.seq,
            self.field,
            canonical_json(&self.original),
            canonical_json(&self.replayed)
        )
    }
}

/// A record without the ignored host fields.
pub fn comparable(r: &Value) -> Value {
    let mut r = r.clone();
    if let Value::Object(m) = &mut r {
        for f in IGNORED_FIELDS {
            m.remove(f);
        }
    }
    r
}

/// The first field (in sorted order) where two records differ, ignoring host fields.
fn field_diff(seq: u64, a: &Value, b: &Value) -> Option<Divergence> {
    let (a, b) = (comparable(a), comparable(b));
    if a == b {
        return None;
    }
    let (Some(am), Some(bm)) = (a.as_object(), b.as_object()) else {
        return Some(Divergence {
            seq,
            field: "<record>".into(),
            original: a,
            replayed: b,
        });
    };
    let mut keys: Vec<&String> = am.keys().chain(bm.keys()).collect();
    keys.sort();
    keys.dedup();
    // `chain` is derived from every other field: report the substantive field first.
    keys.sort_by_key(|k| k.as_str() == "chain");
    for k in keys {
        let (x, y) = (
            am.get(k).cloned().unwrap_or(Value::Null),
            bm.get(k).cloned().unwrap_or(Value::Null),
        );
        if am.contains_key(k) != bm.contains_key(k) || x != y {
            return Some(Divergence {
                seq,
                field: k.clone(),
                original: if am.contains_key(k) {
                    x
                } else {
                    json!("<absent>")
                },
                replayed: if bm.contains_key(k) {
                    y
                } else {
                    json!("<absent>")
                },
            });
        }
    }
    None
}

/// Compares `replayed` with `original` from record `from` on; a replayed ledger shorter than the
/// original is compared only as far as it goes unless `complete`.
pub fn compare_from(
    original: &[Value],
    replayed: &[Value],
    from: usize,
    complete: bool,
) -> Option<Divergence> {
    let n = original.len().max(replayed.len());
    for i in from..n {
        match (original.get(i), replayed.get(i)) {
            (Some(a), Some(b)) => {
                if let Some(d) = field_diff(i as u64, a, b) {
                    return Some(d);
                }
            }
            (Some(a), None) if complete => {
                return Some(Divergence {
                    seq: i as u64,
                    field: "<record>".into(),
                    original: comparable(a),
                    replayed: json!("<absent>"),
                })
            }
            (Some(_), None) => return None,
            (None, Some(b)) => {
                return Some(Divergence {
                    seq: i as u64,
                    field: "<record>".into(),
                    original: json!("<absent>"),
                    replayed: comparable(b),
                })
            }
            (None, None) => break,
        }
    }
    None
}

/// Field-by-field comparison of two complete ledgers ignoring host fields.
pub fn compare(original: &[Value], replayed: &[Value]) -> Option<Divergence> {
    compare_from(original, replayed, 0, true)
}

/// Compares two ledger files; both are read strictly (seq, chain, truncation).
pub fn compare_files(original: &Path, replayed: &Path) -> Result<Option<Divergence>> {
    let a = Ledger::scan(original)?;
    let b = Ledger::scan(replayed)?;
    Ok(compare(&a.records, &b.records))
}

/// Everything a replay reads.
#[derive(Debug, Clone)]
pub struct ReplayInputs {
    /// The original ledger.
    pub ledger: PathBuf,
    /// The Build directory; its verified hash must equal the header's `build`.
    pub build_dir: PathBuf,
    /// The initial world state; its content hash must equal the header's `initial_state`.
    pub initial: Value,
    /// The cassette of the original's adapter traffic (required when the ledger has adapter records).
    pub cassette: Option<PathBuf>,
    /// Directory of the recorded turn scripts (`turn-<n>.py`).
    pub scripts: PathBuf,
    /// Instance store the replay Instance is created in.
    pub home: PathBuf,
    /// Turn options applied to every replayed turn (debug effect assertion).
    pub opts: TurnOptions,
}

/// The outcome of a replay.
#[derive(Debug, Clone)]
pub struct ReplayReport {
    /// The original Instance id, from the ledger header.
    pub original: String,
    /// The id of the new Instance the replay created.
    pub into: String,
    /// Number of records in the replayed ledger.
    pub records: usize,
    /// Number of turns re-run before the end or the first divergence.
    pub turns: u64,
    /// The first divergent record; `None` means identical except host fields.
    pub divergence: Option<Divergence>,
    /// Content hash of the replay Instance's final state.
    pub state_digest: String,
}

impl ReplayReport {
    /// The report as one JSON object, including the ignored host fields.
    pub fn to_json(&self) -> Value {
        json!({
            "original": self.original,
            "into": self.into,
            "records": self.records,
            "turns": self.turns,
            "identical": self.divergence.is_none(),
            "divergence": self.divergence.as_ref().map(Divergence::to_json),
            "state_digest": self.state_digest,
            "ignored_fields": IGNORED_FIELDS,
        })
    }
}

fn refuse(m: String) -> KernelError {
    KernelError::Refused(format!("replay: {m}"))
}

/// Checks the original ledger and the inputs, then replays. Nothing is created when a check fails.
pub fn replay(inp: &ReplayInputs) -> Result<ReplayReport> {
    let scan = Ledger::scan(&inp.ledger)?;
    let records = scan.records;
    let header = records
        .first()
        .ok_or_else(|| refuse("the ledger has no header".into()))?
        .clone();
    let original = header["instance"].as_str().unwrap_or("").to_string();
    if let Some(r) = records
        .iter()
        .find(|r| !REPLAYABLE_KINDS.contains(&r["kind"].as_str().unwrap_or("")))
    {
        return Err(refuse(format!(
            "record {} is a {} record; replay reproduces the records turns produce ({}), not lifecycle events",
            r["seq"],
            r["kind"].as_str().unwrap_or("?"),
            REPLAYABLE_KINDS.join(", ")
        )));
    }
    if !header["created_from"].is_null() {
        return Err(refuse(format!(
            "the Instance was created from snapshot {}; replay starts from an initial state",
            header["created_from"]
        )));
    }
    let build = crate::state::Build::load(&inp.build_dir)?;
    if Some(build.hash.as_str()) != header["build"].as_str() {
        return Err(KernelError::HashMismatch(format!(
            "replay: the ledger names Build {}, the directory holds {}",
            header["build"], build.hash
        )));
    }
    let initial = WorldState::from_json(&inp.initial)?;
    if Some(initial.hash().as_str()) != header["initial_state"].as_str() {
        return Err(KernelError::HashMismatch(format!(
            "replay: the ledger names initial state {}, the input hashes to {}",
            header["initial_state"],
            initial.hash()
        )));
    }
    // Turns must be 1..=T in order, each closed by exactly one turn record, each with a script.
    let turns: Vec<u64> = records
        .iter()
        .filter(|r| r["kind"] == "turn")
        .filter_map(|r| r["turn"].as_u64())
        .collect();
    if turns.iter().enumerate().any(|(i, t)| *t != i as u64 + 1) {
        return Err(refuse(format!(
            "turn records are not 1..{} in order",
            turns.len()
        )));
    }
    let last = records
        .last()
        .ok_or_else(|| refuse("the ledger has no header".into()))?;
    if last["kind"] != "turn" && records.len() > 1 {
        return Err(refuse(format!(
            "the ledger ends inside turn {} (seq {}); recover the Instance first",
            last["turn"], last["seq"]
        )));
    }
    let mut scripts = Vec::new();
    for t in &turns {
        let p = script_file(&inp.scripts, *t);
        let s = std::fs::read_to_string(&p).map_err(|e| {
            refuse(format!(
                "no recorded script for turn {t} at {}: {e}",
                p.display()
            ))
        })?;
        scripts.push(s);
    }
    let uses_adapters = records.iter().any(|r| r["kind"] == "adapter");
    if uses_adapters && inp.cassette.is_none() {
        return Err(refuse("the ledger has adapter records; replay needs the cassette of the original's adapter traffic".into()));
    }

    let opts = CreateOptions {
        seed: header["seed"].as_u64(),
        tail: Some(TailSize::from_json(&header["tail_size"])?),
        arm: header["arm"].as_str().map(str::to_string),
        model: header["model"].as_str().map(str::to_string),
        from_snapshot: None,
        initial: Some(initial.to_json()),
        allow_experimental: !header["experimental"].is_null(),
    };
    let mut inst = Instance::create(&inp.home, &inp.build_dir, opts)?;
    let into = inst.meta.id.clone();
    let mut divergence = compare_from(&records, inst.records(), 0, false);
    let mut done = 0u64;
    if divergence.is_none() {
        for (i, script) in scripts.iter().enumerate() {
            let from = inst.records().len();
            if uses_adapters {
                let cfg = GatewayConfig {
                    mode: Mode::Replay,
                    cassette: inp.cassette.clone(),
                    ..GatewayConfig::default()
                };
                let gw = AdapterGateway::new(cfg, &inst)?;
                let mut gw = Journaled::new(gw, &inst);
                record_script(&inst, script)?;
                run_turn(&mut inst, script, &inp.opts, &mut gw)?;
            } else {
                let mut gw = Journaled::new(NoGateway, &inst);
                record_script(&inst, script)?;
                run_turn(&mut inst, script, &inp.opts, &mut gw)?;
            }
            done = i as u64 + 1;
            divergence = compare_from(&records, inst.records(), from, false);
            if divergence.is_some() {
                break;
            }
        }
    }
    if divergence.is_none() {
        divergence = compare(&records, inst.records());
    }
    if let Some(d) = &divergence {
        let marker = json!({"replay_divergence": d.to_json(), "original": original, "ledger": inp.ledger.display().to_string()});
        write_atomic(
            &inst.dir.join(SEAL_FILE),
            canonical_json(&marker).as_bytes(),
        )?;
    }
    Ok(ReplayReport {
        original,
        into,
        records: inst.records().len(),
        turns: done,
        divergence,
        state_digest: inst.meta.state_hash.clone(),
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn comparison_ignores_only_host_fields() {
        let a = vec![
            json!({"seq": 0, "kind": "header", "host_ts": "x", "duration_us": 1, "instance": "A", "chain": "c0"}),
            json!({"seq": 1, "kind": "turn", "status": "ok", "host_ts": "y", "chain": "c1"}),
        ];
        let mut b = a.clone();
        b[0]["host_ts"] = json!("z");
        b[0]["instance"] = json!("B");
        b[1]["duration_us"] = json!(99);
        assert_eq!(compare(&a, &b), None);
        b[1]["status"] = json!("error");
        let d = compare(&a, &b).unwrap();
        assert_eq!((d.seq, d.field.as_str()), (1, "status"));
        assert_eq!(d.original, json!("ok"));
        assert_eq!(d.replayed, json!("error"));
        let short = &b[..1];
        let d = compare(&a, short).unwrap();
        assert_eq!((d.seq, d.field.as_str()), (1, "<record>"));
        assert!(compare_from(&a, short, 0, false).is_none());
    }

    #[test]
    fn script_paths_are_per_turn() {
        assert_eq!(
            script_path(Path::new("/i"), 7),
            PathBuf::from("/i/scripts/turn-000007.py")
        );
    }
}
