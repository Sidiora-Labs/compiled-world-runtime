// Copyright Sidiora Labs Inc. CWR® 2026-2027
// SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0

//! Idempotency enforcement per adapter contract (kernel.md section 7; clause 7.4).
//!
//! | `idempotency` | Gateway behaviour |
//! |---|---|
//! | `key` | The first outcome per (adapter, key) is stored with the payload hash, in every mode including live, whether it is ok or a declared failure (except `RATE_LIMITED`, which releases the key). An identical request is answered with the stored outcome without contacting the service; a different payload under the same key is `DUPLICATE_KEY` with the original receipt (or the original failure) in `detail`. |
//! | `natural` | Every call is forwarded; the ledger record carries the request hash and the response hash as evidence that equal requests produced equal outcomes when they did. |
//! | `none` | Every call is forwarded; the manual labels the adapter non-idempotent. |
//!
//! The store is a per-Instance append-only JSONL event log (`gateway/idempotency.jsonl` in the
//! Instance directory, outside the Build and outside the state store). For a `key` adapter the
//! gateway writes a `pending` event, fsynced, **before** the request is sent (kernel.md section 9),
//! then `done` with the first outcome (kernel.md section 7: "stores the first outcome per
//! (adapter, key, payload hash)"; clause 7.4: "the original outcome for an identical replay"):
//! an ok result, or a declared failure the call produced (a failure the service reported, a
//! transport failure, a timeout). After `TIMEOUT` the effect may or may not have happened; the
//! refworld manual tells the model to check with the status primitive and post again under a new
//! key, and an identical repeat answers `TIMEOUT` from the store. Two endings release the key
//! with a `cleared` event instead: a defect (`INVARIANT_VIOLATION`, never an outcome:
//! design.body.md pillar 4), and `RATE_LIMITED`, which is the service declining to process the
//! request at all and naming in `retry_after` when the same call may be repeated (clause 7.3: a
//! retry is the model's decision, informed by `retry_after`; storing it would answer that retry
//! with a stale rate limit and make `retry_after` meaningless for the key). Releasing the key on
//! `RATE_LIMITED` departs from the literal text of kernel.md section 7 ("the declared failure the
//! call produced (a defect releases the key)"), under which a declared `RATE_LIMITED` would be
//! stored; it is recorded as deviation `finding.task_2_3_rate_limited_key_release` in
//! spec/cwr/qualification.kvx. The refworld manual's rate-limit example
//! (manual/examples.md, repeat `publish.post` under the same key at `retry_at`) and the benchmark's
//! reference simulator (benchmark/tasks/_authoring/refsim.py) both rely on the release. A `pending` entry
//! left by a crash is visible through [`KeyStore::pending`] for recovery (task 2.4).

use std::collections::BTreeMap;
use std::fs::{self, OpenOptions};
use std::io::Write;
use std::path::{Path, PathBuf};

use serde_json::{json, Map, Value};

use crate::{canonical_json, sha256_hex};

/// File name of the per-Instance key store, in the Instance directory.
pub const STORE_FILE: &str = "idempotency.jsonl";

/// The adapter's idempotency class.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Idempotency {
    /// Every call reaches the service; nothing is deduplicated.
    None,
    /// The service deduplicates by itself; the gateway forwards every call.
    Natural,
    /// The gateway deduplicates by the declared key parameter through the key store.
    Key,
}

impl Idempotency {
    /// The class declared by a raw contract, or `None` when absent or unknown.
    pub fn of(raw: &Value) -> Option<Idempotency> {
        match raw.get("idempotency").and_then(Value::as_str) {
            Some("none") => Some(Idempotency::None),
            Some("natural") => Some(Idempotency::Natural),
            Some("key") => Some(Idempotency::Key),
            _ => None,
        }
    }

    /// The contract spelling of the class.
    pub fn as_str(self) -> &'static str {
        match self {
            Idempotency::None => "none",
            Idempotency::Natural => "natural",
            Idempotency::Key => "key",
        }
    }
}

/// `sha256:` of the payload under a key: every parameter except the key parameter.
pub fn payload_hash(params: &Map<String, Value>, key_param: &str) -> String {
    let mut p = params.clone();
    p.remove(key_param);
    format!(
        "sha256:{}",
        sha256_hex(canonical_json(&Value::Object(p)).as_bytes())
    )
}

/// The stored key: the canonical JSON of the key parameter's value.
pub fn key_of(params: &Map<String, Value>, key_param: &str) -> Option<String> {
    params.get(key_param).map(canonical_json)
}

