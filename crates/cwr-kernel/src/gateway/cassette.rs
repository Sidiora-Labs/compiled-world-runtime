// Copyright Sidiora Labs Inc. CWR® 2026-2027
// SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0

//! Cassettes: recorded adapter traffic of one Instance (kernel.md section 7; clause 7.5).
//!
//! The format is `cwr.cassette` schema version 1, specified in benchmark/cassettes/FORMAT.md and
//! mirrored by benchmark/stubs/cassette_tool.py: a UTF-8 JSONL file whose every line is canonical
//! JSON; line 1 is the header, every later line one entry keyed by (adapter, canonical request
//! hash, sequence). An entry stores the **raw** service response (HTTP status, kept headers and
//! body text, or a timeout, or a transport error), never the normalised outcome, so replay runs
//! the very normalisation live mode runs and both produce identical outcomes.
//!
//! The canonical request hash is `sha256:` over the canonical JSON of
//! `{"adapter": <name>, "params": <codec-form params>}` (FORMAT.md section 3, scheme
//! `sha256-cwr-canonical-json-v1`). The sequence is the ordinal of that (adapter, hash) pair within
//! the Instance, starting at 0. Calls answered by the `key` idempotency store are not service
//! traffic and never appear in a cassette. Nothing host-derived (time, duration, Instance id, host
//! name) is stored.

use std::collections::{BTreeMap, HashMap};
use std::fs::{self, OpenOptions};
use std::io::Write;
use std::path::{Path, PathBuf};

use serde_json::{json, Map, Value};

use crate::{canonical_json, sha256_hex};

/// The `format` value of a cassette header.
pub const FORMAT: &str = "cwr.cassette";
/// The cassette schema version this kernel reads and writes.
pub const SCHEMA_VERSION: u64 = 1;
/// Name of the request hash scheme of [`request_hash`], recorded in the header.
pub const REQUEST_HASH_SCHEME: &str = "sha256-cwr-canonical-json-v1";
/// Response headers a cassette keeps (lower case); every other header is dropped.
pub const KEPT_HEADERS: [&str; 2] = ["content-type", "x-cwr-retry-after-ticks"];

/// The canonical request hash of one adapter call.
pub fn request_hash(adapter: &str, params: &Value) -> String {
    format!(
        "sha256:{}",
        sha256_hex(canonical_json(&json!({"adapter": adapter, "params": params})).as_bytes())
    )
}

/// What the service did, before normalisation.
#[derive(Debug, Clone, PartialEq)]
pub enum RawResponse {
    /// An HTTP response; `headers` holds only [`KEPT_HEADERS`], lower case.
    Http {
        /// HTTP status code.
        status: u16,
        /// Kept response headers, lower case.
        headers: BTreeMap<String, String>,
        /// Response body as received.
        body: String,
    },
    /// No response within the contract's `timeout_ms` of host time.
    Timeout,
    /// Connection refused, reset, DNS failure, protocol breakage: `error` names the failure class.
    TransportError {
        /// The failure class, e.g. `ConnectionRefusedError` or `OSError`.
        error: String,
    },
}

impl RawResponse {
    /// The JSON form stored in a cassette entry.
    pub fn to_json(&self) -> Value {
        match self {
            RawResponse::Http {
                status,
                headers,
                body,
            } => json!({"kind": "http", "status": status, "headers": headers, "body": body}),
            RawResponse::Timeout => json!({"kind": "timeout"}),
            RawResponse::TransportError { error } => {
                json!({"kind": "transport_error", "error": error})
            }
        }
    }

    /// Parses the JSON form, refusing unknown kinds and missing or extra keys.
    pub fn from_json(v: &Value) -> Result<RawResponse, String> {
        let o = v.as_object().ok_or("response is not an object")?;
        let keys: Vec<&str> = o.keys().map(String::as_str).collect();
        match o.get("kind").and_then(Value::as_str) {
            Some("http") => {
                let mut want = vec!["body", "headers", "kind", "status"];
                want.sort();
                let mut have = keys.clone();
                have.sort();
                if have != want {
                    return Err(format!("http response keys {have:?}, expected {want:?}"));
                }
                let status = o["status"]
                    .as_u64()
                    .filter(|s| (100..=599).contains(s))
                    .ok_or("http status must be an integer in 100..599")?
                    as u16;
                let mut headers = BTreeMap::new();
                for (k, hv) in o["headers"]
                    .as_object()
                    .ok_or("headers must be an object")?
                {
                    if !KEPT_HEADERS.contains(&k.as_str()) {
                        return Err(format!("header {k:?} is not one a cassette keeps"));
                    }
                    headers.insert(
                        k.clone(),
                        hv.as_str()
                            .ok_or("header values must be strings")?
                            .to_string(),
                    );
                }
                let body = o["body"]
                    .as_str()
                    .ok_or("body must be a string")?
                    .to_string();
                Ok(RawResponse::Http {
                    status,
                    headers,
                    body,
                })
            }
            Some("timeout") if keys.len() == 1 => Ok(RawResponse::Timeout),
            Some("transport_error") if keys.len() == 2 => Ok(RawResponse::TransportError {
                error: o
                    .get("error")
                    .and_then(Value::as_str)
                    .ok_or("transport_error needs a string error")?
                    .to_string(),
            }),
            Some(k) => Err(format!(
                "response kind {k:?} with keys {keys:?} is not a cassette response form"
            )),
            None => Err("response has no kind".into()),
        }
    }

