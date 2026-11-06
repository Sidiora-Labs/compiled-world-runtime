// Copyright Sidiora Labs Inc. CWR® 2026-2027
// SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0

//! CWR World Kernel.
//!
//! Spec: spec/cwr/kernel.md, spec/cwr/model-surface.md section 6, spec/cwr/compiler.md section 7.
//!
//! Foundations (task 2.1, case CWR-006):
//!
//! * [`state`]: the Build reference (hash-verified before load), the typed per-module world
//!   state, the content-addressed object store and the Instance store with its lifecycle
//!   (`create`, `snapshot`, `restore`, `destroy`) and a kernel-direct probe turn over the
//!   kernel-owned `world.*` facilities.
//! * [`ledger`]: the append-only, hash-chained JSONL ledger with fsync before return, the
//!   strict open that refuses a truncated final record, the ledger tail renderer and the
//!   `world.history` view.
//! * [`clock`]: the world clock in ticks; the only time an Instance can observe.
//! * [`rng`]: the per-Instance PCG32 stream, advanced only by draws.
//! * [`snapshot`]: content-addressed snapshots carrying state, seq, world_time, turn, RNG
//!   state, K and Build hash.
//!
//! Nothing in this crate writes into a Build directory; Builds are read and verified only.

#![warn(missing_docs)]

pub mod clock;
pub mod ledger;
pub mod rng;
pub mod snapshot;
pub mod state;
// Task 2.2: sandboxed executor and atomic contract check.
pub mod contract_check;
pub mod envelope;
pub mod executor;
pub mod limits;
pub mod rpc;
pub mod sandbox;
// Task 2.3: adapter gateway (normalisation, idempotency, cassette modes, timeouts).
pub mod gateway;
// Task 2.4: crash recovery and ledger replay.
pub mod recovery;
pub mod replay;
// Task 2.5: instance upgrade, migrations, recompile notice and proposal outbox.
pub mod migrate;
pub mod notice;
pub mod outbox;
pub mod upgrade;
// Task 3.2: drift injection for --x-drift experimental Builds.
pub mod drift_injector;
// Task 3.1: the model surface (context assembly, ledger tail, turn protocol, envelope rendering).
/// The model surface: context assembly, ledger tail, turn protocol and envelope rendering.
pub mod surface {
    pub mod context;
    pub mod envelope_render;
    pub mod protocol;
    pub mod tail;
}

use std::fmt;
use std::fs;
use std::io::{Read, Write};
use std::path::Path;

use serde_json::Value;
use sha2::{Digest, Sha256};

pub use clock::{TickConfig, WorldClock};
pub use executor::{run_turn, TurnOptions, TurnReport};
pub use ledger::{Kind, Ledger, LedgerScan, Outcome, PreconditionEval, RecordDraft, TailSize};
pub use rng::{Pcg32, RngState};
pub use snapshot::Snapshot;
pub use state::{Build, CreateOptions, Instance, InstanceMeta, ObjectStore, ProbeCall, WorldState};

/// Kernel errors. Each maps to an operator exit code (kernel.md section 2).
#[derive(Debug)]
pub enum KernelError {
    /// Build or snapshot content does not match its recorded hash (exit 2).
    HashMismatch(String),
    /// Experimental Build loaded without `--allow-experimental` (exit 3).
    Experimental(String),
    /// The ledger's final record is truncated; only restore/recovery may open it.
    Truncated(String),
    /// The ledger fails its seq or chain check.
    Corrupt(String),
    /// The Instance is not in a state that allows the operation.
    State(String),
    /// A request the kernel refuses (unknown primitive, bad argument, lineage mismatch).
    Refused(String),
    /// Host I/O failure.
    Io(String),
}

impl KernelError {
    /// Process exit code of the CLI for this error: 2 hash mismatch, 3 experimental, 1 otherwise.
    pub fn exit_code(&self) -> i32 {
        match self {
            KernelError::HashMismatch(_) => 2,
            KernelError::Experimental(_) => 3,
            _ => 1,
        }
    }
}

impl fmt::Display for KernelError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            KernelError::HashMismatch(m) => write!(f, "hash mismatch: {m}"),
            KernelError::Experimental(m) => write!(f, "experimental build refused: {m}"),
            KernelError::Truncated(m) => write!(f, "ledger truncated: {m}"),
            KernelError::Corrupt(m) => write!(f, "ledger corrupt: {m}"),
            KernelError::State(m) => write!(f, "instance state: {m}"),
            KernelError::Refused(m) => write!(f, "refused: {m}"),
            KernelError::Io(m) => write!(f, "io: {m}"),
        }
    }
}

impl std::error::Error for KernelError {}

/// Result of every fallible kernel operation.
pub type Result<T> = std::result::Result<T, KernelError>;

pub(crate) fn io_err(what: &str, path: &Path, e: std::io::Error) -> KernelError {
    KernelError::Io(format!("{what} {}: {e}", path.display()))
}

/// Canonical JSON: object keys sorted bytewise at every level, no insignificant whitespace.
/// Independent of serde_json's map ordering features so hashes are stable.
pub fn canonical_json(v: &Value) -> String {
    let mut out = String::new();
    write_canonical(v, &mut out);
    out
}

