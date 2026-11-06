// Copyright Sidiora Labs Inc. CWR® 2026-2027
// SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0

//! The Instance ledger (kernel.md section 8, clause 8.1) and its model-facing views
//! (model-surface.md section 6, clause 9.4).
//!
//! One Instance, one append-only JSONL file. Every record is one canonical JSON object on one
//! line, written with a single `write` on an `O_APPEND` descriptor and fsynced before
//! [`Ledger::append`] returns, so no result can reach a script or an operator before its
//! record is durable.
//!
//! Record fields are those of kernel.md section 8 (`seq`, `instance`, `build`, `turn`,
//! `world_time`, `kind`, `primitive`, `params_hash`, `preconditions`, `outcome`,
//! `duration_us`, `host_ts`, `experimental`) plus the kind-specific fields named there, plus
//! one integrity field, `chain`: `sha256:` of the previous record's `chain`, a newline and the
//! canonical JSON of this record without `chain`, `host_ts`, `duration_us` and `instance`. The
//! chain makes any edit, reorder, deletion or splice detectable (the `instance` field itself is
//! checked on open), and because it excludes host-derived fields (the two host fields and the
//! ULID, whose time component is host time) a faithful replay into a new Instance reproduces it
//! exactly and no host-derived value reaches the model through it.
//!
//! [`Ledger::open`] refuses a ledger whose final record is truncated or whose seq or chain does
//! not verify. Only restore/recovery may read such a file, through [`Ledger::scan_lenient`].

use std::collections::BTreeMap;
use std::fs::{self, File, OpenOptions};
use std::io::Write;
use std::path::{Path, PathBuf};
use std::str::FromStr;

use cwr_contract::Code;
use serde_json::{json, Map, Value};

use crate::{canonical_json, io_err, sha256_hex, sync_dir, KernelError, Result};

/// Record kinds (kernel.md section 8).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Kind {
    /// The first record of every ledger.
    Header,
    /// One primitive call.
    Primitive,
    /// One external adapter call.
    Adapter,
    /// The record that closes a turn.
    Turn,
    /// A drift observation.
    Drift,
    /// A `world.propose` outbox entry.
    Proposal,
    /// A Build upgrade of the Instance.
    Upgrade,
    /// A crash recovery of the Instance.
    Recovery,
}

impl Kind {
    /// Every kind, in declaration order.
    pub const ALL: [Kind; 8] = [
        Kind::Header,
        Kind::Primitive,
        Kind::Adapter,
        Kind::Turn,
        Kind::Drift,
        Kind::Proposal,
        Kind::Upgrade,
        Kind::Recovery,
    ];

    /// The `kind` value written in the record.
    pub fn as_str(self) -> &'static str {
        match self {
            Kind::Header => "header",
            Kind::Primitive => "primitive",
            Kind::Adapter => "adapter",
            Kind::Turn => "turn",
            Kind::Drift => "drift",
            Kind::Proposal => "proposal",
            Kind::Upgrade => "upgrade",
            Kind::Recovery => "recovery",
        }
    }

    /// The kind spelled `s`, or `None` for an unknown kind.
    pub fn parse(s: &str) -> Option<Kind> {
        Kind::ALL.into_iter().find(|k| k.as_str() == s)
    }
}

/// One evaluated precondition: `{index, expr_hash, passed}`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PreconditionEval {
    /// 1-based position of the precondition in its contract.
    pub index: u32,
    /// `sha256:<hex>` of the precondition source.
    pub expr_hash: String,
    /// Whether the precondition held.
    pub passed: bool,
}

/// `{status, code, shape, side_effect}`; `code` is drawn from the closed envelope only.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Outcome {
    /// Whether the call succeeded.
    pub ok: bool,
    /// Envelope code of a failed call; `None` when ok.
    pub code: Option<Code>,
    /// Declared return type of the callee.
    pub shape: Option<String>,
    /// Declared side effect of the callee.
    pub side_effect: Option<String>,
}

impl Outcome {
    /// A successful outcome.
    pub fn ok(shape: &str, side_effect: &str) -> Self {
        Outcome {
            ok: true,
            code: None,
            shape: Some(shape.into()),
            side_effect: Some(side_effect.into()),
        }
    }

    /// A failed outcome with `code`.
    pub fn error(code: Code, shape: &str, side_effect: &str) -> Self {
        Outcome {
            ok: false,
            code: Some(code),
            shape: Some(shape.into()),
            side_effect: Some(side_effect.into()),
        }
    }

