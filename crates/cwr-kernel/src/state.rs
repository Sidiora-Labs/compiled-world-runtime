// Copyright Sidiora Labs Inc. CWR® 2026-2027
// SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0

//! Build reference, typed world state, content-addressed object store and the Instance store
//! (kernel.md sections 1, 2, 6, 8 and 9; clauses 1.5, 6.6, 8.1, 8.2 and 9.4).
//!
//! An Instance lives in `<home>/instances/<ULID>/`, never inside a Build:
//!
//! ```text
//! instance.json        metadata: Build reference, lifecycle status, seed, K, arm, model,
//!                      turn, world_time, last seq and chain, RNG state, state hash
//! ledger.jsonl         the append-only, hash-chained ledger (crate::ledger)
//! objects/<hex>        content-addressed world-state blobs (canonical JSON, sha256 name)
//! snapshots/<hex>.json content-addressed snapshots (crate::snapshot)
//! retained/            ledger lineages abandoned by a restore, kept for inspection
//! lock                 advisory lock held for the duration of any operation
//! ```
//!
//! The Build is identified and verified by the hash algorithm of compiler.md section 7 before
//! any Instance is created or opened against it, and is only ever read.

use std::collections::{BTreeMap, BTreeSet};
use std::fs::{self, File};
use std::path::{Path, PathBuf};
use std::time::Instant;

use cwr_contract::kvx::{self, Json};
use cwr_contract::Code;
use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};

use crate::clock::{TickConfig, WorldClock};
use crate::envelope::{trace_item, turn_envelope, Failure, Resources};
use crate::ledger::{history_view, Kind, Ledger, Outcome, PreconditionEval, RecordDraft, TailSize};
use crate::rng::{Pcg32, RngState};
use crate::snapshot::Snapshot;
use crate::{
    canonical_json, io_err, params_hash, sha256_hex, sync_dir, write_atomic, KernelError, Result,
};

// ---------------------------------------------------------------------------------------------
// Build reference
// ---------------------------------------------------------------------------------------------

/// Top-level entries of a Build directory that are inside its hash (compiler.md section 7). A Build
/// directory holds no other top-level entry; any other entry fails verification.
pub const IN_HASH_FILES: [&str; 5] = [
    "manifest.kvx",
    "contracts.json",
    "contract_diff.json",
    "manual.md",
    "tests.json",
];
/// Top-level directories of a Build directory whose contents are inside its hash.
pub const IN_HASH_DIRS: [&str; 5] = ["sdk", "lib", "adapters", "fixtures", "migrations"];

/// What the kernel needs from a primitive's compiled contract.
#[derive(Debug, Clone, PartialEq)]
pub struct PrimitiveInfo {
    /// Declared side effect: `read_only`, `mutating` or `external`.
    pub side_effect: String,
    /// Declared return type as written.
    pub returns: String,
    /// Declared parameters as compiled (name to type).
    pub params: Value,
    /// Declared preconditions as (source, failure code name).
    pub preconditions: Vec<(String, String)>,
}

/// A loaded, hash-verified Build. Read-only.
#[derive(Debug, Clone)]
pub struct Build {
    /// Directory the Build was loaded from.
    pub dir: PathBuf,
    /// `sha256:<64hex>`.
    pub hash: String,
    /// World name.
    pub name: String,
    /// World version.
    pub version: String,
    /// Experimental flag name, None for a control Build.
    pub experimental: Option<String>,
    /// Modules in declared order.
    pub modules: Vec<String>,
    /// Compiled per-turn limits by name.
    pub limits: BTreeMap<String, u64>,
    /// Compiled clock ticks.
    pub clock: TickConfig,
    /// Whether `world.seed` may reveal the seed to the model.
    pub seed_visible: bool,
    /// Every primitive and adapter the Build enumerates, by qualified name.
    pub primitives: BTreeMap<String, PrimitiveInfo>,
}

fn is_hex64(s: &str) -> bool {
    s.len() == 64
        && s.bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}

/// The recorded Build hash of `manifest.kvx` (compiler.md section 7): the file's last line, which
/// must be exactly `hash = "sha256:<64 lowercase hex>"` followed by one newline. Returns the hex
/// digits, or None when the last line is anything else (other spacing, a comment, `\r`, another
/// key), so the one line excluded from the hash has a single possible spelling.
pub fn manifest_recorded_hash(bytes: &[u8]) -> Option<String> {
    let body = bytes.strip_suffix(b"\n")?;
    let start = body
        .iter()
        .rposition(|b| *b == b'\n')
        .map(|i| i + 1)
        .unwrap_or(0);
    let line = std::str::from_utf8(&body[start..]).ok()?;
    let hex = line.strip_prefix("hash = \"sha256:")?.strip_suffix('"')?;
    is_hex64(hex).then(|| hex.to_string())
}

/// `manifest.kvx` bytes as hashed: only the recorded hash line ([`manifest_recorded_hash`]) is
/// removed. Every other byte, including any other `hash` key in any section, enters the hash.
fn manifest_without_hash(bytes: &[u8]) -> Vec<u8> {
    match manifest_recorded_hash(bytes) {
        Some(_) => {
            let body = &bytes[..bytes.len() - 1];
            let start = body
                .iter()
                .rposition(|b| *b == b'\n')
                .map(|i| i + 1)
                .unwrap_or(0);
            bytes[..start].to_vec()
        }
        None => bytes.to_vec(),
    }
}

fn collect_files(root: &Path, rel: &str, out: &mut Vec<String>) -> Result<()> {
    let dir = root.join(rel);
    let before = out.len();
    let mut entries: Vec<_> = fs::read_dir(&dir)
        .map_err(|e| io_err("read dir", &dir, e))?
        .collect::<std::result::Result<_, _>>()
        .map_err(|e| io_err("read dir", &dir, e))?;
    entries.sort_by_key(|e| e.file_name());
    for e in entries {
        let name = e.file_name().into_string().map_err(|_| {
            KernelError::Refused(format!("non-UTF-8 file name in {}", dir.display()))
        })?;
        let ft = e
            .file_type()
            .map_err(|err| io_err("stat", &e.path(), err))?;
        let child = format!("{rel}/{name}");
        if ft.is_symlink() {
            return Err(KernelError::HashMismatch(format!(
                "Build contains a symbolic link {child}"
            )));
        } else if ft.is_dir() {
            collect_files(root, &child, out)?;
        } else if ft.is_file() {
            out.push(child);
        } else {
            return Err(KernelError::HashMismatch(format!(
                "Build contains a special file {child}"
            )));
        }
    }
    // An empty directory is an entry of its own (`<path>/`, zero bytes), so adding or removing
    // one changes the hash (compiler.md section 7).
    if out.len() == before {
        out.push(format!("{rel}/"));
    }
    Ok(())
}

fn json_from_kvx(j: &Json) -> Value {
    j.to_value()
}