/// A stored first outcome.
#[derive(Debug, Clone, PartialEq)]
pub enum Stored {
    /// The ok result (the receipt).
    Ok(Value),
    /// A declared failure: code name, retry_after (RATE_LIMITED only) and detail.
    Fail {
        /// Envelope code name.
        code: String,
        /// World ticks to wait before retrying (RATE_LIMITED only).
        retry_after: Option<u64>,
        /// Explanation recorded with the outcome.
        detail: String,
    },
}

impl Stored {
    /// The `done` event fields for this outcome.
    fn fields(&self) -> (Value, Value) {
        match self {
            Stored::Ok(v) => (v.clone(), Value::Null),
            Stored::Fail {
                code,
                retry_after,
                detail,
            } => (
                Value::Null,
                json!({"code": code, "retry_after": retry_after, "detail": detail}),
            ),
        }
    }

    fn from_event(v: &Value) -> Stored {
        match v.get("failure").filter(|f| f.is_object()) {
            Some(f) => Stored::Fail {
                code: f
                    .get("code")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .to_string(),
                retry_after: f.get("retry_after").and_then(Value::as_u64),
                detail: f
                    .get("detail")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .to_string(),
            },
            None => Stored::Ok(v.get("outcome").cloned().unwrap_or(Value::Null)),
        }
    }

    /// The original outcome as `DUPLICATE_KEY` detail text.
    pub fn describe(&self) -> String {
        match self {
            Stored::Ok(v) => format!("original receipt: {}", canonical_json(v)),
            Stored::Fail { code, detail, .. } => {
                format!("original outcome: {code} ({detail}); there is no receipt")
            }
        }
    }
}

/// What the store holds for one (adapter, key).
#[derive(Debug, Clone, PartialEq)]
pub enum Slot {
    /// The call was forwarded and its outcome is not yet known.
    Pending {
        /// Hash of the call payload the key was first used with.
        payload_hash: String,
        /// Request hash of that first call.
        request_hash: String,
    },
    /// The call completed with a stored first outcome.
    Done {
        /// Hash of the call payload the key was first used with.
        payload_hash: String,
        /// Request hash of that first call.
        request_hash: String,
        /// The first outcome, returned to every identical retry.
        outcome: Stored,
    },
}

/// What the store says about a new call.
#[derive(Debug, Clone, PartialEq)]
pub enum Lookup {
    /// No stored outcome: forward the call.
    Miss,
    /// A crash left the key pending: the outcome is unknown, forward the call.
    Pending,
    /// Identical payload: return the stored outcome without contacting the service.
    Replay(Stored),
    /// Different payload under a stored key: `DUPLICATE_KEY` with the original outcome.
    Duplicate {
        /// The outcome stored for the key's first payload.
        original: Stored,
    },
}

/// The per-Instance `key` idempotency store.
#[derive(Debug)]
pub struct KeyStore {
    path: PathBuf,
    slots: BTreeMap<(String, String), Slot>,
}

impl KeyStore {
    /// Opens (creating the directory if needed) the store under `dir`. A torn final line, the
    /// only damage a crash during an append can leave, is ignored.
    pub fn open(dir: &Path) -> Result<KeyStore, String> {
        fs::create_dir_all(dir).map_err(|e| format!("cannot create {}: {e}", dir.display()))?;
        let path = dir.join(STORE_FILE);
        let mut slots = BTreeMap::new();
        if let Ok(text) = fs::read_to_string(&path) {
            let lines: Vec<&str> = text.split('\n').collect();
            for (i, line) in lines.iter().enumerate() {
                if line.is_empty() {
                    continue;
                }
                let v: Value = match serde_json::from_str(line) {
                    Ok(v) => v,
                    Err(_) if i + 1 == lines.len() => break,
                    Err(e) => return Err(format!("{} line {}: {e}", path.display(), i + 1)),
                };
                let s = |k: &str| v.get(k).and_then(Value::as_str).unwrap_or("").to_string();
                let k = (s("adapter"), s("key"));
                match s("event").as_str() {
                    "pending" => {
                        slots.insert(
                            k,
                            Slot::Pending {
                                payload_hash: s("payload_hash"),
                                request_hash: s("request_hash"),
                            },
                        );
                    }
                    "done" => {
                        slots.insert(
                            k,
                            Slot::Done {
                                payload_hash: s("payload_hash"),
                                request_hash: s("request_hash"),
                                outcome: Stored::from_event(&v),
                            },
                        );
                    }
                    "cleared" => {
                        slots.remove(&k);
                    }
                    other => {
                        return Err(format!(
                            "{} line {}: unknown event {other:?}",
                            path.display(),
                            i + 1
                        ))
                    }
                }
            }
        }
        Ok(KeyStore { path, slots })
    }