    fn to_json(&self) -> Value {
        json!({
            "status": if self.ok { "ok" } else { "error" },
            "code": self.code.map(|c| c.as_str()),
            "shape": self.shape,
            "side_effect": self.side_effect,
        })
    }
}

/// What a caller supplies for one record; the ledger fills `seq`, `instance`, `build`,
/// `experimental`, `host_ts` and `chain`.
#[derive(Debug, Clone)]
pub struct RecordDraft {
    /// Turn the record belongs to.
    pub turn: u64,
    /// World time at the record.
    pub world_time: u64,
    /// Record kind.
    pub kind: Kind,
    /// Primitive or adapter called, for call records.
    pub primitive: Option<String>,
    /// `blake3:<hex>` of the call parameters, for call records.
    pub params_hash: Option<String>,
    /// Precondition evaluations, in declared order.
    pub preconditions: Vec<PreconditionEval>,
    /// Outcome of the call, for call records.
    pub outcome: Option<Outcome>,
    /// Host duration of the call in microseconds (a host field, ignored by replay).
    pub duration_us: u64,
    /// Kind-specific fields (header: build, seed, tail_size, arm, model, created_from; turn:
    /// status, code, returned_hash, resources, calls; recovery: snapshot, resumed_from,
    /// interrupted_turn; ...).
    pub extra: BTreeMap<String, Value>,
}

impl RecordDraft {
    /// A draft with no call fields and no extra fields.
    pub fn new(kind: Kind, turn: u64, world_time: u64) -> Self {
        RecordDraft {
            turn,
            world_time,
            kind,
            primitive: None,
            params_hash: None,
            preconditions: Vec::new(),
            outcome: None,
            duration_us: 0,
            extra: BTreeMap::new(),
        }
    }

    /// The draft with extra field `key` set to `value`.
    pub fn with(mut self, key: &str, value: Value) -> Self {
        self.extra.insert(key.to_string(), value);
        self
    }
}

/// The schema fields every record carries, in kernel.md order, plus `chain`.
pub const COMMON_FIELDS: [&str; 14] = [
    "seq",
    "instance",
    "build",
    "turn",
    "world_time",
    "kind",
    "primitive",
    "params_hash",
    "preconditions",
    "outcome",
    "duration_us",
    "host_ts",
    "experimental",
    "chain",
];

/// Fields excluded from replay comparison (kernel.md section 9).
pub const HOST_FIELDS: [&str; 2] = ["host_ts", "duration_us"];
/// Fields excluded from the chain input besides `chain` itself.
pub const UNCHAINED_FIELDS: [&str; 3] = ["host_ts", "duration_us", "instance"];

/// The chain value of `record` given the previous record's chain ("" before the header).
pub fn chain_of(prev: &str, record: &Value) -> String {
    let mut r = record.clone();
    if let Value::Object(m) = &mut r {
        m.remove("chain");
        for f in UNCHAINED_FIELDS {
            m.remove(f);
        }
    }
    let mut bytes = prev.as_bytes().to_vec();
    bytes.push(b'\n');
    bytes.extend_from_slice(canonical_json(&r).as_bytes());
    format!("sha256:{}", sha256_hex(&bytes))
}

/// The result of reading a ledger file.
#[derive(Debug, Clone)]
pub struct LedgerScan {
    /// Every complete record, in file order.
    pub records: Vec<Value>,
    /// Byte offset just after each complete record line.
    pub line_ends: Vec<usize>,
    /// Bytes after the last complete line (a truncated record), if any.
    pub truncated_tail: Option<Vec<u8>>,
    /// The whole file.
    pub raw: Vec<u8>,
}

impl LedgerScan {
    /// Seq of the last complete record, or `None` for an empty ledger.
    pub fn last_seq(&self) -> Option<u64> {
        self.records.last().and_then(|r| r["seq"].as_u64())
    }

    /// Chain hash of the last complete record, or `None` for an empty ledger.
    pub fn last_chain(&self) -> Option<&str> {
        self.records.last().and_then(|r| r["chain"].as_str())
    }

    /// Bytes of records `0..=seq`.
    pub fn prefix_bytes(&self, seq: u64) -> Option<&[u8]> {
        let end = *self.line_ends.get(seq as usize)?;
        Some(&self.raw[..end])
    }
}