impl Build {
    /// The Build hash of compiler.md section 7, as 64 hex digits.
    pub fn compute_hash(dir: &Path) -> Result<String> {
        let mut files: Vec<String> = Vec::new();
        let rd = fs::read_dir(dir).map_err(|e| io_err("read dir", dir, e))?;
        for e in rd {
            let e = e.map_err(|err| io_err("read dir", dir, err))?;
            let name = e.file_name().to_string_lossy().into_owned();
            let ft = e
                .file_type()
                .map_err(|err| io_err("stat", &e.path(), err))?;
            let ok = (IN_HASH_FILES.contains(&name.as_str()) && ft.is_file())
                || (IN_HASH_DIRS.contains(&name.as_str()) && ft.is_dir());
            if !ok {
                return Err(KernelError::HashMismatch(format!(
                    "Build {} has an unexpected top-level entry {name:?}",
                    dir.display()
                )));
            }
        }
        for f in IN_HASH_FILES {
            let p = dir.join(f);
            if p.is_file() {
                files.push(f.to_string());
            } else if f == "manifest.kvx" || f == "contracts.json" {
                return Err(KernelError::HashMismatch(format!(
                    "Build {} lacks {f}",
                    dir.display()
                )));
            }
        }
        for d in IN_HASH_DIRS {
            if dir.join(d).is_dir() {
                collect_files(dir, d, &mut files)?;
            }
        }
        files.sort_by(|a, b| a.as_bytes().cmp(b.as_bytes()));
        let mut h = Sha256::new();
        for f in &files {
            let p = dir.join(f);
            let mut bytes = if f.ends_with('/') {
                Vec::new()
            } else {
                fs::read(&p).map_err(|e| io_err("read", &p, e))?
            };
            if f == "manifest.kvx" {
                bytes = manifest_without_hash(&bytes);
            }
            h.update(f.as_bytes());
            h.update([0u8]);
            h.update(bytes.len().to_string().as_bytes());
            h.update([0u8]);
            h.update(&bytes);
        }
        Ok(h.finalize().iter().map(|b| format!("{b:02x}")).collect())
    }

    /// Resolves a Build reference: an existing directory, or a hash looked up as
    /// `<root>/<hex>` in each of `roots`.
    pub fn resolve(reference: &str, roots: &[PathBuf]) -> Result<PathBuf> {
        let p = Path::new(reference);
        if p.is_dir() {
            return Ok(p.to_path_buf());
        }
        let hex = reference.trim_start_matches("sha256:");
        if is_hex64(hex) {
            for r in roots {
                let c = r.join(hex);
                if c.is_dir() {
                    return Ok(c);
                }
            }
        }
        Err(KernelError::Refused(format!(
            "no Build directory for {reference:?}"
        )))
    }

    /// Verifies the hash, then reads `manifest.kvx` and `contracts.json`.
    pub fn load(dir: &Path) -> Result<Build> {
        let dir = fs::canonicalize(dir).map_err(|e| io_err("resolve Build", dir, e))?;
        let computed = Self::compute_hash(&dir)?;
        let manifest_path = dir.join("manifest.kvx");
        let text =
            fs::read_to_string(&manifest_path).map_err(|e| io_err("read", &manifest_path, e))?;
        let doc = kvx::parse("manifest.kvx", &text).map_err(|errs| {
            KernelError::HashMismatch(format!(
                "manifest.kvx does not parse: {}",
                errs.iter()
                    .map(|e| e.to_string())
                    .collect::<Vec<_>>()
                    .join("; ")
            ))
        })?;
        let key = |k: &str| -> Option<Value> {
            doc.sections
                .iter()
                .find_map(|s| s.get(k))
                .map(|e| json_from_kvx(&e.value.value))
        };
        let recorded = manifest_recorded_hash(text.as_bytes()).ok_or_else(|| {
            KernelError::HashMismatch(
                "manifest.kvx does not end with its recorded hash line `hash = \"sha256:<hex>\"`"
                    .into(),
            )
        })?;
        if recorded != computed {
            return Err(KernelError::HashMismatch(format!(
                "Build {} hashes to sha256:{computed}, manifest records sha256:{recorded}",
                dir.display()
            )));
        }
        let base = dir.file_name().and_then(|n| n.to_str()).unwrap_or("");
        if is_hex64(base) && base != computed {
            return Err(KernelError::HashMismatch(format!(
                "Build directory {base} hashes to sha256:{computed}"
            )));
        }
        let cpath = dir.join("contracts.json");
        let contracts: Value =
            serde_json::from_slice(&fs::read(&cpath).map_err(|e| io_err("read", &cpath, e))?)
                .map_err(|e| KernelError::Refused(format!("contracts.json: {e}")))?;
        let find = |k: &str| -> Option<&Value> {
            contracts
                .get(k)
                .or_else(|| contracts.get("world").and_then(|w| w.get(k)))
                .or_else(|| contracts.pointer(&format!("/world/world/{k}")))
        };
        let bad = |m: String| KernelError::Refused(format!("Build {}: {m}", dir.display()));
        let name = key("name")
            .and_then(|v| v.as_str().map(str::to_string))
            .or_else(|| find("name").and_then(|v| v.as_str().map(str::to_string)))
            .ok_or_else(|| bad("no world name".into()))?;
        let version = key("version")
            .and_then(|v| v.as_str().map(str::to_string))
            .or_else(|| find("version").and_then(|v| v.as_str().map(str::to_string)))
            .ok_or_else(|| bad("no version".into()))?;
        let experimental = match key("experimental") {
            Some(Value::Bool(true)) => Some(
                key("experimental_flag")
                    .and_then(|v| v.as_str().map(str::to_string))
                    .unwrap_or_else(|| "experimental".into()),
            ),
            _ => None,
        };
        let modules: Vec<String> = find("modules")
            .and_then(Value::as_array)
            .map(|a| {
                a.iter()
                    .filter_map(|m| m.as_str().map(str::to_string))
                    .collect()
            })
            .ok_or_else(|| bad("contracts.json lacks modules".into()))?;
        let mut limits = BTreeMap::new();
        let lv = find("limits").ok_or_else(|| bad("contracts.json lacks limits".into()))?;
        for n in cwr_contract::Limits::NAMES {
            let v = lv
                .get(n)
                .and_then(Value::as_u64)
                .filter(|v| *v > 0)
                .ok_or_else(|| bad(format!("limit {n} missing or not positive")))?;
            limits.insert(n.to_string(), v);
        }
        let cv = find("clock").ok_or_else(|| bad("contracts.json lacks clock".into()))?;
        let tick = |k: &str| {
            cv.get(k)
                .and_then(Value::as_u64)
                .ok_or_else(|| bad(format!("clock.{k} missing")))
        };
        let clock = TickConfig::new(tick("tick_per_primitive")?, tick("tick_per_turn")?)?;
        let seed_visible = find("rng")
            .and_then(|r| r.get("seed_visible"))
            .and_then(Value::as_bool)
            .ok_or_else(|| bad("rng.seed_visible missing".into()))?;
        let mut primitives = BTreeMap::new();
        // Strict, like the contract table of the atomic check: a contract that lacks a field or
        // carries a malformed precondition refuses the Build instead of being read partially.
        let mut add = |p: &Value| -> Result<()> {
            let n = p
                .get("name")
                .and_then(Value::as_str)
                .ok_or_else(|| bad("a contracts.json entry lacks name".into()))?;
            let field = |k: &str| {
                p.get(k)
                    .and_then(Value::as_str)
                    .map(str::to_string)
                    .ok_or_else(|| bad(format!("contracts.json: {n} lacks {k}")))
            };
            let side_effect = field("side_effect")?;
            if !["read_only", "mutating", "external"].contains(&side_effect.as_str()) {
                return Err(bad(format!(
                    "contracts.json: {n} has side_effect {side_effect:?}"
                )));
            }
            let mut preconditions = Vec::new();
            for pc in p
                .get("preconditions")
                .and_then(Value::as_array)
                .into_iter()
                .flatten()
            {
                match (
                    pc.get(0).and_then(Value::as_str),
                    pc.get(1).and_then(Value::as_str),
                ) {
                    (Some(src), Some(code)) if code.parse::<Code>().is_ok() => {
                        preconditions.push((src.to_string(), code.to_string()))
                    }
                    _ => {
                        return Err(bad(format!(
                            "contracts.json: {n} has a malformed precondition {pc}"
                        )))
                    }
                }
            }
            primitives.insert(
                n.to_string(),
                PrimitiveInfo {
                    side_effect,
                    returns: field("returns")?,
                    params: p.get("params").cloned().unwrap_or(json!({})),
                    preconditions,
                },
            );
            Ok(())
        };
        for list in ["primitives", "adapters"] {
            if let Some(items) = contracts.get(list).and_then(Value::as_array) {
                for item in items {
                    if item.get("name").is_some() {
                        add(item)?;
                    }
                    for nested in ["primitives", "adapters"] {
                        for p in item
                            .get(nested)
                            .and_then(Value::as_array)
                            .into_iter()
                            .flatten()
                        {
                            add(p)?;
                        }
                    }
                }
            }
        }
        for item in contracts
            .get("contracts")
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
        {
            for p in item
                .get("primitives")
                .and_then(Value::as_array)
                .into_iter()
                .flatten()
            {
                add(p)?;
            }
        }
        Ok(Build {
            dir,
            hash: format!("sha256:{computed}"),
            name,
            version,
            experimental,
            modules,
            limits,
            clock,
            seed_visible,
            primitives,
        })
    }

