// Copyright Sidiora Labs Inc. CWR® 2026-2027
// SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0

//! Component 4 of the context: the last result envelope (model-surface.md sections 1 and 4;
//! clauses 9.1 and 9.5; task 3.1, case CWR-012).
//!
//! The envelope is the one the kernel stored when it ended the previous turn (an executed turn, a
//! protocol refusal or a turn closed by recovery). It is rendered as one line of canonical JSON with
//! exactly the six fields `status`, `returned`, `error`, `trace`, `world` and `resources`.
//!
//! [`store`] keeps one envelope per `turn` record, at `envelopes/<chain>.json` keyed by the hex of
//! the record's chain hash, which is unique to that record in that lineage. [`render`] selects the
//! envelope of the ledger's last live `turn` record, so after `cwr instance restore` to an earlier
//! snapshot the context shows the envelope of the restored lineage's last turn, never one from the
//! abandoned lineage. The selected envelope must agree with its record (turn, world time, Build and
//! status), and its status and error code must come from the closed sets. When no stored envelope
//! belongs to the last turn record (a crash between the record and the envelope write, or an
//! Instance written before per-turn envelopes), the component is absent rather than an error. There
//! is no envelope before the first turn; the component is then absent too.

use std::path::{Path, PathBuf};

use cwr_contract::Code;
use serde_json::Value;

use crate::executor::LAST_ENVELOPE;
use crate::{canonical_json, io_err, write_atomic, KernelError, Result};

/// Directory of per-turn envelopes inside an Instance directory.
pub const ENVELOPES_DIR: &str = "envelopes";

/// The six envelope fields, in canonical (sorted) order.
pub const FIELDS: [&str; 6] = ["error", "resources", "returned", "status", "trace", "world"];
/// The four `world` coordinates.
pub const WORLD_FIELDS: [&str; 4] = ["build", "time", "turn", "version"];
/// Envelope statuses.
pub const STATUSES: [&str; 3] = ["ok", "error", "partial"];

fn bad(m: String) -> KernelError {
    KernelError::Corrupt(format!("last result envelope: {m}"))
}

/// Checks the shape of `env`: six fields, a known status, an error from the closed envelope or
/// null, and four world coordinates.
pub fn check_shape(env: &Value) -> Result<()> {
    let o = env
        .as_object()
        .ok_or_else(|| bad("not a JSON object".into()))?;
    let mut keys: Vec<&str> = o.keys().map(String::as_str).collect();
    keys.sort_unstable();
    if keys != FIELDS {
        return Err(bad(format!("fields {keys:?}, expected {FIELDS:?}")));
    }
    let status = o["status"].as_str().unwrap_or("");
    if !STATUSES.contains(&status) {
        return Err(bad(format!(
            "status {} is not ok, error or partial",
            o["status"]
        )));
    }
    match &o["error"] {
        Value::Null if status == "ok" => {}
        Value::Null => return Err(bad(format!("status {status} without an error"))),
        e => {
            let code = e["code"].as_str().unwrap_or("");
            if code.parse::<Code>().is_err() {
                return Err(bad(format!(
                    "error code {:?} is outside the envelope",
                    e["code"]
                )));
            }
        }
    }
    if !o["trace"].is_array() {
        return Err(bad("trace is not a list".into()));
    }
    let world = o["world"]
        .as_object()
        .ok_or_else(|| bad("world is not an object".into()))?;
    let mut wk: Vec<&str> = world.keys().map(String::as_str).collect();
    wk.sort_unstable();
    if wk != WORLD_FIELDS {
        return Err(bad(format!(
            "world fields {wk:?}, expected {WORLD_FIELDS:?}"
        )));
    }
    if !o["resources"].is_object() {
        return Err(bad("resources is not an object".into()));
    }
    Ok(())
}

/// The last `turn` record of `records`, if any turn has ended.
pub fn last_turn_record(records: &[Value]) -> Option<&Value> {
    records.iter().rev().find(|r| r["kind"] == "turn")
}

/// Where the envelope of `turn_record` is kept, or `None` when the record has no valid chain hash.
pub fn envelope_path(instance_dir: &Path, turn_record: &Value) -> Option<PathBuf> {
    let chain = turn_record["chain"].as_str()?;
    let hex = chain.strip_prefix("sha256:").unwrap_or(chain);
    if hex.is_empty() || !hex.bytes().all(|b| b.is_ascii_hexdigit()) {
        return None;
    }
    Some(instance_dir.join(ENVELOPES_DIR).join(format!("{hex}.json")))
}

/// Stores `env` as the envelope of the durable `turn_record`: under [`ENVELOPES_DIR`] keyed by
/// the record's chain hash, and as the Instance's last result envelope. Both writes are atomic.
pub fn store(instance_dir: &Path, turn_record: &Value, env: &Value) -> Result<()> {
    let bytes = format!("{}\n", canonical_json(env));
    let path = envelope_path(instance_dir, turn_record).ok_or_else(|| {
        bad(format!(
            "turn record {} carries no chain hash to key its envelope",
            turn_record["seq"]
        ))
    })?;
    let dir = instance_dir.join(ENVELOPES_DIR);
    std::fs::create_dir_all(&dir).map_err(|e| io_err("create", &dir, e))?;
    write_atomic(&path, bytes.as_bytes())?;
    write_atomic(&instance_dir.join(LAST_ENVELOPE), bytes.as_bytes())
}