fn parse_and_verify(path: &Path, raw: Vec<u8>) -> Result<LedgerScan> {
    let mut records = Vec::new();
    let mut line_ends = Vec::new();
    let mut start = 0usize;
    let mut prev_chain = String::new();
    while let Some(pos) = raw[start..].iter().position(|b| *b == b'\n') {
        let end = start + pos;
        let line = std::str::from_utf8(&raw[start..end]).map_err(|_| {
            KernelError::Corrupt(format!(
                "{}: record {} is not UTF-8",
                path.display(),
                records.len()
            ))
        })?;
        let v: Value = serde_json::from_str(line).map_err(|e| {
            KernelError::Corrupt(format!(
                "{}: record {} is not JSON: {e}",
                path.display(),
                records.len()
            ))
        })?;
        let seq = records.len() as u64;
        if v.get("seq").and_then(Value::as_u64) != Some(seq) {
            return Err(KernelError::Corrupt(format!(
                "{}: record at line {} has seq {} (expected {seq})",
                path.display(),
                seq + 1,
                v["seq"]
            )));
        }
        for f in COMMON_FIELDS {
            if v.get(f).is_none() {
                return Err(KernelError::Corrupt(format!(
                    "{}: record {seq} lacks field {f}",
                    path.display()
                )));
            }
        }
        let expect = chain_of(&prev_chain, &v);
        if v["chain"].as_str() != Some(expect.as_str()) {
            return Err(KernelError::Corrupt(format!(
                "{}: record {seq} fails the hash chain",
                path.display()
            )));
        }
        if seq == 0 && v["kind"] != "header" {
            return Err(KernelError::Corrupt(format!(
                "{}: record 0 is not a header",
                path.display()
            )));
        }
        prev_chain = expect;
        records.push(v);
        line_ends.push(end + 1);
        start = end + 1;
    }
    let truncated_tail = (start < raw.len()).then(|| raw[start..].to_vec());
    Ok(LedgerScan {
        records,
        line_ends,
        truncated_tail,
        raw,
    })
}

/// An open, writable ledger.
#[derive(Debug)]
pub struct Ledger {
    path: PathBuf,
    file: File,
    next_seq: u64,
    last_chain: String,
    instance: String,
    build: String,
    experimental: Option<String>,
}

impl Ledger {
    /// Creates a new, empty ledger file; the first append must be the header.
    pub fn create(
        path: &Path,
        instance: &str,
        build: &str,
        experimental: Option<&str>,
    ) -> Result<Ledger> {
        let file = OpenOptions::new()
            .append(true)
            .create_new(true)
            .open(path)
            .map_err(|e| io_err("create ledger", path, e))?;
        if let Some(dir) = path.parent() {
            sync_dir(dir)?;
        }
        Ok(Ledger {
            path: path.to_path_buf(),
            file,
            next_seq: 0,
            last_chain: String::new(),
            instance: instance.into(),
            build: build.into(),
            experimental: experimental.map(str::to_string),
        })
    }

    /// Strict read: refuses a truncated final record, a seq gap or duplicate and a broken chain.
    pub fn scan(path: &Path) -> Result<LedgerScan> {
        let s = Self::scan_lenient(path)?;
        if let Some(tail) = &s.truncated_tail {
            return Err(KernelError::Truncated(format!(
                "{}: final record is truncated ({} bytes after seq {}); run `cwr instance restore` to recover",
                path.display(),
                tail.len(),
                s.last_seq().map(|n| n.to_string()).unwrap_or_else(|| "none".into())
            )));
        }
        Ok(s)
    }

    /// Reads complete records and reports, rather than refuses, a truncated tail. Used only by
    /// restore and recovery.
    pub fn scan_lenient(path: &Path) -> Result<LedgerScan> {
        let raw = fs::read(path).map_err(|e| io_err("read ledger", path, e))?;
        parse_and_verify(path, raw)
    }

    /// Opens for append after a strict scan. `build` and `experimental` stamp future records.
    pub fn open(
        path: &Path,
        instance: &str,
        build: &str,
        experimental: Option<&str>,
    ) -> Result<(Ledger, LedgerScan)> {
        let scan = Self::scan(path)?;
        if scan.records.is_empty() {
            return Err(KernelError::Corrupt(format!(
                "{}: ledger has no header",
                path.display()
            )));
        }
        if let Some(r) = scan.records.iter().find(|r| r["instance"] != instance) {
            return Err(KernelError::Corrupt(format!(
                "{}: record {} belongs to instance {}",
                path.display(),
                r["seq"],
                r["instance"]
            )));
        }
        let file = OpenOptions::new()
            .append(true)
            .open(path)
            .map_err(|e| io_err("open ledger", path, e))?;
        let ledger = Ledger {
            path: path.to_path_buf(),
            file,
            next_seq: scan.records.len() as u64,
            last_chain: scan.last_chain().unwrap_or_default().to_string(),
            instance: instance.into(),
            build: build.into(),
            experimental: experimental.map(str::to_string),
        };
        Ok((ledger, scan))
    }