    /// Clause 10.1 entry check: an experimental Build needs `--allow-experimental`.
    pub fn check_experimental(&self, allow: bool) -> Result<()> {
        match (&self.experimental, allow) {
            (Some(flag), false) => Err(KernelError::Experimental(format!(
                "Build {} is experimental ({flag}); pass --allow-experimental",
                self.hash
            ))),
            _ => Ok(()),
        }
    }
}

// ---------------------------------------------------------------------------------------------
// Typed world state and the object store
// ---------------------------------------------------------------------------------------------

/// Per-module Instance state: exactly the Build's modules, each a JSON object.
#[derive(Debug, Clone, PartialEq)]
pub struct WorldState {
    modules: BTreeMap<String, Value>,
}

impl WorldState {
    /// A state with an empty object for each of `modules`.
    pub fn empty(modules: &[String]) -> Self {
        WorldState {
            modules: modules
                .iter()
                .map(|m| (m.clone(), Value::Object(Map::new())))
                .collect(),
        }
    }

    /// Parses a JSON object of module name to object state; anything else is [`KernelError::Refused`].
    pub fn from_json(v: &Value) -> Result<Self> {
        let obj = v.as_object().ok_or_else(|| {
            KernelError::Refused("world state must be an object of module states".into())
        })?;
        let mut modules = BTreeMap::new();
        for (k, s) in obj {
            if !s.is_object() {
                return Err(KernelError::Refused(format!(
                    "state of module {k} must be an object"
                )));
            }
            modules.insert(k.clone(), s.clone());
        }
        Ok(WorldState { modules })
    }

    /// The JSON object of module name to state.
    pub fn to_json(&self) -> Value {
        Value::Object(
            self.modules
                .iter()
                .map(|(k, v)| (k.clone(), v.clone()))
                .collect(),
        )
    }

    /// Module names in sorted order.
    pub fn modules(&self) -> impl Iterator<Item = &str> {
        self.modules.keys().map(String::as_str)
    }

    /// The state of `module`, or `None` when the state has no such module.
    pub fn get(&self, module: &str) -> Option<&Value> {
        self.modules.get(module)
    }

    /// Replaces one module's state. Unknown modules and non-object states are refused.
    pub fn set(&mut self, module: &str, value: Value) -> Result<()> {
        if !self.modules.contains_key(module) {
            return Err(KernelError::Refused(format!(
                "module {module} is not part of this Build"
            )));
        }
        if !value.is_object() {
            return Err(KernelError::Refused(format!(
                "state of module {module} must be an object"
            )));
        }
        self.modules.insert(module.to_string(), value);
        Ok(())
    }

    /// The state's module set must equal the Build's.
    pub fn check_modules(&self, build: &Build) -> Result<()> {
        let have: BTreeSet<&str> = self.modules().collect();
        let want: BTreeSet<&str> = build.modules.iter().map(String::as_str).collect();
        if have != want {
            return Err(KernelError::Refused(format!(
                "state modules {have:?} differ from Build modules {want:?}"
            )));
        }
        Ok(())
    }

    /// Canonical JSON bytes of the state; identical for equal states.
    pub fn bytes(&self) -> Vec<u8> {
        canonical_json(&self.to_json()).into_bytes()
    }

    /// `sha256:<hex>` of the canonical bytes: the state's content address.
    pub fn hash(&self) -> String {
        format!("sha256:{}", sha256_hex(&self.bytes()))
    }
}

/// Content-addressed blobs: `objects/<hex>` where hex is the sha256 of the bytes.
#[derive(Debug, Clone)]
pub struct ObjectStore {
    dir: PathBuf,
}

impl ObjectStore {
    /// A store rooted at `dir`; nothing is created until the first put.
    pub fn new(dir: PathBuf) -> Self {
        ObjectStore { dir }
    }

    /// Stores `bytes` durably under their hash and returns `sha256:<hex>`; storing the same bytes twice is a no-op.
    pub fn put(&self, bytes: &[u8]) -> Result<String> {
        fs::create_dir_all(&self.dir).map_err(|e| io_err("create", &self.dir, e))?;
        let hex = sha256_hex(bytes);
        let path = self.dir.join(&hex);
        if !path.is_file() || fs::read(&path).map(|b| b != bytes).unwrap_or(true) {
            write_atomic(&path, bytes)?;
        }
        Ok(format!("sha256:{hex}"))
    }

    /// The bytes stored under `hash`, verified against it; a mismatch is [`KernelError::HashMismatch`].
    pub fn get(&self, hash: &str) -> Result<Vec<u8>> {
        let hex = hash.trim_start_matches("sha256:");
        let path = self.dir.join(hex);
        let bytes = fs::read(&path).map_err(|e| io_err("read object", &path, e))?;
        if sha256_hex(&bytes) != hex {
            return Err(KernelError::HashMismatch(format!(
                "object {} does not match its address",
                path.display()
            )));
        }
        Ok(bytes)
    }
}

// ---------------------------------------------------------------------------------------------
// Instance metadata
// ---------------------------------------------------------------------------------------------

/// The `format` value of `instance.json`.
pub const INSTANCE_FORMAT: &str = "cwr.instance";

/// The contents of `instance.json`: an Instance's identity and its position at the last durable record.
#[derive(Debug, Clone, PartialEq)]
pub struct InstanceMeta {
    /// Instance id.
    pub id: String,
    /// `created`, `idle`, `in_turn`, `destroyed` (kernel.md section 2; upgrade and recovery
    /// states belong to later tasks).
    pub status: String,
    /// Hash of the Build the Instance runs.
    pub build: String,
    /// Directory of that Build.
    pub build_dir: PathBuf,
    /// World name of the Build.
    pub world_name: String,
    /// World version of the Build.
    pub world_version: String,
    /// Experimental flag of the Build, None for a control Build.
    pub experimental: Option<String>,
    /// Seed of the world RNG.
    pub seed: u64,
    /// Whether `world.seed` may reveal the seed to the model.
    pub seed_visible: bool,
    /// Ledger tail size shown to the model.
    pub tail_size: TailSize,
    /// Experimental arm label recorded in the header, if any.
    pub arm: Option<String>,
    /// Model label recorded in the header, if any.
    pub model: Option<String>,
    /// Snapshot hash the Instance was created from, if any.
    pub created_from: Option<String>,
    /// Last completed turn.
    pub turn: u64,
    /// World time at the last durable record.
    pub world_time: u64,
    /// Seq and chain of the last durable ledger record.
    pub seq: u64,
    /// Chain hash of the last durable record.
    pub chain: String,
    /// RNG state at the last durable record.
    pub rng: RngState,
    /// Content hash of the state at the last durable record.
    pub state_hash: String,
}