    fn write(&self, event: Value) -> Result<(), String> {
        let mut f = OpenOptions::new()
            .create(true)
            .append(true)
            .open(&self.path)
            .map_err(|e| format!("cannot open {}: {e}", self.path.display()))?;
        f.write_all(format!("{}\n", canonical_json(&event)).as_bytes())
            .and_then(|_| f.sync_all())
            .map_err(|e| format!("cannot append to {}: {e}", self.path.display()))
    }

    /// What a new call of `adapter` under `key` with `payload_hash` must do.
    pub fn lookup(&self, adapter: &str, key: &str, payload_hash: &str) -> Lookup {
        match self.slots.get(&(adapter.to_string(), key.to_string())) {
            None => Lookup::Miss,
            Some(Slot::Pending { .. }) => Lookup::Pending,
            Some(Slot::Done {
                payload_hash: ph,
                outcome,
                ..
            }) if ph == payload_hash => Lookup::Replay(outcome.clone()),
            Some(Slot::Done { outcome, .. }) => Lookup::Duplicate {
                original: outcome.clone(),
            },
        }
    }

    /// Records the key as pending, durably, before the request is sent.
    pub fn begin(
        &mut self,
        adapter: &str,
        key: &str,
        payload_hash: &str,
        request_hash: &str,
    ) -> Result<(), String> {
        self.write(json!({"event": "pending", "adapter": adapter, "key": key, "payload_hash": payload_hash, "request_hash": request_hash}))?;
        self.slots.insert(
            (adapter.into(), key.into()),
            Slot::Pending {
                payload_hash: payload_hash.into(),
                request_hash: request_hash.into(),
            },
        );
        Ok(())
    }

    /// Stores the first outcome: from now on the key answers identical requests from the store.
    pub fn complete(
        &mut self,
        adapter: &str,
        key: &str,
        payload_hash: &str,
        request_hash: &str,
        outcome: &Stored,
    ) -> Result<(), String> {
        let (ok, failure) = outcome.fields();
        self.write(json!({"event": "done", "adapter": adapter, "key": key, "payload_hash": payload_hash, "request_hash": request_hash, "outcome": ok, "failure": failure}))?;
        self.slots.insert(
            (adapter.into(), key.into()),
            Slot::Done {
                payload_hash: payload_hash.into(),
                request_hash: request_hash.into(),
                outcome: outcome.clone(),
            },
        );
        Ok(())
    }

    /// Releases a pending key whose call ended in a defect or a rate limit rather than an outcome.
    pub fn clear(&mut self, adapter: &str, key: &str) -> Result<(), String> {
        if matches!(
            self.slots.get(&(adapter.to_string(), key.to_string())),
            Some(Slot::Pending { .. })
        ) {
            self.write(json!({"event": "cleared", "adapter": adapter, "key": key}))?;
            self.slots.remove(&(adapter.to_string(), key.to_string()));
        }
        Ok(())
    }

    /// Keys a crash left pending: (adapter, key, request hash).
    pub fn pending(&self) -> Vec<(String, String, String)> {
        self.slots
            .iter()
            .filter_map(|((a, k), s)| match s {
                Slot::Pending { request_hash, .. } => {
                    Some((a.clone(), k.clone(), request_hash.clone()))
                }
                _ => None,
            })
            .collect()
    }

    /// The stored slot of (`adapter`, `key`), if any.
    pub fn slot(&self, adapter: &str, key: &str) -> Option<&Slot> {
        self.slots.get(&(adapter.to_string(), key.to_string()))
    }

    /// Reads the store under `dir` without creating anything: an absent directory is an empty
    /// store. Used by `cwr instance keys`, which must not write to the Instance it inspects.
    pub fn read(dir: &Path) -> Result<KeyStore, String> {
        if dir.is_dir() {
            KeyStore::open(dir)
        } else {
            Ok(KeyStore {
                path: dir.join(STORE_FILE),
                slots: BTreeMap::new(),
            })
        }
    }