    /// Path of the ledger file.
    pub fn path(&self) -> &Path {
        &self.path
    }

    /// The seq the next record will carry.
    pub fn next_seq(&self) -> u64 {
        self.next_seq
    }

    /// Chain hash of the last appended record.
    pub fn last_chain(&self) -> &str {
        &self.last_chain
    }

    /// Rebinds future records to another Build (only at an `upgrade` record).
    pub fn set_build(&mut self, build: &str, experimental: Option<&str>) {
        self.build = build.into();
        self.experimental = experimental.map(str::to_string);
    }

    /// Appends one record and fsyncs before returning it.
    pub fn append(&mut self, draft: RecordDraft) -> Result<Value> {
        if (self.next_seq == 0) != (draft.kind == Kind::Header) {
            return Err(KernelError::Refused(
                "the header is exactly the record at seq 0".into(),
            ));
        }
        let mut m = Map::new();
        for (k, v) in draft.extra {
            if COMMON_FIELDS.contains(&k.as_str()) {
                return Err(KernelError::Refused(format!(
                    "kind-specific field {k} shadows a schema field"
                )));
            }
            m.insert(k, v);
        }
        m.insert("seq".into(), json!(self.next_seq));
        m.insert("instance".into(), json!(self.instance));
        m.insert("build".into(), json!(self.build));
        m.insert("turn".into(), json!(draft.turn));
        m.insert("world_time".into(), json!(draft.world_time));
        m.insert("kind".into(), json!(draft.kind.as_str()));
        m.insert("primitive".into(), json!(draft.primitive));
        m.insert("params_hash".into(), json!(draft.params_hash));
        m.insert(
            "preconditions".into(),
            Value::Array(
                draft
                    .preconditions
                    .iter()
                    .map(
                        |p| json!({"index": p.index, "expr_hash": p.expr_hash, "passed": p.passed}),
                    )
                    .collect(),
            ),
        );
        m.insert(
            "outcome".into(),
            draft
                .outcome
                .as_ref()
                .map(Outcome::to_json)
                .unwrap_or(Value::Null),
        );
        m.insert("duration_us".into(), json!(draft.duration_us));
        m.insert("host_ts".into(), json!(crate::host_ts()));
        m.insert("experimental".into(), json!(self.experimental));
        let mut record = Value::Object(m);
        let chain = chain_of(&self.last_chain, &record);
        record["chain"] = json!(chain);
        let mut line = canonical_json(&record);
        line.push('\n');
        self.file
            .write_all(line.as_bytes())
            .map_err(|e| io_err("append ledger", &self.path, e))?;
        self.file
            .sync_data()
            .map_err(|e| io_err("fsync ledger", &self.path, e))?;
        self.next_seq += 1;
        self.last_chain = chain;
        Ok(record)
    }
}

/// Ledger tail size K: a positive record count or `full` (model-surface.md section 6).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TailSize {
    /// The last K records, K at least 1.
    Last(u64),
    /// The whole ledger.
    Full,
}

impl TailSize {
    /// Tail size of an Instance created without `--tail`.
    pub const DEFAULT: TailSize = TailSize::Last(20);

    /// K as a number, or the string `full`.
    pub fn to_json(self) -> Value {
        match self {
            TailSize::Last(k) => json!(k),
            TailSize::Full => json!("full"),
        }
    }

    /// Parses a positive integer or `full`; anything else is [`KernelError::Refused`].
    pub fn from_json(v: &Value) -> Result<TailSize> {
        match v {
            Value::String(s) if s == "full" => Ok(TailSize::Full),
            Value::Number(n) => match n.as_u64() {
                Some(k) if k >= 1 => Ok(TailSize::Last(k)),
                _ => Err(KernelError::Refused(format!(
                    "tail size {n} must be a positive integer or \"full\""
                ))),
            },
            other => Err(KernelError::Refused(format!(
                "tail size {other} must be a positive integer or \"full\""
            ))),
        }
    }
}