fn write_canonical(v: &Value, out: &mut String) {
    match v {
        Value::Object(map) => {
            let mut entries: Vec<(&String, &Value)> = map.iter().collect();
            entries.sort_by(|x, y| x.0.cmp(y.0));
            out.push('{');
            for (i, (k, item)) in entries.into_iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                out.push_str(&Value::from(k.as_str()).to_string());
                out.push(':');
                write_canonical(item, out);
            }
            out.push('}');
        }
        Value::Array(items) => {
            out.push('[');
            for (i, item) in items.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                write_canonical(item, out);
            }
            out.push(']');
        }
        other => out.push_str(&other.to_string()),
    }
}

/// Lower-case hex SHA-256 of `bytes`.
pub fn sha256_hex(bytes: &[u8]) -> String {
    let digest = Sha256::digest(bytes);
    digest.iter().map(|b| format!("{b:02x}")).collect()
}

/// `blake3:<hex>` of the codec-serialised parameters (kernel.md section 8, `params_hash`).
/// Until the SDK codec of task 1.3 exists the codec form is canonical JSON.
pub fn params_hash(params: &Value) -> String {
    format!(
        "blake3:{}",
        blake3::hash(canonical_json(params).as_bytes()).to_hex()
    )
}

/// Host timestamp in RFC 3339 UTC with microseconds. Written only to `host_ts`, which is
/// never rendered to the model and never returned by any primitive.
pub fn host_ts() -> String {
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default();
    let secs = now.as_secs() as i64;
    let micros = now.subsec_micros();
    let days = secs.div_euclid(86_400);
    let sod = secs.rem_euclid(86_400);
    let (y, m, d) = civil_from_days(days);
    format!(
        "{y:04}-{m:02}-{d:02}T{:02}:{:02}:{:02}.{micros:06}Z",
        sod / 3600,
        (sod % 3600) / 60,
        sod % 60
    )
}

/// Days since 1970-01-01 to (year, month, day), proleptic Gregorian (H. Hinnant's algorithm).
fn civil_from_days(z: i64) -> (i64, u32, u32) {
    let z = z + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z.rem_euclid(146_097);
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = (doy - (153 * mp + 2) / 5 + 1) as u32;
    let m = if mp < 10 { mp + 3 } else { mp - 9 } as u32;
    (if m <= 2 { y + 1 } else { y }, m, d)
}

/// A new ULID (48-bit millisecond timestamp, 80 random bits, Crockford base32). The id is an
/// operator handle; it is stripped from every record `world.history` returns because its time
/// component is host-derived.
pub fn new_ulid() -> String {
    const ALPHABET: &[u8; 32] = b"0123456789ABCDEFGHJKMNPQRSTVWXYZ";
    let ms = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis();
    let mut rand = [0u8; 10];
    let filled = fs::File::open("/dev/urandom")
        .and_then(|mut f| f.read_exact(&mut rand))
        .is_ok();
    if !filled {
        // Fallback entropy: address-space and time jitter; uniqueness is all an id needs.
        let mix = std::time::Instant::now().elapsed().as_nanos()
            ^ (&rand as *const _ as u128)
            ^ (std::process::id() as u128) << 64;
        rand.copy_from_slice(&mix.to_le_bytes()[..10]);
    }
    let mut value: u128 = (ms & ((1u128 << 48) - 1)) << 80;
    for (i, b) in rand.iter().enumerate() {
        value |= (*b as u128) << (8 * (9 - i));
    }
    (0..26)
        .map(|i| ALPHABET[((value >> (5 * (25 - i))) & 31) as usize] as char)
        .collect()
}

/// Writes `bytes` to `path` atomically: temp file in the same directory, fsync, rename, fsync dir.
pub(crate) fn write_atomic(path: &Path, bytes: &[u8]) -> Result<()> {
    let dir = path
        .parent()
        .ok_or_else(|| KernelError::Io(format!("no parent for {}", path.display())))?;
    let tmp = dir.join(format!(
        ".{}.tmp",
        path.file_name().and_then(|n| n.to_str()).unwrap_or("file")
    ));
    {
        let mut f = fs::File::create(&tmp).map_err(|e| io_err("create", &tmp, e))?;
        f.write_all(bytes).map_err(|e| io_err("write", &tmp, e))?;
        f.sync_all().map_err(|e| io_err("fsync", &tmp, e))?;
    }
    fs::rename(&tmp, path).map_err(|e| io_err("rename", path, e))?;
    sync_dir(dir)
}

pub(crate) fn sync_dir(dir: &Path) -> Result<()> {
    fs::File::open(dir)
        .and_then(|d| d.sync_all())
        .map_err(|e| io_err("fsync dir", dir, e))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn canonical_json_sorts_keys() {
        let v: Value =
            serde_json::from_str(r#"{"b":1,"a":{"d":[1,{"z":0,"y":1}],"c":"x"}}"#).unwrap();
        assert_eq!(
            canonical_json(&v),
            r#"{"a":{"c":"x","d":[1,{"y":1,"z":0}]},"b":1}"#
        );
    }

    #[test]
    fn civil_dates() {
        assert_eq!(civil_from_days(0), (1970, 1, 1));
        assert_eq!(civil_from_days(20_398), (2025, 11, 6));
        assert_eq!(civil_from_days(-1), (1969, 12, 31));
    }

    #[test]
    fn ulid_shape() {
        let a = new_ulid();
        assert_eq!(a.len(), 26);
        assert_ne!(a, new_ulid());
    }
}