impl InstanceMeta {
    /// The JSON form written to `instance.json`.
    pub fn to_json(&self) -> Value {
        json!({
            "format": INSTANCE_FORMAT,
            "schema_version": 1,
            "id": self.id,
            "status": self.status,
            "build": self.build,
            "build_dir": self.build_dir.to_string_lossy(),
            "world": {"name": self.world_name, "version": self.world_version},
            "experimental": self.experimental,
            "seed": self.seed,
            "seed_visible": self.seed_visible,
            "tail_size": self.tail_size.to_json(),
            "arm": self.arm,
            "model": self.model,
            "created_from": self.created_from,
            "turn": self.turn,
            "world_time": self.world_time,
            "seq": self.seq,
            "chain": self.chain,
            "rng": self.rng.to_json(),
            "state_hash": self.state_hash,
        })
    }

    /// Parses `instance.json`; an unknown format or a missing field is [`KernelError::Refused`].
    pub fn from_json(v: &Value) -> Result<Self> {
        let bad = |m: &str| KernelError::Refused(format!("instance.json: {m}"));
        if v["format"] != INSTANCE_FORMAT || v["schema_version"] != 1 {
            return Err(bad("unknown format or schema_version"));
        }
        let s = |k: &str| {
            v[k].as_str()
                .map(str::to_string)
                .ok_or_else(|| bad(&format!("{k} missing")))
        };
        let n = |k: &str| v[k].as_u64().ok_or_else(|| bad(&format!("{k} missing")));
        let opt = |k: &str| v[k].as_str().map(str::to_string);
        Ok(InstanceMeta {
            id: s("id")?,
            status: s("status")?,
            build: s("build")?,
            build_dir: PathBuf::from(s("build_dir")?),
            world_name: v["world"]["name"]
                .as_str()
                .ok_or_else(|| bad("world.name missing"))?
                .into(),
            world_version: v["world"]["version"]
                .as_str()
                .ok_or_else(|| bad("world.version missing"))?
                .into(),
            experimental: opt("experimental"),
            seed: n("seed")?,
            seed_visible: v["seed_visible"]
                .as_bool()
                .ok_or_else(|| bad("seed_visible missing"))?,
            tail_size: TailSize::from_json(&v["tail_size"])?,
            arm: opt("arm"),
            model: opt("model"),
            created_from: opt("created_from"),
            turn: n("turn")?,
            world_time: n("world_time")?,
            seq: n("seq")?,
            chain: s("chain")?,
            rng: RngState::from_json(&v["rng"])?,
            state_hash: s("state_hash")?,
        })
    }
}

// ---------------------------------------------------------------------------------------------
// Instance
// ---------------------------------------------------------------------------------------------

/// Options of `cwr instance create`.
#[derive(Debug, Clone, Default)]
pub struct CreateOptions {
    /// Seed; [`DEFAULT_SEED`] when absent.
    pub seed: Option<u64>,
    /// Ledger tail size; [`TailSize::DEFAULT`] when absent.
    pub tail: Option<TailSize>,
    /// Experimental arm label recorded in the header.
    pub arm: Option<String>,
    /// Model label recorded in the header.
    pub model: Option<String>,
    /// Snapshot file (or hash) to start from.
    pub from_snapshot: Option<String>,
    /// Initial module state: a JSON object of module name to module state object.
    pub initial: Option<Value>,
    /// Whether an experimental Build may be used.
    pub allow_experimental: bool,
    /// Digest of the cassette set the harness replays against (`sha256:<64 hex>`), recorded in
    /// the header as `cassette_digest` when present (experiments.md section 2); absent otherwise.
    pub cassette_digest: Option<String>,
}

/// Default seed when `--seed` is absent: fixed so that an unseeded Instance is reproducible.
pub const DEFAULT_SEED: u64 = 0;

/// The kernel-owned `world.*` facilities a probe turn can call (refworld lib/world.py reads
/// exactly these through the kernel state API). `world.propose` belongs to the outbox task.
pub const KERNEL_FACILITIES: [&str; 6] = [
    "world.now",
    "world.version",
    "world.seed",
    "world.limits",
    "world.random",
    "world.history",
];
/// A kernel-owned facility of [`KERNEL_FACILITIES`], resolved from its name before a probe turn
/// opens so the turn itself dispatches over a closed set.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Facility {
    Now,
    Version,
    Seed,
    Limits,
    Random,
    History,
}

impl Facility {
    /// The facility named `name`, or `None` when it is not one of [`KERNEL_FACILITIES`].
    fn parse(name: &str) -> Option<Facility> {
        match name {
            "world.now" => Some(Facility::Now),
            "world.version" => Some(Facility::Version),
            "world.seed" => Some(Facility::Seed),
            "world.limits" => Some(Facility::Limits),
            "world.random" => Some(Facility::Random),
            "world.history" => Some(Facility::History),
            _ => None,
        }
    }
}

/// The only world.history precondition the probe evaluates (reference-world.md section 2.5).
pub const HISTORY_PRECONDITION: &str = "n >= 0 and n <= 500";

/// One call of a probe turn.
#[derive(Debug, Clone)]
pub struct ProbeCall {
    /// Qualified name of a kernel-owned `world.*` facility.
    pub primitive: String,
    /// Call parameters.
    pub params: Value,
}

/// An open Instance holding its operation lock.
#[derive(Debug)]
pub struct Instance {
    /// The Instance directory.
    pub dir: PathBuf,
    /// Metadata at the last durable record.
    pub meta: InstanceMeta,
    /// The verified Build the Instance runs.
    pub build: Build,
    /// Current per-module state.
    pub state: WorldState,
    /// Current world clock.
    pub clock: WorldClock,
    /// Current world RNG.
    pub rng: Pcg32,
    ledger: Ledger,
    records: Vec<Value>,
    objects: ObjectStore,
    current_turn: Option<u64>,
    _lock: File,
}

fn instances_dir(home: &Path) -> PathBuf {
    home.join("instances")
}

fn lock_dir(dir: &Path) -> Result<File> {
    let p = dir.join("lock");
    let f = fs::OpenOptions::new()
        .create(true)
        .truncate(false)
        .write(true)
        .open(&p)
        .map_err(|e| io_err("open lock", &p, e))?;
    f.try_lock().map_err(|_| {
        KernelError::Conflict(format!(
            "instance {} is busy (lock held by another operation)",
            dir.display()
        ))
    })?;
    Ok(f)
}

fn read_meta(dir: &Path) -> Result<InstanceMeta> {
    let p = dir.join("instance.json");
    let bytes = fs::read(&p).map_err(|e| io_err("read", &p, e))?;
    let v: Value = serde_json::from_slice(&bytes)
        .map_err(|e| KernelError::Refused(format!("{}: {e}", p.display())))?;
    InstanceMeta::from_json(&v)
}

fn write_meta(dir: &Path, meta: &InstanceMeta) -> Result<()> {
    let mut s = serde_json::to_string_pretty(&meta.to_json())
        .map_err(|e| KernelError::Io(format!("serialise instance metadata: {e}")))?;
    s.push('\n');
    write_atomic(&dir.join("instance.json"), s.as_bytes())
}

