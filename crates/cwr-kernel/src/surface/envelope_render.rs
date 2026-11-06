// Copyright Sidiora Labs Inc. CWR® 2026-2027
// SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0

//! Component 4 of the context: the last result envelope (model-surface.md sections 1 and 4;
//! clauses 9.1 and 9.5; task 3.1, case CWR-012).
//!
//! The envelope is the one the kernel stored when it ended the previous turn (an executed turn, a
//! protocol refusal or a turn closed by recovery). It is rendered as one line of canonical JSON with
//! exactly the six fields `status`, `returned`, `error`, `trace`, `world` and `resources`. Before
//! rendering it is checked against the ledger: it must belong to the last `turn` record (same turn,
//! world time and Build), so a context never shows a stale envelope from an abandoned lineage, and
//! its status and error code must come from the closed sets. There is no envelope before the first
//! turn; the component is then absent.

use std::path::Path;

use cwr_contract::Code;
use serde_json::Value;

use crate::executor::LAST_ENVELOPE;
use crate::{canonical_json, io_err, KernelError, Result};

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

/// The rendered component 4 for an Instance directory and its live ledger records: `None` before
/// the first turn, otherwise the stored envelope as canonical JSON, checked against the last
/// `turn` record.
pub fn render(instance_dir: &Path, records: &[Value]) -> Result<Option<String>> {
    let Some(turn) = last_turn_record(records) else {
        return Ok(None);
    };
    let path = instance_dir.join(LAST_ENVELOPE);
    let bytes = std::fs::read(&path).map_err(|e| io_err("read", &path, e))?;
    let env: Value = serde_json::from_slice(&bytes)
        .map_err(|e| bad(format!("{} is not JSON: {e}", path.display())))?;
    check_shape(&env)?;
    let w = &env["world"];
    if w["turn"] != turn["turn"] || w["time"] != turn["world_time"] || w["build"] != turn["build"] {
        return Err(bad(format!(
            "it belongs to turn {} at world time {} of {}, but the ledger's last turn record is turn {} at world time {} of {}",
            w["turn"], w["time"], w["build"], turn["turn"], turn["world_time"], turn["build"]
        )));
    }
    if env["status"] != turn["status"] {
        return Err(bad(format!(
            "status {} disagrees with the ledger's turn record status {}",
            env["status"], turn["status"]
        )));
    }
    Ok(Some(canonical_json(&env)))
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
        let turn = json!({"kind": "turn", "turn": 1, "world_time": 10, "build": "sha256:b", "status": "ok"});
        assert!(
            render(&dir, &[header.clone(), turn.clone()]).is_err(),
            "missing file"
        );
        std::fs::write(
            dir.join(LAST_ENVELOPE),
            canonical_json(&env("ok", Value::Null)),
        )
        .unwrap();
        let r = render(&dir, &[header.clone(), turn.clone()])
            .unwrap()
            .unwrap();
        assert_eq!(r, canonical_json(&env("ok", Value::Null)));
        let later = json!({"kind": "turn", "turn": 2, "world_time": 20, "build": "sha256:b", "status": "ok"});
        assert!(
            render(&dir, &[header, turn, later]).is_err(),
            "stale envelope"
        );
        let _ = std::fs::remove_dir_all(&dir);
    }
}