impl FromStr for TailSize {
    type Err = KernelError;
    fn from_str(s: &str) -> Result<TailSize> {
        if s == "full" {
            return Ok(TailSize::Full);
        }
        match s.parse::<u64>() {
            Ok(k) if k >= 1 => Ok(TailSize::Last(k)),
            _ => Err(KernelError::Refused(format!(
                "--tail {s:?} must be a positive integer or full"
            ))),
        }
    }
}

/// Record kinds the tail renders (model-surface.md section 6).
pub const TAIL_KINDS: [&str; 5] = ["primitive", "adapter", "turn", "drift", "proposal"];

fn status_code_extras(status: &str, code: Option<&str>, extras: &[String]) -> String {
    let mut s = format!("{status:<6}");
    match code {
        Some(c) => {
            s.push(' ');
            s.push_str(c);
            for e in extras {
                s.push_str("  ");
                s.push_str(e);
            }
        }
        None => {
            for (i, e) in extras.iter().enumerate() {
                s.push_str(if i == 0 { " " } else { "  " });
                s.push_str(e);
            }
        }
    }
    s
}

/// Renders one record as one tail line, or None for kinds the tail omits.
pub fn render_line(r: &Value) -> Option<String> {
    let kind = r["kind"].as_str()?;
    if !TAIL_KINDS.contains(&kind) {
        return None;
    }
    let turn = format!("t{}", r["turn"].as_u64().unwrap_or(0));
    let wt = format!("wt{}", r["world_time"].as_u64().unwrap_or(0));
    let line = match kind {
        "turn" => {
            let status = r["status"].as_str().unwrap_or("error");
            let code = r["code"].as_str();
            let mut extras = Vec::new();
            if let Some(n) = r["calls"].as_u64() {
                extras.push(format!("calls={n}"));
            }
            format!(
                "{turn:<3}  {:<28}{}",
                "turn",
                status_code_extras(status, code, &extras)
            )
        }
        // Task 3.2: a drift record is rendered only when it is marked visible; the drift grammar
        // has no visibility key, so injected drift never reaches the tail (model-surface.md 8).
        "drift" if !visible_to_model(r) => return None,
        "drift" => {
            let name = r["primitive"].as_str().unwrap_or("drift");
            let what = r["change"]
                .as_str()
                .map(|s| vec![s.to_string()])
                .unwrap_or_default();
            format!(
                "{turn:<3}  {wt:<5}  {name:<20} {}",
                status_code_extras("drift", None, &what)
            )
        }
        _ => {
            let name = r["primitive"].as_str().unwrap_or(kind);
            let outcome = &r["outcome"];
            let status = outcome["status"].as_str().unwrap_or("ok");
            let code = outcome["code"].as_str();
            let mut extras = Vec::new();
            if let Some(n) = r["retry_after"].as_u64() {
                extras.push(format!("retry_after={n}"));
            }
            if let Some(l) = r["limit"].as_str() {
                extras.push(format!("limit={l}"));
            }
            format!(
                "{turn:<3}  {wt:<5}  {name:<20} {}",
                status_code_extras(status, code, &extras)
            )
        }
    };
    Some(line.trim_end().to_string())
}

/// Component 3 of the context: the last K renderable records, oldest first, one per line.
pub fn render_tail(records: &[Value], k: TailSize) -> String {
    let lines: Vec<String> = records.iter().filter_map(render_line).collect();
    let skip = match k {
        TailSize::Full => 0,
        TailSize::Last(k) => lines.len().saturating_sub(k as usize),
    };
    let mut out = String::new();
    for l in &lines[skip..] {
        out.push_str(l);
        out.push('\n');
    }
    out
}

/// Fields `world.history` never returns: host fields, the Instance id (a ULID whose time
/// component is host-derived), the experiment's arm label and the experimental flag.
pub const HIDDEN_FROM_MODEL: [&str; 5] =
    ["host_ts", "duration_us", "instance", "arm", "experimental"];

/// Whether a record may reach the model through the tail or `world.history`: every record except
/// a `drift` record not marked `visible = true` (task 3.2).
pub fn visible_to_model(r: &Value) -> bool {
    r["kind"] != "drift" || r["visible"] == Value::Bool(true)
}