fn sha256_value(v: &Value) -> String {
    format!("sha256:{}", sha256_hex(canonical_json(v).as_bytes()))
}

impl Instance {
    /// Directory of Instance `id` under `home`. The id is joined as given: callers holding
    /// operator input use [`Instance::checked_dir`].
    pub fn dir_of(home: &Path, id: &str) -> PathBuf {
        instances_dir(home).join(id)
    }

    /// Directory of Instance `id` under `home`, after checking that `id` is a ULID
    /// ([`crate::is_ulid`]); anything else is refused, so operator input never escapes the store.
    pub fn checked_dir(home: &Path, id: &str) -> Result<PathBuf> {
        if !crate::is_ulid(id) {
            return Err(KernelError::Refused(format!(
                "{id:?} is not an Instance id (a 26-character ULID)"
            )));
        }
        Ok(Self::dir_of(home, id))
    }

    /// `cwr instance create`: verify the Build, write metadata, initial state and the header.
    pub fn create(home: &Path, build_dir: &Path, opts: CreateOptions) -> Result<Instance> {
        Self::create_as(home, build_dir, opts, &crate::new_ulid())
    }

    /// [`Instance::create`] under an id chosen by the operator (`cwr instance replay --into`).
    /// The id must be a ULID ([`crate::is_ulid`]) that names no Instance in `home` yet.
    pub fn create_as(
        home: &Path,
        build_dir: &Path,
        opts: CreateOptions,
        id: &str,
    ) -> Result<Instance> {
        let dir = Self::checked_dir(home, id)?;
        if dir.exists() {
            return Err(KernelError::Refused(format!(
                "an Instance {id} already exists in {}",
                home.display()
            )));
        }
        let build = Build::load(build_dir)?;
        build.check_experimental(opts.allow_experimental)?;
        if let Some(d) = &opts.cassette_digest {
            let hex = d.strip_prefix("sha256:").unwrap_or("");
            if hex.len() != 64
                || !hex
                    .bytes()
                    .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
            {
                return Err(KernelError::Refused(format!(
                    "cassette digest {d:?} is not sha256:<64 lowercase hex>"
                )));
            }
        }
        fs::create_dir_all(instances_dir(home)).map_err(|e| io_err("create", home, e))?;
        let home_abs = fs::canonicalize(home).map_err(|e| io_err("resolve", home, e))?;
        if home_abs.starts_with(&build.dir) {
            return Err(KernelError::Refused(
                "instance store must live outside the Build directory".into(),
            ));
        }

        let mut state = WorldState::empty(&build.modules);
        let (mut seed, mut rng, mut world_time, mut turn, mut tail, mut created_from) = (
            opts.seed.unwrap_or(DEFAULT_SEED),
            None,
            0u64,
            0u64,
            opts.tail.unwrap_or(TailSize::DEFAULT),
            None,
        );
        if let Some(reference) = &opts.from_snapshot {
            if opts.seed.is_some() || opts.initial.is_some() {
                return Err(KernelError::Refused("--from-snapshot carries its own seed and state; --seed and --initial are refused with it".into()));
            }
            let path = Snapshot::resolve(reference, Path::new("."));
            let (snap, hash) = Snapshot::load(&path)?;
            if snap.build != build.hash {
                return Err(KernelError::Refused(format!(
                    "snapshot {hash} belongs to Build {}, not {}",
                    snap.build, build.hash
                )));
            }
            let src_ledger = Self::dir_of(home, &snap.instance).join("ledger.jsonl");
            if src_ledger.is_file() {
                Ledger::scan(&src_ledger)?;
            }
            snap.state.check_modules(&build)?;
            state = snap.state.clone();
            seed = snap.seed;
            rng = Some(snap.rng);
            world_time = snap.world_time;
            turn = snap.turn;
            if opts.tail.is_none() {
                tail = snap.tail_size;
            }
            created_from = Some(hash);
        }
        if let Some(initial) = &opts.initial {
            let obj = initial.as_object().ok_or_else(|| {
                KernelError::Refused("--initial must be a JSON object of module states".into())
            })?;
            for (m, v) in obj {
                state.set(m, v.clone())?;
            }
        }
        let rng = rng
            .map(Pcg32::from_state)
            .unwrap_or_else(|| Pcg32::seeded(seed));

        let id = id.to_string();
        fs::create_dir(&dir).map_err(|e| io_err("create", &dir, e))?;
        sync_dir(&instances_dir(home))?;
        let lock = lock_dir(&dir)?;
        let objects = ObjectStore::new(dir.join("objects"));
        let state_hash = objects.put(&state.bytes())?;
        let mut meta = InstanceMeta {
            id: id.clone(),
            status: "created".into(),
            build: build.hash.clone(),
            build_dir: build.dir.clone(),
            world_name: build.name.clone(),
            world_version: build.version.clone(),
            experimental: build.experimental.clone(),
            seed,
            seed_visible: build.seed_visible,
            tail_size: tail,
            arm: opts.arm.clone(),
            model: opts.model.clone(),
            created_from: created_from.clone(),
            turn,
            world_time,
            seq: 0,
            chain: String::new(),
            rng: rng.state(),
            state_hash: state_hash.clone(),
        };
        write_meta(&dir, &meta)?;
        let mut ledger = Ledger::create(
            &dir.join("ledger.jsonl"),
            &id,
            &build.hash,
            build.experimental.as_deref(),
        )?;
        let header = RecordDraft::new(Kind::Header, turn, world_time)
            .with("seed", json!(seed))
            .with("tail_size", tail.to_json())
            .with("arm", json!(opts.arm))
            .with("model", json!(opts.model))
            .with("created_from", json!(created_from))
            .with("initial_state", json!(state_hash));
        let header = match &opts.cassette_digest {
            Some(d) => header.with("cassette_digest", json!(d)),
            None => header,
        };
        let rec = ledger.append(header)?;
        meta.status = "idle".into();
        meta.chain = rec["chain"].as_str().unwrap_or_default().to_string();
        write_meta(&dir, &meta)?;
        let clock = WorldClock::at(build.clock, world_time);
        Ok(Instance {
            dir,
            meta,
            build,
            state,
            clock,
            rng,
            ledger,
            records: vec![rec],
            objects,
            current_turn: None,
            _lock: lock,
        })
    }

    /// Opens an Instance for operation: lock, verify the Build hash, strict ledger scan,
    /// metadata and state consistency.
    pub fn open(home: &Path, id: &str) -> Result<Instance> {
        let dir = Self::checked_dir(home, id)?;
        if !dir.is_dir() {
            return Err(KernelError::Refused(format!("no instance {id}")));
        }
        let lock = lock_dir(&dir)?;
        let meta = read_meta(&dir)?;
        if meta.status == "destroyed" {
            return Err(KernelError::State(format!(
                "instance {id} is destroyed; its ledger is sealed"
            )));
        }
        let build = Build::load(&meta.build_dir)?;
        if build.hash != meta.build {
            return Err(KernelError::HashMismatch(format!(
                "instance is bound to {}, directory holds {}",
                meta.build, build.hash
            )));
        }
        let (ledger, scan) = Ledger::open(
            &dir.join("ledger.jsonl"),
            id,
            &build.hash,
            build.experimental.as_deref(),
        )?;
        if scan.last_seq() != Some(meta.seq) || scan.last_chain() != Some(meta.chain.as_str()) {
            return Err(KernelError::State(format!(
                "metadata (seq {}) disagrees with the ledger (seq {:?}); recovery required",
                meta.seq,
                scan.last_seq()
            )));
        }
        let objects = ObjectStore::new(dir.join("objects"));
        let bytes = objects.get(&meta.state_hash)?;
        let state = WorldState::from_json(
            &serde_json::from_slice(&bytes)
                .map_err(|e| KernelError::Corrupt(format!("state object: {e}")))?,
        )?;
        state.check_modules(&build)?;
        let clock = WorldClock::at(build.clock, meta.world_time);
        let rng = Pcg32::from_state(meta.rng);
        Ok(Instance {
            dir,
            meta,
            build,
            state,
            clock,
            rng,
            ledger,
            records: scan.records,
            objects,
            current_turn: None,
            _lock: lock,
        })
    }