    /// `sha256:` over the canonical JSON of the cassette form; the ledger's response evidence.
    pub fn hash(&self) -> String {
        format!(
            "sha256:{}",
            sha256_hex(canonical_json(&self.to_json()).as_bytes())
        )
    }
}

/// One cassette entry.
#[derive(Debug, Clone, PartialEq)]
pub struct Entry {
    /// Qualified adapter name.
    pub adapter: String,
    /// Call parameters as sent.
    pub params: Value,
    /// [`request_hash`] of the adapter and parameters.
    pub request_hash: String,
    /// Ordinal of this call among calls with the same adapter and request hash.
    pub seq: u64,
    /// World time of the call, when recorded.
    pub world_time: Option<u64>,
    /// What the service did.
    pub response: RawResponse,
}

impl Entry {
    /// The JSON line of this entry.
    pub fn to_json(&self) -> Value {
        json!({
            "adapter": self.adapter,
            "params": self.params,
            "request_hash": self.request_hash,
            "seq": self.seq,
            "world_time": self.world_time,
            "response": self.response.to_json(),
        })
    }
}

/// The header line of a new cassette carrying `label`.
pub fn header(label: Option<&str>) -> Value {
    json!({"format": FORMAT, "schema_version": SCHEMA_VERSION, "request_hash": REQUEST_HASH_SCHEME, "label": label})
}

fn valid_adapter_name(s: &str) -> bool {
    let ident = |p: &str| {
        let mut ch = p.chars();
        matches!(ch.next(), Some(c) if c.is_ascii_lowercase() || c == '_')
            && ch.all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '_')
    };
    matches!(s.split_once('.'), Some((m, n)) if ident(m) && ident(n))
}

/// A cassette in memory, optionally bound to a file it appends to (record mode).
#[derive(Debug, Clone, Default)]
pub struct Cassette {
    /// Header label (`<task>/seed-<n>` by convention).
    pub label: Option<String>,
    /// Entries in file order.
    pub entries: Vec<Entry>,
    index: HashMap<(String, String, u64), usize>,
    path: Option<PathBuf>,
}

impl Cassette {
    /// An empty in-memory cassette bound to no file.
    pub fn new(label: Option<&str>) -> Cassette {
        Cassette {
            label: label.map(str::to_string),
            ..Default::default()
        }
    }