/// What `world.history(n)` returns: the last `n` records visible to the model, oldest first,
/// without hidden fields and without the seed unless the Build makes it visible.
pub fn history_view(records: &[Value], n: usize, seed_visible: bool) -> Vec<Value> {
    let visible: Vec<&Value> = records.iter().filter(|r| visible_to_model(r)).collect();
    let start = visible.len().saturating_sub(n);
    visible[start..]
        .iter()
        .map(|r| {
            let mut r = (*r).clone();
            if let Value::Object(m) = &mut r {
                for f in HIDDEN_FROM_MODEL {
                    m.remove(f);
                }
                if !seed_visible {
                    m.remove("seed");
                }
            }
            r
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tmp(name: &str) -> PathBuf {
        let d = std::env::temp_dir().join(format!("cwr-ledger-{}-{name}", std::process::id()));
        let _ = fs::remove_dir_all(&d);
        fs::create_dir_all(&d).unwrap();
        d.join("ledger.jsonl")
    }

    fn header() -> RecordDraft {
        RecordDraft::new(Kind::Header, 0, 0)
            .with("seed", json!(1))
            .with("tail_size", json!(20))
    }

    #[test]
    fn append_scan_chain_and_tamper() {
        let p = tmp("chain");
        let mut l = Ledger::create(&p, "I", "sha256:00", None).unwrap();
        l.append(header()).unwrap();
        let mut d = RecordDraft::new(Kind::Primitive, 1, 1);
        d.primitive = Some("world.now".into());
        d.outcome = Some(Outcome::ok("int", "read_only"));
        l.append(d).unwrap();
        let s = Ledger::scan(&p).unwrap();
        assert_eq!(s.records.len(), 2);
        assert!(
            Ledger::append(&mut l, header()).is_err(),
            "second header refused"
        );
        // Tamper: change world_time in record 1 without fixing the chain.
        let text = fs::read_to_string(&p)
            .unwrap()
            .replace("\"world_time\":1", "\"world_time\":2");
        fs::write(&p, text).unwrap();
        assert!(matches!(Ledger::scan(&p), Err(KernelError::Corrupt(_))));
    }

    #[test]
    fn truncated_tail_refused_strict_reported_lenient() {
        let p = tmp("trunc");
        let mut l = Ledger::create(&p, "I", "sha256:00", None).unwrap();
        l.append(header()).unwrap();
        drop(l);
        let mut f = OpenOptions::new().append(true).open(&p).unwrap();
        f.write_all(b"{\"seq\":1,\"kin").unwrap();
        assert!(matches!(Ledger::scan(&p), Err(KernelError::Truncated(_))));
        assert!(matches!(
            Ledger::open(&p, "I", "sha256:00", None),
            Err(KernelError::Truncated(_))
        ));
        let s = Ledger::scan_lenient(&p).unwrap();
        assert_eq!(s.records.len(), 1);
        assert_eq!(s.truncated_tail.as_deref(), Some(&b"{\"seq\":1,\"kin"[..]));
    }

    #[test]
    fn tail_format_matches_model_surface() {
        let r = |turn: u64, wt: u64, name: &str, status: &str, code: Option<&str>| {
            json!({"kind": "primitive", "turn": turn, "world_time": wt, "primitive": name,
                   "outcome": {"status": status, "code": code}})
        };
        let mut rate = r(38, 415, "publish.post", "error", Some("RATE_LIMITED"));
        rate["retry_after"] = json!(30);
        let records = vec![
            json!({"kind": "header", "turn": 0, "world_time": 0}),
            r(36, 410, "fs.delete", "error", Some("LOCKED")),
            r(37, 412, "fs.unlock", "ok", None),
            r(37, 413, "fs.delete", "ok", None),
            json!({"kind": "turn", "turn": 37, "world_time": 423, "status": "ok", "code": null, "calls": 2}),
            rate,
        ];
        let expected = "\
t36  wt410  fs.delete            error  LOCKED
t37  wt412  fs.unlock            ok
t37  wt413  fs.delete            ok
t37  turn                        ok     calls=2
t38  wt415  publish.post         error  RATE_LIMITED  retry_after=30
";
        assert_eq!(render_tail(&records, TailSize::Full), expected);
        assert_eq!(render_tail(&records, TailSize::Last(2)).lines().count(), 2);
        assert_eq!(render_tail(&records, TailSize::DEFAULT).lines().count(), 5);
    }

    #[test]
    fn history_hides_host_fields() {
        let records = vec![
            json!({"seq": 0, "kind": "header", "host_ts": "x", "duration_us": 3, "instance": "I", "arm": "a", "seed": 5}),
        ];
        let v = history_view(&records, 5, false);
        assert_eq!(v, vec![json!({"seq": 0, "kind": "header"})]);
        let v = history_view(&records, 5, true);
        assert_eq!(v[0]["seed"], 5);
    }
}