    /// Ledger records of the live lineage.
    pub fn records(&self) -> &[Value] {
        &self.records
    }

    /// The model-facing ledger tail (component 3 of the context).
    pub fn tail(&self) -> String {
        crate::surface::tail::render(&self.records, self.meta.tail_size)
    }

    /// `world.history(n)`.
    pub fn history(&self, n: usize) -> Vec<Value> {
        history_view(&self.records, n, self.meta.seed_visible)
    }

    fn sync_meta_from_record(&mut self, rec: &Value) -> Result<()> {
        self.meta.seq = rec["seq"].as_u64().unwrap_or(self.meta.seq);
        self.meta.chain = rec["chain"].as_str().unwrap_or_default().to_string();
        self.meta.world_time = self.clock.now();
        self.meta.rng = self.rng.state();
        self.records.push(rec.clone());
        write_meta(&self.dir, &self.meta)
    }

    /// Appends one record. If `new_state` is given it is written to the object store first and
    /// becomes the live state only once the record is durable (the state store commits with
    /// the ledger record). Metadata follows the record.
    pub fn append(&mut self, draft: RecordDraft, new_state: Option<WorldState>) -> Result<Value> {
        let staged = match new_state {
            Some(s) => {
                s.check_modules(&self.build)?;
                let h = self.objects.put(&s.bytes())?;
                Some((s, h))
            }
            None => None,
        };
        // Task 2.4: commit intent (and fault-injection points) around the durable append.
        crate::recovery::before_append(
            &self.dir,
            self.ledger.next_seq(),
            staged.as_ref().map(|(_, h)| h.as_str()),
            &self.meta,
            self.rng.state(),
        )?;
        let rec = self.ledger.append(draft)?;
        crate::recovery::after_append(rec["seq"].as_u64().unwrap_or(0));
        if let Some((s, h)) = staged {
            self.state = s;
            self.meta.state_hash = h;
        }
        self.sync_meta_from_record(&rec)?;
        Ok(rec)
    }

    /// Marks every record this handle appends from now on with `"language_guard": false`
    /// (the operator diagnostic that switches off the sandbox's in-interpreter layer).
    pub fn mark_language_guard_off(&mut self) {
        self.ledger.mark_language_guard_off();
    }

    /// Starts a turn: `idle` to `in_turn`; returns the turn number records will carry.
    pub fn begin_turn(&mut self) -> Result<u64> {
        if self.meta.status != "idle" {
            return Err(KernelError::Conflict(format!(
                "instance is {}, not idle",
                self.meta.status
            )));
        }
        let n = self.meta.turn + 1;
        self.meta.status = "in_turn".into();
        write_meta(&self.dir, &self.meta)?;
        self.current_turn = Some(n);
        Ok(n)
    }

    /// Number of the open turn, or `None` between turns.
    pub fn current_turn(&self) -> Option<u64> {
        self.current_turn
    }

    /// Ends the current turn: advance by `tick_per_turn`, append the `turn` record, back to idle.
    pub fn end_turn(
        &mut self,
        status: &str,
        code: Option<Code>,
        calls: u64,
        returned: &Value,
        resources: Value,
    ) -> Result<Value> {
        let n = self
            .current_turn
            .ok_or_else(|| KernelError::State("no turn in progress".into()))?;
        self.clock.advance_turn()?;
        let draft = RecordDraft::new(Kind::Turn, n, self.clock.now())
            .with("status", json!(status))
            .with("code", json!(code.map(|c| c.as_str())))
            .with("calls", json!(calls))
            .with("returned_hash", json!(sha256_value(returned)))
            .with("resources", resources);
        let rec = self.ledger.append(draft)?;
        self.meta.turn = n;
        self.meta.status = "idle".into();
        self.current_turn = None;
        self.sync_meta_from_record(&rec)?;
        Ok(rec)
    }

    /// Ends the open turn as refused before anything executed (the turn protocol): appends
    /// `draft`, which must be the `turn` record of the open turn, without advancing world time,
    /// and returns to `idle`. Anything else is [`KernelError::Refused`] and nothing is written.
    pub fn end_turn_refused(&mut self, draft: RecordDraft) -> Result<Value> {
        let n = self
            .current_turn
            .ok_or_else(|| KernelError::State("no turn in progress".into()))?;
        if draft.kind != Kind::Turn || draft.turn != n {
            return Err(KernelError::Refused(format!(
                "a refused turn {n} is closed only by its own turn record"
            )));
        }
        let rec = self.ledger.append(draft)?;
        self.meta.turn = n;
        self.meta.status = "idle".into();
        self.current_turn = None;
        self.sync_meta_from_record(&rec)?;
        Ok(rec)
    }