    /// Parses and verifies cassette bytes: header, entry keys, canonical encoding, hash and
    /// params agreement, contiguous `seq` per (adapter, hash), non-decreasing `world_time` and
    /// the response forms. Any problem refuses the whole cassette.
    pub fn parse(bytes: &[u8]) -> Result<Cassette, Vec<String>> {
        let mut errors = Vec::new();
        let text = match std::str::from_utf8(bytes) {
            Ok(t) => t,
            Err(e) => return Err(vec![format!("not UTF-8: {e}")]),
        };
        if !text.ends_with('\n') {
            errors.push("a cassette ends with a newline".into());
        }
        let mut c = Cassette::default();
        let mut counts: HashMap<(String, String), u64> = HashMap::new();
        let mut last_wt: Option<u64> = None;
        for (i, line) in text.lines().enumerate() {
            let n = i + 1;
            let v: Value = match serde_json::from_str(line) {
                Ok(v) => v,
                Err(e) => {
                    errors.push(format!("line {n}: not JSON: {e}"));
                    continue;
                }
            };
            if canonical_json(&v) != line {
                errors.push(format!("line {n}: not canonical JSON"));
            }
            if n == 1 {
                let o = v.as_object();
                let ok = o.map(|o| o.len() == 4).unwrap_or(false)
                    && v.get("format") == Some(&json!(FORMAT))
                    && v.get("schema_version") == Some(&json!(SCHEMA_VERSION))
                    && v.get("request_hash") == Some(&json!(REQUEST_HASH_SCHEME))
                    && matches!(v.get("label"), Some(Value::Null) | Some(Value::String(_)));
                if !ok {
                    errors.push(format!("line 1: not a {FORMAT} schema {SCHEMA_VERSION} header with request_hash {REQUEST_HASH_SCHEME}"));
                }
                c.label = v.get("label").and_then(Value::as_str).map(str::to_string);
                continue;
            }
            let Some(o) = v.as_object() else {
                errors.push(format!("line {n}: an entry is an object"));
                continue;
            };
            let mut keys: Vec<&str> = o.keys().map(String::as_str).collect();
            keys.sort();
            if keys
                != [
                    "adapter",
                    "params",
                    "request_hash",
                    "response",
                    "seq",
                    "world_time",
                ]
            {
                errors.push(format!("line {n}: entry keys {keys:?}"));
                continue;
            }
            let adapter = o["adapter"].as_str().unwrap_or("").to_string();
            if !valid_adapter_name(&adapter) {
                errors.push(format!(
                    "line {n}: adapter {adapter:?} is not a qualified adapter name"
                ));
            }
            if !o["params"].is_object() {
                errors.push(format!("line {n}: params must be an object"));
            }
            let rh = o["request_hash"].as_str().unwrap_or("").to_string();
            if rh != request_hash(&adapter, &o["params"]) {
                errors.push(format!(
                    "line {n}: request_hash does not match adapter and params"
                ));
            }
            let Some(seq) = o["seq"].as_u64() else {
                errors.push(format!("line {n}: seq must be a non-negative integer"));
                continue;
            };
            let expected = counts.entry((adapter.clone(), rh.clone())).or_insert(0);
            if seq != *expected {
                errors.push(format!(
                    "line {n}: seq {seq} for ({adapter}, {rh}) should be {expected}"
                ));
            }
            *expected += 1;
            let world_time = match &o["world_time"] {
                Value::Null => None,
                w => match w.as_u64() {
                    Some(t) => Some(t),
                    None => {
                        errors.push(format!(
                            "line {n}: world_time must be a non-negative integer or null"
                        ));
                        None
                    }
                },
            };
            if let Some(t) = world_time {
                if last_wt.map(|l| t < l).unwrap_or(false) {
                    errors.push(format!("line {n}: world_time decreases"));
                }
                last_wt = Some(t);
            }
            match RawResponse::from_json(&o["response"]) {
                Ok(response) => c.push(Entry {
                    adapter,
                    params: o["params"].clone(),
                    request_hash: rh,
                    seq,
                    world_time,
                    response,
                }),
                Err(e) => errors.push(format!("line {n}: {e}")),
            }
        }
        if text.is_empty() {
            errors.push("empty cassette: no header".into());
        }
        if errors.is_empty() {
            Ok(c)
        } else {
            Err(errors)
        }
    }

    /// Loads and verifies a cassette file.
    pub fn load(path: &Path) -> Result<Cassette, String> {
        let bytes =
            fs::read(path).map_err(|e| format!("cannot read cassette {}: {e}", path.display()))?;
        let mut c = Cassette::parse(&bytes).map_err(|errs| {
            format!(
                "cassette {} is refused: {}",
                path.display(),
                errs.join("; ")
            )
        })?;
        c.path = Some(path.to_path_buf());
        Ok(c)
    }

    /// Opens `path` for recording: an existing cassette is verified and appended to; a missing
    /// one is created with its header line.
    pub fn open_for_record(path: &Path, label: Option<&str>) -> Result<Cassette, String> {
        if path.exists() {
            return Cassette::load(path);
        }
        let mut f = OpenOptions::new()
            .create_new(true)
            .write(true)
            .open(path)
            .map_err(|e| format!("cannot create cassette {}: {e}", path.display()))?;
        f.write_all(format!("{}\n", canonical_json(&header(label))).as_bytes())
            .and_then(|_| f.sync_all())
            .map_err(|e| format!("cannot write cassette {}: {e}", path.display()))?;
        let mut c = Cassette::new(label);
        c.path = Some(path.to_path_buf());
        Ok(c)
    }

    fn push(&mut self, e: Entry) {
        self.index.insert(
            (e.adapter.clone(), e.request_hash.clone(), e.seq),
            self.entries.len(),
        );
        self.entries.push(e);
    }

    /// Number of entries for one (adapter, hash) pair.
    pub fn count(&self, adapter: &str, hash: &str) -> u64 {
        self.entries
            .iter()
            .filter(|e| e.adapter == adapter && e.request_hash == hash)
            .count() as u64
    }