/// Whether `env` belongs to `turn`: same turn, world time, Build and status.
fn belongs(env: &Value, turn: &Value) -> bool {
    let w = &env["world"];
    w["turn"] == turn["turn"]
        && w["time"] == turn["world_time"]
        && w["build"] == turn["build"]
        && env["status"] == turn["status"]
}

/// Reads and shape-checks one stored envelope; `None` when the file does not exist.
fn read_envelope(path: &Path) -> Result<Option<Value>> {
    let bytes = match std::fs::read(path) {
        Ok(b) => b,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(e) => return Err(io_err("read", path, e)),
    };
    let env: Value = serde_json::from_slice(&bytes)
        .map_err(|e| bad(format!("{} is not JSON: {e}", path.display())))?;
    check_shape(&env)?;
    Ok(Some(env))
}

/// The rendered component 4 for an Instance directory and its live ledger records: `None` before
/// the first turn or when no stored envelope belongs to the last `turn` record, otherwise that
/// record's envelope as canonical JSON. A stored envelope that is not JSON or not of the envelope
/// shape is [`KernelError::Corrupt`].
pub fn render(instance_dir: &Path, records: &[Value]) -> Result<Option<String>> {
    let Some(turn) = last_turn_record(records) else {
        return Ok(None);
    };
    let candidates = envelope_path(instance_dir, turn)
        .into_iter()
        .chain(std::iter::once(instance_dir.join(LAST_ENVELOPE)));
    for path in candidates {
        if let Some(env) = read_envelope(&path)? {
            if belongs(&env, turn) {
                return Ok(Some(canonical_json(&env)));
            }
        }
    }
    Ok(None)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn env(status: &str, error: Value) -> Value {
        json!({"status": status, "returned": null, "error": error, "trace": [],
               "world": {"version": "0.2.0", "build": "sha256:b", "time": 10, "turn": 1},
               "resources": {"cpu_ms": 0}})
    }

    #[test]
    fn shapes() {
        assert!(check_shape(&env("ok", Value::Null)).is_ok());
        assert!(check_shape(&env("error", json!({"code": "INVALID"}))).is_ok());
        assert!(check_shape(&env("error", Value::Null)).is_err());
        assert!(check_shape(&env("done", Value::Null)).is_err());
        assert!(check_shape(&env("error", json!({"code": "OOPS"}))).is_err());
        let mut extra = env("ok", Value::Null);
        extra["host"] = json!("x");
        assert!(check_shape(&extra).is_err());
    }

    #[test]
    fn render_checks_the_ledger() {
        let dir = std::env::temp_dir().join(format!("cwr-envrender-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let header = json!({"kind": "header", "turn": 0, "world_time": 0, "build": "sha256:b"});
        assert_eq!(render(&dir, std::slice::from_ref(&header)).unwrap(), None);
        let turn = json!({"kind": "turn", "turn": 1, "world_time": 10, "build": "sha256:b", "status": "ok", "chain": "sha256:aa01"});
        assert_eq!(
            render(&dir, &[header.clone(), turn.clone()]).unwrap(),
            None,
            "no stored envelope: the component is absent, not an error"
        );
        store(&dir, &turn, &env("ok", Value::Null)).unwrap();
        let r = render(&dir, &[header.clone(), turn.clone()])
            .unwrap()
            .unwrap();
        assert_eq!(r, canonical_json(&env("ok", Value::Null)));
        let later = json!({"kind": "turn", "turn": 2, "world_time": 20, "build": "sha256:b", "status": "ok", "chain": "sha256:bb02"});
        assert_eq!(
            render(&dir, &[header.clone(), turn.clone(), later.clone()]).unwrap(),
            None,
            "a crash before the envelope of turn 2 was written leaves the component absent"
        );
        let mut env2 = env("ok", Value::Null);
        env2["world"]["turn"] = json!(2);
        env2["world"]["time"] = json!(20);
        store(&dir, &later, &env2).unwrap();
        assert_eq!(
            render(&dir, &[header.clone(), turn.clone(), later])
                .unwrap()
                .unwrap(),
            canonical_json(&env2)
        );
        assert_eq!(
            render(&dir, &[header.clone(), turn.clone()])
                .unwrap()
                .unwrap(),
            canonical_json(&env("ok", Value::Null)),
            "after a restore to turn 1 the context shows turn 1's envelope, not the abandoned one"
        );
        std::fs::write(dir.join(LAST_ENVELOPE), "not json").unwrap();
        let p = envelope_path(&dir, &turn).unwrap();
        std::fs::write(&p, "{}").unwrap();
        assert!(
            render(&dir, &[header, turn]).is_err(),
            "a stored envelope of the wrong shape is corruption"
        );
        let _ = std::fs::remove_dir_all(&dir);
    }
}