    /// A kernel-direct turn over the kernel-owned `world.*` facilities, without a sandbox.
    /// Each call is validated, its precondition evaluated, executed, ticked and recorded
    /// durably before `on_result` sees its result; the turn ends with a `turn` record.
    ///
    /// A probe is a real turn, not a read-only inspection: it advances the turn counter and
    /// `world_time` (by `tick_per_primitive` per executed call and `tick_per_turn` at the end),
    /// every `world.random` call draws from the Instance's stream, and its records appear in the
    /// model's ledger tail. It returns, and stores as the last result envelope, the standard
    /// envelope shape of model-surface.md section 4 (`cpu_ms` is 0: no script ran).
    pub fn probe(
        &mut self,
        calls: &[ProbeCall],
        mut on_result: impl FnMut(&Value),
    ) -> Result<Value> {
        let mut resolved = Vec::with_capacity(calls.len());
        for c in calls {
            let Some(facility) = Facility::parse(&c.primitive) else {
                return Err(KernelError::Refused(format!(
                    "{} is not a kernel-owned world facility; probe turns call only {}",
                    c.primitive,
                    KERNEL_FACILITIES.join(", ")
                )));
            };
            let Some(info) = self.build.primitives.get(&c.primitive) else {
                return Err(KernelError::Refused(format!(
                    "{} is not enumerated by Build {}",
                    c.primitive, self.build.hash
                )));
            };
            if c.primitive == "world.history"
                && info
                    .preconditions
                    .iter()
                    .map(|(e, _)| e.as_str())
                    .ne([HISTORY_PRECONDITION])
            {
                return Err(KernelError::Refused(
                    "world.history declares preconditions this probe cannot evaluate".into(),
                ));
            }
            if c.primitive != "world.history" && !info.preconditions.is_empty() {
                return Err(KernelError::Refused(format!(
                    "{} declares preconditions this probe cannot evaluate",
                    c.primitive
                )));
            }
            resolved.push((facility, info.clone()));
        }
        let limit_of = |k: &str| {
            self.build.limits.get(k).copied().ok_or_else(|| {
                KernelError::Refused(format!("Build {} has no limit {k}", self.build.hash))
            })
        };
        let limit = limit_of("primitive_calls")?;
        let adapter_limit = limit_of("adapter_calls")?;
        let turn_started = Instant::now();
        let turn = self.begin_turn()?;
        let mut made = 0u64;
        let mut returned = Vec::new();
        let mut trace = Vec::new();
        let mut failure: Option<Failure> = None;
        let mut mutated = false;
        for (c, (facility, info)) in calls.iter().zip(resolved) {
            let started = Instant::now();
            made += 1;
            let mut draft = RecordDraft::new(Kind::Primitive, turn, self.clock.now());
            draft.primitive = Some(c.primitive.clone());
            draft.params_hash = Some(params_hash(&c.params));
            let fail = |draft: &mut RecordDraft, code: Code| {
                draft.outcome = Some(Outcome::error(code, &info.returns, &info.side_effect));
            };
            let mut result = Value::Null;
            if made > limit {
                fail(&mut draft, Code::Limit);
                draft.extra.insert("limit".into(), json!("primitive_calls"));
            } else if !self.params_valid(&c.primitive, &c.params) {
                fail(&mut draft, Code::Invalid);
            } else {
                let mut passed = true;
                if facility == Facility::History {
                    let n = c.params["n"].as_i64().unwrap_or(-1);
                    passed = (0..=500).contains(&n);
                    let expr = info
                        .preconditions
                        .first()
                        .map_or(HISTORY_PRECONDITION, |p| &p.0);
                    draft.preconditions.push(PreconditionEval {
                        index: 1,
                        expr_hash: format!("sha256:{}", sha256_hex(expr.as_bytes())),
                        passed,
                    });
                }
                if !passed {
                    fail(&mut draft, Code::Invalid);
                } else {
                    result = match facility {
                        Facility::Now => json!(self.clock.now()),
                        Facility::Version => json!(self.build.version),
                        Facility::Seed => json!(self.meta.seed),
                        // The same records the sandboxed path returns (manual records Limits and
                        // LedgerRecord), so a probe and a script observe one world.
                        Facility::Limits => {
                            let budget = crate::limits::Budget {
                                primitive_calls: made,
                                adapter_calls: 0,
                            };
                            budget.limits_record(
                                &crate::limits::Limits::of(&self.build)?,
                                self.build.clock.tick_per_primitive,
                                self.build.clock.tick_per_turn,
                            )
                        }
                        Facility::Random => json!(self.rng.next_u32()),
                        Facility::History => Value::Array(
                            self.history(c.params["n"].as_u64().unwrap_or(0) as usize)
                                .iter()
                                .map(crate::contract_check::ledger_record_view)
                                .collect(),
                        ),
                    };
                    self.clock.advance_primitive()?;
                    draft.world_time = self.clock.now();
                    draft.outcome = Some(Outcome::ok(&info.returns, &info.side_effect));
                    if info.side_effect != "read_only" {
                        mutated = true;
                    }
                }
            }
            draft.duration_us = started.elapsed().as_micros() as u64;
            let ok = draft.outcome.as_ref().map(|o| o.ok).unwrap_or(false);
            let code = draft.outcome.as_ref().and_then(|o| o.code);
            let over_limit = draft.extra.contains_key("limit");
            let failed_precondition = draft.preconditions.iter().any(|p| !p.passed);
            let rec = self.append(draft, None)?;
            let item = json!({
                "primitive": c.primitive,
                "status": if ok { "ok" } else { "error" },
                "code": code.map(|c| c.as_str()),
                "world_time": rec["world_time"],
                "returned": result,
            });
            on_result(&item);
            trace.push(trace_item(
                &c.primitive,
                code,
                rec["world_time"].as_u64().unwrap_or(0),
                true,
            ));
            returned.push(item["returned"].clone());
            if !ok {
                let code = code.unwrap_or(Code::Invalid);
                failure = Some(if over_limit {
                    Failure::limit(
                        &c.primitive,
                        "primitive_calls",
                        format!("the turn already made its {limit} primitive calls"),
                    )
                } else if failed_precondition {
                    Failure::precondition(
                        code,
                        &c.primitive,
                        1,
                        format!("precondition 1 `{HISTORY_PRECONDITION}` was false"),
                    )
                } else {
                    Failure::new(
                        code,
                        &c.primitive,
                        format!("{} parameters do not match its contract", c.primitive),
                    )
                });
                break;
            }
        }
        let status = crate::envelope::turn_status(failure.is_some(), mutated);
        let returned = Value::Array(returned);
        let resources = Resources {
            cpu_ms: 0,
            wall_ms: turn_started.elapsed().as_millis() as u64,
            primitive_calls: made,
            adapter_calls: 0,
            remaining_primitive_calls: limit.saturating_sub(made),
            remaining_adapter_calls: adapter_limit,
        };
        let code = failure.as_ref().map(|f| f.code);
        let rec = self.end_turn(status, code, made, &returned, resources.ledger_json())?;
        let world = json!({"version": self.build.version, "build": self.build.hash, "time": rec["world_time"], "turn": rec["turn"]});
        let env = turn_envelope(status, returned, failure.as_ref(), trace, world, &resources);
        write_atomic(
            &self.dir.join(crate::executor::LAST_ENVELOPE),
            format!("{}\n", canonical_json(&env)).as_bytes(),
        )?;
        Ok(env)
    }

    fn params_valid(&self, primitive: &str, params: &Value) -> bool {
        let Some(obj) = params.as_object() else {
            return false;
        };
        match primitive {
            "world.history" => {
                obj.len() == 1
                    && obj
                        .get("n")
                        .map(|n| n.is_i64() || n.is_u64())
                        .unwrap_or(false)
            }
            _ => obj.is_empty(),
        }
    }

    /// `cwr instance snapshot`: capture state, seq, chain, world_time, turn, RNG, K, seed and
    /// Build hash into a content-addressed file. Returns `sha256:<hex>`.
    pub fn snapshot(&mut self) -> Result<String> {
        if self.meta.status != "idle" {
            return Err(KernelError::State(format!(
                "snapshot needs an idle instance, not {}",
                self.meta.status
            )));
        }
        self.to_snapshot().write(&self.dir.join("snapshots"))
    }

    /// A snapshot of the Instance at its last durable record.
    pub fn to_snapshot(&self) -> Snapshot {
        Snapshot {
            instance: self.meta.id.clone(),
            build: self.meta.build.clone(),
            seq: self.meta.seq,
            chain: self.meta.chain.clone(),
            world_time: self.clock.now(),
            turn: self.meta.turn,
            rng: self.rng.state(),
            tail_size: self.meta.tail_size,
            seed: self.meta.seed,
            state: self.state.clone(),
        }
    }

