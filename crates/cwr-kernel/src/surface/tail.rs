// Copyright Sidiora Labs Inc. CWR® 2026-2027
// SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0

//! Component 3 of the context: the ledger tail (model-surface.md section 6; clause 9.4; task 3.1,
//! case CWR-012).
//!
//! The tail renders the last K records of kinds `primitive`, `adapter`, `turn`, `drift` and
//! `proposal`, oldest first, one line each. Every line carries the turn (`t<n>`), the world time
//! (`wt<n>`), the primitive, the status and, when there is one, the code, so a model can read from
//! the tail alone when each action happened and how it ended. Primitive, adapter, drift and
//! proposal lines are the kernel's own rendering ([`crate::ledger::render_line`]). A `turn` line
//! names `turn`, or the pseudo-primitive of a refused turn (`turn.protocol`), in the primitive
//! column and adds `calls=<n>`:
//!
//! ```text
//! t37  wt413  fs.delete            ok
//! t37  wt423  turn                 ok     calls=2
//! t38  wt423  turn.protocol        error  INVALID  calls=0
//! ```
//!
//! The world time column on `turn` lines is what clause 9.4 requires of every line; a refused
//! turn's line therefore shows directly that world time did not advance. Call parameters are never
//! rendered; `params_hash` is reachable through `world.history`.
//!
//! K is read from the Instance header record (`tail_size`), never from anything the model can
//! change; an explicit override is an operator choice for the H3 sweep, K in {5, 20, full}.

use serde_json::Value;

use crate::ledger::{render_line, TailSize, TAIL_KINDS};
use crate::{KernelError, Result};

/// Width of the primitive column (the name is followed by one space).
const NAME_WIDTH: usize = 20;

/// K as recorded in the header record (seq 0) of `records`.
pub fn header_tail_size(records: &[Value]) -> Result<TailSize> {
    let header = records
        .first()
        .filter(|r| r["kind"] == "header")
        .ok_or_else(|| KernelError::Corrupt("the ledger has no header record at seq 0".into()))?;
    let k = header
        .get("tail_size")
        .ok_or_else(|| KernelError::Corrupt("the header record carries no tail_size".into()))?;
    TailSize::from_json(k)
}

fn status_code(status: &str, code: Option<&str>, extras: &[String]) -> String {
    let mut s = format!("{status:<6}");
    let mut sep = " ";
    if let Some(c) = code {
        s.push(' ');
        s.push_str(c);
        sep = "  ";
    }
    for e in extras {
        s.push_str(sep);
        s.push_str(e);
        sep = "  ";
    }
    s
}

/// One tail line for `record`, or `None` for kinds the tail omits (header, upgrade, recovery).
pub fn line(record: &Value) -> Option<String> {
    let kind = record["kind"].as_str()?;
    if !TAIL_KINDS.contains(&kind) {
        return None;
    }
    if kind != "turn" {
        return render_line(record);
    }
    let turn = format!("t{}", record["turn"].as_u64()?);
    let wt = format!("wt{}", record["world_time"].as_u64()?);
    let name = record["primitive"].as_str().unwrap_or("turn");
    let status = record["status"].as_str()?;
    let calls: Vec<String> = record["calls"]
        .as_u64()
        .map(|n| format!("calls={n}"))
        .into_iter()
        .collect();
    let rendered = format!(
        "{turn:<3}  {wt:<5}  {name:<NAME_WIDTH$} {}",
        status_code(status, record["code"].as_str(), &calls)
    );
    Some(rendered.trim_end().to_string())
}

/// The last `k` renderable records of `records`, oldest first, each on one line ending in `\n`.
pub fn render(records: &[Value], k: TailSize) -> String {
    let lines: Vec<String> = records.iter().filter_map(line).collect();
    let skip = match k {
        TailSize::Full => 0,
        TailSize::Last(k) => lines
            .len()
            .saturating_sub(usize::try_from(k).unwrap_or(usize::MAX)),
    };
    let mut out = String::new();
    for l in lines.iter().skip(skip) {
        out.push_str(l);
        out.push('\n');
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn prim(turn: u64, wt: u64, name: &str, status: &str, code: Option<&str>) -> Value {
        json!({"kind": "primitive", "turn": turn, "world_time": wt, "primitive": name,
               "outcome": {"status": status, "code": code}})
    }

    #[test]
    fn every_line_carries_turn_world_time_primitive_status_and_code() {
        let mut rate = prim(38, 415, "publish.post", "error", Some("RATE_LIMITED"));
        rate["retry_after"] = json!(30);
        let records = vec![
            json!({"kind": "header", "turn": 0, "world_time": 0, "tail_size": 20}),
            prim(36, 410, "fs.delete", "error", Some("LOCKED")),
            prim(37, 412, "fs.unlock", "ok", None),
            prim(37, 413, "fs.delete", "ok", None),
            json!({"kind": "turn", "turn": 37, "world_time": 423, "primitive": null,
                   "status": "ok", "code": null, "calls": 2}),
            rate,
            json!({"kind": "upgrade", "turn": 38, "world_time": 425}),
            json!({"kind": "turn", "turn": 39, "world_time": 425, "primitive": "turn.protocol",
                   "status": "error", "code": "INVALID", "calls": 0}),
        ];
        let expected = "\
t36  wt410  fs.delete            error  LOCKED
t37  wt412  fs.unlock            ok
t37  wt413  fs.delete            ok
t37  wt423  turn                 ok     calls=2
t38  wt415  publish.post         error  RATE_LIMITED  retry_after=30
t39  wt425  turn.protocol        error  INVALID  calls=0
";
        assert_eq!(render(&records, TailSize::Full), expected);
        assert_eq!(render(&records, TailSize::DEFAULT), expected);
        let last2 = render(&records, TailSize::Last(2));
        assert_eq!(last2.lines().count(), 2);
        assert!(last2.starts_with("t38  wt415  publish.post"));
    }

    #[test]
    fn k_comes_from_the_header() {
        let h = |k: Value| vec![json!({"kind": "header", "tail_size": k})];
        assert_eq!(header_tail_size(&h(json!(5))).unwrap(), TailSize::Last(5));
        assert_eq!(header_tail_size(&h(json!("full"))).unwrap(), TailSize::Full);
        assert!(header_tail_size(&h(json!(0))).is_err());
        assert!(header_tail_size(&[json!({"kind": "turn"})]).is_err());
        assert!(header_tail_size(&[]).is_err());
    }
}