    /// Per-pair counts, for checking a cassette against an Instance's ledger.
    pub fn counts(&self) -> BTreeMap<(String, String), u64> {
        let mut m = BTreeMap::new();
        for e in &self.entries {
            *m.entry((e.adapter.clone(), e.request_hash.clone()))
                .or_insert(0) += 1;
        }
        m
    }

    /// Replay lookup by (adapter, canonical request hash, sequence).
    pub fn get(&self, adapter: &str, hash: &str, seq: u64) -> Option<&Entry> {
        self.index
            .get(&(adapter.to_string(), hash.to_string(), seq))
            .map(|i| &self.entries[*i])
    }

    /// Appends one entry and, when bound to a file, writes it durably (fsync) before returning.
    pub fn append(&mut self, e: Entry) -> Result<(), String> {
        if e.seq != self.count(&e.adapter, &e.request_hash) {
            return Err(format!(
                "cassette sequence {} for {} is not contiguous",
                e.seq, e.adapter
            ));
        }
        if let Some(path) = &self.path {
            let mut f = OpenOptions::new()
                .append(true)
                .open(path)
                .map_err(|err| format!("cannot open cassette {}: {err}", path.display()))?;
            f.write_all(format!("{}\n", canonical_json(&e.to_json())).as_bytes())
                .and_then(|_| f.sync_all())
                .map_err(|err| format!("cannot append to cassette {}: {err}", path.display()))?;
        }
        self.push(e);
        Ok(())
    }

    /// The canonical bytes of the whole cassette.
    pub fn to_bytes(&self) -> Vec<u8> {
        let mut out = format!("{}\n", canonical_json(&header(self.label.as_deref())));
        for e in &self.entries {
            out.push_str(&canonical_json(&e.to_json()));
            out.push('\n');
        }
        out.into_bytes()
    }
}

/// Keeps only [`KEPT_HEADERS`] from a header list, lower-cased.
pub fn kept_headers(all: &[(String, String)]) -> BTreeMap<String, String> {
    let mut m = BTreeMap::new();
    for (k, v) in all {
        let k = k.to_ascii_lowercase();
        if KEPT_HEADERS.contains(&k.as_str()) {
            m.insert(k, v.trim().to_string());
        }
    }
    m
}

/// Params as a JSON object value.
pub fn params_value(params: &Map<String, Value>) -> Value {
    Value::Object(params.clone())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn request_hash_matches_format_md() {
        // Key order of the input does not matter; the hash is over canonical JSON.
        let a = request_hash("search.query", &json!({"q": "tennis", "limit": 3}));
        let b = request_hash("search.query", &json!({"limit": 3, "q": "tennis"}));
        assert_eq!(a, b);
        let text = r#"{"adapter":"search.query","params":{"limit":3,"q":"tennis"}}"#;
        assert_eq!(a, format!("sha256:{}", sha256_hex(text.as_bytes())));
    }

    #[test]
    fn round_trip_and_verify() {
        let mut c = Cassette::new(Some("t/seed-1"));
        let p = json!({"q": "tennis", "limit": 3});
        let h = request_hash("search.query", &p);
        let mut headers = BTreeMap::new();
        headers.insert("content-type".to_string(), "application/json".to_string());
        c.append(Entry {
            adapter: "search.query".into(),
            params: p.clone(),
            request_hash: h.clone(),
            seq: 0,
            world_time: Some(0),
            response: RawResponse::Http {
                status: 200,
                headers,
                body: "{\"result\":[]}".into(),
            },
        })
        .unwrap();
        c.append(Entry {
            adapter: "search.query".into(),
            params: p.clone(),
            request_hash: h.clone(),
            seq: 1,
            world_time: Some(1),
            response: RawResponse::Timeout,
        })
        .unwrap();
        assert!(c
            .append(Entry {
                adapter: "search.query".into(),
                params: p,
                request_hash: h.clone(),
                seq: 5,
                world_time: Some(2),
                response: RawResponse::Timeout
            })
            .is_err());
        let bytes = c.to_bytes();
        let back = Cassette::parse(&bytes).unwrap();
        assert_eq!(back.entries, c.entries);
        assert_eq!(
            back.get("search.query", &h, 1).unwrap().response,
            RawResponse::Timeout
        );
        assert!(back.get("search.query", &h, 2).is_none());
        // A tampered hash, a gap in seq or a non-canonical line is refused.
        let tampered = String::from_utf8(bytes.clone())
            .unwrap()
            .replace("\"seq\":1", "\"seq\":2");
        assert!(Cassette::parse(tampered.as_bytes()).is_err());
        let spaced = String::from_utf8(bytes)
            .unwrap()
            .replacen("{\"format\"", "{ \"format\"", 1);
        assert!(Cassette::parse(spaced.as_bytes()).is_err());
    }
}