    /// `cwr instance restore`: verify the snapshot and its Build, cut the live ledger back to
    /// the snapshot's record (retaining everything after it under `retained/`), restore state,
    /// clock, turn, RNG and K, and append a `recovery` record. Accepts a ledger whose final
    /// record is truncated: this is the explicit recovery path.
    pub fn restore(home: &Path, id: &str, reference: &str) -> Result<(Instance, Value)> {
        let dir = Self::checked_dir(home, id)?;
        if !dir.is_dir() {
            return Err(KernelError::Refused(format!("no instance {id}")));
        }
        let lock = lock_dir(&dir)?;
        let mut meta = read_meta(&dir)?;
        if meta.status == "destroyed" {
            return Err(KernelError::State(format!("instance {id} is destroyed")));
        }
        let path = Snapshot::resolve(reference, &dir.join("snapshots"));
        let (snap, snap_hash) = Snapshot::load(&path)?;
        if snap.instance != meta.id {
            return Err(KernelError::Refused(format!(
                "snapshot {snap_hash} belongs to instance {}, not {id}",
                snap.instance
            )));
        }
        let build_dir = if snap.build == meta.build {
            meta.build_dir.clone()
        } else {
            meta.build_dir
                .parent()
                .map(|p| p.join(snap.build.trim_start_matches("sha256:")))
                .unwrap_or_default()
        };
        let build = Build::load(&build_dir)?;
        if build.hash != snap.build {
            return Err(KernelError::HashMismatch(format!(
                "snapshot names Build {}, directory holds {}",
                snap.build, build.hash
            )));
        }
        if build.experimental.is_some() && build.experimental != meta.experimental {
            build.check_experimental(false)?;
        }
        snap.state.check_modules(&build)?;

        // Find the snapshot's record in the live ledger or a retained lineage.
        let ledger_path = dir.join("ledger.jsonl");
        let live = Ledger::scan_lenient(&ledger_path)?;
        let matches = |s: &crate::ledger::LedgerScan| {
            s.records
                .get(snap.seq as usize)
                .and_then(|r| r["chain"].as_str())
                == Some(snap.chain.as_str())
        };
        let retained_dir = dir.join("retained");
        let prefix: Vec<u8> = if matches(&live) {
            live.prefix_bytes(snap.seq).unwrap_or_default().to_vec()
        } else {
            let mut found = None;
            if retained_dir.is_dir() {
                let mut files: Vec<_> = fs::read_dir(&retained_dir)
                    .map_err(|e| io_err("read dir", &retained_dir, e))?
                    .filter_map(|e| e.ok().map(|e| e.path()))
                    .collect();
                files.sort();
                for f in files {
                    if let Ok(s) = Ledger::scan_lenient(&f) {
                        if matches(&s) {
                            found = s.prefix_bytes(snap.seq).map(<[u8]>::to_vec);
                            break;
                        }
                    }
                }
            }
            found.ok_or_else(|| {
                KernelError::Refused(format!(
                    "snapshot {snap_hash} (seq {}) is not in this instance's lineage",
                    snap.seq
                ))
            })?
        };
        let mut retained = None;
        if live.raw != prefix {
            fs::create_dir_all(&retained_dir).map_err(|e| io_err("create", &retained_dir, e))?;
            let n = fs::read_dir(&retained_dir).map(|d| d.count()).unwrap_or(0);
            let name = format!("ledger.{n:04}.jsonl");
            write_atomic(&retained_dir.join(&name), &live.raw)?;
            retained = Some(format!("retained/{name}"));
        }
        let abandoned = live.records.len() as u64 > snap.seq + 1
            || live.truncated_tail.is_some()
            || !matches(&live);
        write_atomic(&ledger_path, &prefix)?;

        let interrupted = (meta.status == "in_turn").then_some(meta.turn + 1);
        let objects = ObjectStore::new(dir.join("objects"));
        let state_hash = objects.put(&snap.state.bytes())?;
        meta.build = build.hash.clone();
        meta.build_dir = build.dir.clone();
        meta.world_name = build.name.clone();
        meta.world_version = build.version.clone();
        meta.experimental = build.experimental.clone();
        meta.seed_visible = build.seed_visible;
        meta.seed = snap.seed;
        meta.tail_size = snap.tail_size;
        meta.turn = snap.turn;
        meta.world_time = snap.world_time;
        meta.rng = snap.rng;
        meta.state_hash = state_hash;
        meta.seq = snap.seq;
        meta.chain = snap.chain.clone();
        meta.status = "recovering".into();
        write_meta(&dir, &meta)?;

        let (ledger, scan) =
            Ledger::open(&ledger_path, id, &build.hash, build.experimental.as_deref())?;
        let clock = WorldClock::at(build.clock, snap.world_time);
        let mut inst = Instance {
            dir: dir.clone(),
            meta,
            build,
            state: snap.state.clone(),
            clock,
            rng: Pcg32::from_state(snap.rng),
            ledger,
            records: scan.records,
            objects,
            current_turn: None,
            _lock: lock,
        };
        let draft = RecordDraft::new(Kind::Recovery, snap.turn, snap.world_time)
            .with("snapshot", json!(snap_hash))
            .with("resumed_from", json!(snap.seq))
            .with("interrupted_turn", json!(interrupted))
            .with(
                "abandoned_from",
                json!(if abandoned { Some(snap.seq + 1) } else { None }),
            )
            .with("retained", json!(retained))
            .with("tail_size", snap.tail_size.to_json());
        inst.meta.status = "idle".into();
        let rec = inst.append(draft, None)?;
        Ok((inst, rec))
    }

    /// Instance-directory entries `destroy` releases: the state store, snapshots, the adapter
    /// gateway's idempotency store, recorded turn inputs, the recovery and commit files, stored
    /// notices and the last result envelope. The ledger, the metadata, retained ledger lineages,
    /// a defect seal and the proposal outbox (which only the human owner reads) are kept.
    pub const RELEASED_ON_DESTROY: [&'static str; 9] = [
        "objects",
        "snapshots",
        crate::gateway::GATEWAY_DIR,
        crate::replay::SCRIPTS_DIR,
        crate::notice::NOTICE_DIR,
        crate::recovery::INFLIGHT_FILE,
        crate::recovery::COMMIT_FILE,
        crate::recovery::JOURNAL_FILE,
        crate::executor::LAST_ENVELOPE,
    ];

    /// `cwr instance destroy`: seal and retain the ledger, release state and snapshots.
    /// Allowed only from `idle` (kernel.md section 2); an Instance a crash left mid-turn is
    /// recovered first by the caller.
    pub fn destroy(home: &Path, id: &str) -> Result<InstanceMeta> {
        let dir = Self::checked_dir(home, id)?;
        if !dir.is_dir() {
            return Err(KernelError::Refused(format!("no instance {id}")));
        }
        let _lock = lock_dir(&dir)?;
        let mut meta = read_meta(&dir)?;
        match meta.status.as_str() {
            "destroyed" => {
                return Err(KernelError::State(format!(
                    "instance {id} is already destroyed"
                )))
            }
            "idle" => {}
            other => {
                return Err(KernelError::Conflict(format!(
                    "instance {id} is {other}; destroy needs an idle instance (run `cwr instance recover {id}` after a crash)"
                )))
            }
        }
        for sub in Self::RELEASED_ON_DESTROY {
            let p = dir.join(sub);
            if p.is_dir() {
                fs::remove_dir_all(&p).map_err(|e| io_err("remove", &p, e))?;
            } else if p.exists() {
                fs::remove_file(&p).map_err(|e| io_err("remove", &p, e))?;
            }
        }
        meta.status = "destroyed".into();
        write_meta(&dir, &meta)?;
        for f in ["ledger.jsonl", "instance.json"] {
            let p = dir.join(f);
            let mut perm = fs::metadata(&p)
                .map_err(|e| io_err("stat", &p, e))?
                .permissions();
            perm.set_readonly(true);
            fs::set_permissions(&p, perm).map_err(|e| io_err("seal", &p, e))?;
        }
        sync_dir(&dir)?;
        Ok(meta)
    }

    /// Reads metadata without locking (for `show` on any Instance, including destroyed ones).
    pub fn read_meta(home: &Path, id: &str) -> Result<InstanceMeta> {
        read_meta(&Self::checked_dir(home, id)?)
    }
}
