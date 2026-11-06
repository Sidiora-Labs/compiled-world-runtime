// Copyright Sidiora Labs Inc. CWR® 2026-2027
// SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0

//! Content-addressed snapshots (kernel.md section 9, clause 8.2).
//!
//! A snapshot captures the typed state store, the ledger `seq` (and that record's `chain`, which
//! pins the lineage), `world_time`, `turn`, the RNG state, the tail size K, the seed and the
//! Build hash. The file is canonical JSON plus a newline; its name is the sha256 of its bytes.
//! It is written to a temporary file, fsynced and renamed, so a crash mid-write leaves either
//! no file or a file that fails hash verification and is discarded.

use std::fs;
use std::path::{Path, PathBuf};

use serde_json::{json, Value};

use crate::ledger::TailSize;
use crate::rng::RngState;
use crate::state::WorldState;
use crate::{canonical_json, io_err, sha256_hex, write_atomic, KernelError, Result};

/// The `format` value of a snapshot file.
pub const SNAPSHOT_FORMAT: &str = "cwr.snapshot";
/// The snapshot schema version this kernel reads and writes.
pub const SNAPSHOT_SCHEMA_VERSION: u64 = 1;

/// A capture of an Instance at one durable ledger record, enough to resume it exactly.
#[derive(Debug, Clone, PartialEq)]
pub struct Snapshot {
    /// Id of the captured Instance.
    pub instance: String,
    /// Hash of the Build the Instance ran.
    pub build: String,
    /// Seq of the ledger record the capture is at.
    pub seq: u64,
    /// Chain hash of that record.
    pub chain: String,
    /// World time at the capture.
    pub world_time: u64,
    /// Last completed turn at the capture.
    pub turn: u64,
    /// RNG state at the capture.
    pub rng: RngState,
    /// Ledger tail size of the Instance.
    pub tail_size: TailSize,
    /// Seed the Instance was created with.
    pub seed: u64,
    /// Per-module state at the capture.
    pub state: WorldState,
}

impl Snapshot {
    /// The JSON form, including the state and its hash.
    pub fn to_json(&self) -> Value {
        json!({
            "format": SNAPSHOT_FORMAT,
            "schema_version": SNAPSHOT_SCHEMA_VERSION,
            "instance": self.instance,
            "build": self.build,
            "seq": self.seq,
            "chain": self.chain,
            "world_time": self.world_time,
            "turn": self.turn,
            "rng": self.rng.to_json(),
            "tail_size": self.tail_size.to_json(),
            "seed": self.seed,
            "state_hash": self.state.hash(),
            "state": self.state.to_json(),
        })
    }

    /// Canonical JSON bytes of the file, newline-terminated; identical for identical snapshots.
    pub fn bytes(&self) -> Vec<u8> {
        let mut s = canonical_json(&self.to_json());
        s.push('\n');
        s.into_bytes()
    }

    /// `sha256:<hex>` over the snapshot file bytes.
    pub fn hash(&self) -> String {
        format!("sha256:{}", sha256_hex(&self.bytes()))
    }

    /// Writes `<dir>/<hex>.json` atomically and returns the snapshot hash.
    pub fn write(&self, dir: &Path) -> Result<String> {
        fs::create_dir_all(dir).map_err(|e| io_err("create", dir, e))?;
        let bytes = self.bytes();
        let hex = sha256_hex(&bytes);
        write_atomic(&dir.join(format!("{hex}.json")), &bytes)?;
        Ok(format!("sha256:{hex}"))
    }

    /// File name of the snapshot with hash `hash` in a snapshots directory.
    pub fn file_name(hash: &str) -> String {
        format!("{}.json", hash.trim_start_matches("sha256:"))
    }

    /// Loads and verifies a snapshot file: its bytes must hash to its file name and the inline
    /// state must hash to `state_hash`. Any mismatch is [`KernelError::HashMismatch`].
    pub fn load(path: &Path) -> Result<(Snapshot, String)> {
        let bytes = fs::read(path).map_err(|e| io_err("read snapshot", path, e))?;
        let hex = sha256_hex(&bytes);
        let stem = path.file_stem().and_then(|s| s.to_str()).unwrap_or("");
        if stem != hex {
            return Err(KernelError::HashMismatch(format!(
                "snapshot {} hashes to sha256:{hex}",
                path.display()
            )));
        }
        let v: Value = serde_json::from_slice(&bytes).map_err(|e| {
            KernelError::HashMismatch(format!("snapshot {} is not JSON: {e}", path.display()))
        })?;
        let bad = |m: &str| KernelError::Refused(format!("snapshot {}: {m}", path.display()));
        if v["format"] != SNAPSHOT_FORMAT || v["schema_version"] != SNAPSHOT_SCHEMA_VERSION {
            return Err(bad("unknown format or schema_version"));
        }
        let s = |k: &str| {
            v[k].as_str()
                .map(str::to_string)
                .ok_or_else(|| bad(&format!("{k} missing")))
        };
        let n = |k: &str| v[k].as_u64().ok_or_else(|| bad(&format!("{k} missing")));
        let state = WorldState::from_json(&v["state"])?;
        if v["state_hash"].as_str() != Some(state.hash().as_str()) {
            return Err(KernelError::HashMismatch(format!(
                "snapshot {} state does not match state_hash",
                path.display()
            )));
        }
        let snap = Snapshot {
            instance: s("instance")?,
            build: s("build")?,
            seq: n("seq")?,
            chain: s("chain")?,
            world_time: n("world_time")?,
            turn: n("turn")?,
            rng: RngState::from_json(&v["rng"])?,
            tail_size: TailSize::from_json(&v["tail_size"])?,
            seed: n("seed")?,
            state,
        };
        if snap.bytes() != bytes {
            return Err(KernelError::HashMismatch(format!(
                "snapshot {} is not in canonical form",
                path.display()
            )));
        }
        Ok((snap, format!("sha256:{hex}")))
    }

    /// Resolves a snapshot reference: a path to a file, or a hash (with or without `sha256:`)
    /// looked up in `dir`.
    pub fn resolve(reference: &str, dir: &Path) -> PathBuf {
        let p = Path::new(reference);
        if p.is_file() {
            return p.to_path_buf();
        }
        dir.join(Snapshot::file_name(reference))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::rng::Pcg32;

    #[test]
    fn roundtrip_and_tamper() {
        let dir = std::env::temp_dir().join(format!("cwr-snap-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        let mut state = WorldState::empty(&["kv".to_string(), "world".to_string()]);
        state.set("kv", json!({"a": 1})).unwrap();
        let snap = Snapshot {
            instance: "I".into(),
            build: "sha256:00".into(),
            seq: 4,
            chain: "sha256:11".into(),
            world_time: 12,
            turn: 1,
            rng: Pcg32::seeded(u64::MAX).state(),
            tail_size: TailSize::Full,
            seed: u64::MAX,
            state,
        };
        let h = snap.write(&dir).unwrap();
        let path = dir.join(Snapshot::file_name(&h));
        let (back, h2) = Snapshot::load(&path).unwrap();
        assert_eq!(back, snap);
        assert_eq!(h, h2);
        let text = fs::read_to_string(&path)
            .unwrap()
            .replace("\"world_time\":12", "\"world_time\":13");
        fs::write(&path, text).unwrap();
        assert!(matches!(
            Snapshot::load(&path),
            Err(KernelError::HashMismatch(_))
        ));
    }
}