    /// Every completed slot as one `done` event (`adapter`, `key`, `payload_hash`,
    /// `request_hash`, `outcome`, `failure`), in (adapter, key) order; pending slots are omitted.
    pub fn done_events(&self) -> Vec<Value> {
        self.slots
            .iter()
            .filter_map(|((adapter, key), slot)| match slot {
                Slot::Done {
                    payload_hash,
                    request_hash,
                    outcome,
                } => {
                    let (ok, failure) = outcome.fields();
                    Some(json!({"event": "done", "adapter": adapter, "key": key, "payload_hash": payload_hash, "request_hash": request_hash, "outcome": ok, "failure": failure}))
                }
                Slot::Pending { .. } => None,
            })
            .collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tmp(name: &str) -> PathBuf {
        let d = std::env::temp_dir().join(format!("cwr-idem-{name}-{}", std::process::id()));
        let _ = fs::remove_dir_all(&d);
        d
    }

    #[test]
    fn key_store_replay_duplicate_and_persistence() {
        let d = tmp("a");
        let mut s = KeyStore::open(&d).unwrap();
        let p1: Map<String, Value> =
            serde_json::from_value(json!({"key": "k1", "title": "T", "body": "a"})).unwrap();
        let p2: Map<String, Value> =
            serde_json::from_value(json!({"key": "k1", "title": "T", "body": "b"})).unwrap();
        let (k, h1, h2) = (
            key_of(&p1, "key").unwrap(),
            payload_hash(&p1, "key"),
            payload_hash(&p2, "key"),
        );
        assert_ne!(h1, h2);
        assert_eq!(s.lookup("publish.post", &k, &h1), Lookup::Miss);
        s.begin("publish.post", &k, &h1, "sha256:r1").unwrap();
        assert_eq!(s.lookup("publish.post", &k, &h1), Lookup::Pending);
        // A defect releases the key.
        s.clear("publish.post", &k).unwrap();
        assert_eq!(s.lookup("publish.post", &k, &h1), Lookup::Miss);
        s.begin("publish.post", &k, &h1, "sha256:r1").unwrap();
        let receipt = Stored::Ok(json!({"post_id": "p1", "key": "k1", "accepted_at": 3}));
        s.complete("publish.post", &k, &h1, "sha256:r1", &receipt)
            .unwrap();
        // A failed first outcome is stored too.
        let p3: Map<String, Value> =
            serde_json::from_value(json!({"key": "k2", "title": "T", "body": "a"})).unwrap();
        let (k2, h3) = (key_of(&p3, "key").unwrap(), payload_hash(&p3, "key"));
        s.begin("publish.post", &k2, &h3, "sha256:r3").unwrap();
        let failed = Stored::Fail {
            code: "TIMEOUT".into(),
            retry_after: None,
            detail: "no response".into(),
        };
        s.complete("publish.post", &k2, &h3, "sha256:r3", &failed)
            .unwrap();
        drop(s);
        let s = KeyStore::open(&d).unwrap();
        assert_eq!(
            s.lookup("publish.post", &k, &h1),
            Lookup::Replay(receipt.clone())
        );
        assert_eq!(
            s.lookup("publish.post", &k, &h2),
            Lookup::Duplicate { original: receipt }
        );
        assert_eq!(
            s.lookup("publish.post", &k2, &h3),
            Lookup::Replay(failed.clone())
        );
        assert!(failed.describe().contains("TIMEOUT"));
        assert!(s.pending().is_empty());
        let _ = fs::remove_dir_all(&d);
    }

    #[test]
    fn read_lists_done_slots_without_creating_anything() {
        let d = tmp("c");
        let empty = KeyStore::read(&d).unwrap();
        assert!(empty.done_events().is_empty());
        assert!(!d.exists());
        let mut s = KeyStore::open(&d).unwrap();
        s.begin("publish.post", "\"a\"", "sha256:pa", "sha256:ra")
            .unwrap();
        s.begin("publish.post", "\"b\"", "sha256:pb", "sha256:rb")
            .unwrap();
        let receipt = Stored::Ok(json!({"post_id": "p1"}));
        s.complete("publish.post", "\"b\"", "sha256:pb", "sha256:rb", &receipt)
            .unwrap();
        drop(s);
        let events = KeyStore::read(&d).unwrap().done_events();
        assert_eq!(
            events,
            vec![
                json!({"event": "done", "adapter": "publish.post", "key": "\"b\"",
                "payload_hash": "sha256:pb", "request_hash": "sha256:rb",
                "outcome": {"post_id": "p1"}, "failure": null})
            ]
        );
        let _ = fs::remove_dir_all(&d);
    }

    #[test]
    fn crash_leaves_pending_and_torn_line_is_ignored() {
        let d = tmp("b");
        let mut s = KeyStore::open(&d).unwrap();
        s.begin("publish.post", "\"k\"", "sha256:p", "sha256:r")
            .unwrap();
        drop(s);
        let mut f = OpenOptions::new()
            .append(true)
            .open(d.join(STORE_FILE))
            .unwrap();
        f.write_all(b"{\"event\":\"do").unwrap();
        let s = KeyStore::open(&d).unwrap();
        assert_eq!(
            s.pending(),
            vec![(
                "publish.post".to_string(),
                "\"k\"".to_string(),
                "sha256:r".to_string()
            )]
        );
        let _ = fs::remove_dir_all(&d);
    }
}
