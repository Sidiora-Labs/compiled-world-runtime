// Copyright Sidiora Labs Inc. CWR® 2026-2027
// SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0

//! `cwr instance upgrade`: the controlled-change transaction (kernel.md sections 2, 9 and 10;
//! clauses 3.6, 5.2, 5.3 and 5.5).
//!
//! [`upgrade`] performs, in order:
//!
//! 1. resolve any earlier interrupted upgrade of the Instance first (see below); stop there when
//!    it was rolled back or when its completed commit bound the Build requested now;
//! 2. open the Instance (lock, Build hash, strict ledger) and require it `idle` and not sealed;
//! 3. load the target Build, which verifies its hash over its contents;
//! 4. refuse a target with a different world name, a version that is not strictly greater
//!    (numerically) than the running one, the Build already bound, or an experimental target,
//!    unless `--allow-experimental` is given and the target is experimental, which lifts only the
//!    version rule;
//! 5. plan the migrations ([`crate::migrate::plan`]): every state-shape change between the two
//!    Builds must name a migration present in the target;
//! 6. take a snapshot, write the upgrade journal `upgrade.json` and enter `upgrading`;
//! 7. apply the migrations in changelog order in the sandbox to a copy of the state and validate
//!    the result against the target's state shapes;
//! 8. store the recompile notice ([`crate::notice`]), then commit: the new state object is
//!    written, the `upgrade` record (stamped with the new Build, carrying old and new hashes and
//!    versions, the migrations applied, the snapshot, the new state hash and the notice hash) is
//!    appended and fsynced, the metadata is rebound to the new Build and the Instance returns to
//!    `idle`.
//!
//! A failure in steps 6 to 8 before the `upgrade` record is durable restores the snapshot from
//! step 6, leaves the old Build bound and the ledger untouched, and is reported with exit code 6;
//! a refusal in steps 3 to 5 changes nothing and is exit code 5. A turn is refused while the
//! Instance is `upgrading`, so no partially migrated state is ever visible.
//!
//! A crash leaves `upgrade.json` behind. The next `upgrade` of that Instance resolves it before
//! anything else: if the journal's `upgrade` record is durable the commit is completed (roll
//! forward); otherwise the snapshot is restored, a truncated record is cut, the staged notice is
//! discarded and the earlier failure is reported with exit code 6. A completed commit ends the
//! command with exit code 0 only when it bound the Build the operator asks for now; for any other
//! target the requested upgrade then runs from the newly bound Build and its exit code is the
//! requested upgrade's own, with the completion reported in [`UpgradeReport::resolved`] or in the
//! error text. Fault points for the drivers:
//! `CWR_FAULT_INJECT=upgrade:after_snapshot|after_migrate|after_notice|after_commit`.

use std::fs::{self, File};
use std::path::{Path, PathBuf};

use serde_json::{json, Value};

use crate::contract_check::SEAL_FILE;
use crate::ledger::{Kind, Ledger, RecordDraft};
use crate::migrate::{self, semver};
use crate::notice::{self, Notice};
use crate::recovery::maybe_fault;
use crate::snapshot::Snapshot;
use crate::state::{Build, Instance, InstanceMeta, ObjectStore};
use crate::{canonical_json, io_err, write_atomic, KernelError};

/// The upgrade journal inside an Instance directory, present only while an upgrade is open.
pub const JOURNAL: &str = "upgrade.json";

/// Operator options of `cwr instance upgrade`.
#[derive(Debug, Clone, Copy, Default)]
pub struct UpgradeOptions {
    /// `--allow-experimental`: accept an experimental target and lift the version rule for it.
    pub allow_experimental: bool,
}

/// What a committed upgrade produced.
#[derive(Debug, Clone)]
pub struct UpgradeReport {
    /// The durable `upgrade` ledger record.
    pub record: Value,
    /// The stored recompile notice.
    pub notice: Notice,
    /// Hash of the pre-upgrade snapshot.
    pub snapshot: String,
    /// An earlier interrupted upgrade whose commit was completed before this one ran.
    pub resolved: Option<Resolution>,
}

/// How an earlier interrupted upgrade was resolved.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Resolution {
    /// The `upgrade` record was durable: the commit was completed.
    Completed {
        /// Seq of the `upgrade` record.
        seq: u64,
        /// Build now bound.
        build: String,
    },
    /// The upgrade had not committed: the pre-upgrade snapshot was restored.
    RolledBack {
        /// Snapshot restored.
        snapshot: String,
        /// What the interrupted upgrade was doing, from its journal.
        failure: String,
    },
}

/// Why an upgrade did not commit. Each variant maps to an operator exit code.
#[derive(Debug)]
pub enum UpgradeError {
    /// The target Build is refused (hash, name, version, experimental, missing migration);
    /// nothing changed. Exit 5.
    Refused(String),
    /// A migration or the commit failed; the Instance was rolled back to its pre-upgrade
    /// snapshot with the old Build bound. Exit 6.
    RolledBack {
        /// The failure.
        detail: String,
        /// Snapshot restored.
        snapshot: String,
    },
    /// An earlier upgrade was interrupted and has just been resolved; the requested upgrade was
    /// not attempted. Exit 6 for a rollback, 0 when the completed earlier upgrade bound exactly
    /// the Build requested now (a completion to any other Build is never reported this way).
    Interrupted(Resolution),
    /// An earlier interrupted upgrade was completed, then the requested upgrade failed. Exit as
    /// the requested upgrade's failure.
    AfterCompletion {
        /// The completed earlier upgrade.
        resolution: Resolution,
        /// The requested upgrade's failure.
        error: Box<UpgradeError>,
    },
    /// Any other kernel failure (Instance not idle, sealed, I/O). Exit as the kernel error.
    Kernel(KernelError),
}

impl UpgradeError {
    /// Operator exit code (kernel.md section 2).
    pub fn exit_code(&self) -> i32 {
        match self {
            UpgradeError::Refused(_) => 5,
            UpgradeError::RolledBack { .. } => 6,
            UpgradeError::Interrupted(Resolution::RolledBack { .. }) => 6,
            UpgradeError::Interrupted(Resolution::Completed { .. }) => 0,
            UpgradeError::AfterCompletion { error, .. } => error.exit_code(),
            UpgradeError::Kernel(e) => e.exit_code(),
        }
    }
}

impl std::fmt::Display for UpgradeError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            UpgradeError::Refused(m) => write!(f, "upgrade refused: {m}"),
            UpgradeError::RolledBack { detail, snapshot } => write!(
                f,
                "upgrade failed and was rolled back to snapshot {snapshot}; the old Build stays bound: {detail}"
            ),
            UpgradeError::Interrupted(Resolution::RolledBack { snapshot, failure }) => write!(
                f,
                "an earlier upgrade was interrupted ({failure}); it was rolled back to snapshot {snapshot} and the old Build stays bound"
            ),
            UpgradeError::Interrupted(Resolution::Completed { seq, build }) => write!(
                f,
                "an earlier interrupted upgrade had committed (upgrade record seq {seq}); its commit was completed and {build} is bound"
            ),
            UpgradeError::AfterCompletion { resolution, error } => write!(
                f,
                "{}; then {error}",
                UpgradeError::Interrupted(resolution.clone())
            ),
            UpgradeError::Kernel(e) => write!(f, "{e}"),
        }
    }
}

impl std::error::Error for UpgradeError {}

impl From<KernelError> for UpgradeError {
    fn from(e: KernelError) -> Self {
        UpgradeError::Kernel(e)
    }
}

type Result<T> = std::result::Result<T, UpgradeError>;

fn write_meta(dir: &Path, meta: &InstanceMeta) -> crate::Result<()> {
    let mut s = serde_json::to_string_pretty(&meta.to_json())
        .map_err(|e| KernelError::Io(format!("serialise instance metadata: {e}")))?;
    s.push('\n');
    write_atomic(&dir.join("instance.json"), s.as_bytes())
}

fn lock(dir: &Path) -> crate::Result<File> {
    let p = dir.join("lock");
    let f = fs::OpenOptions::new()
        .create(true)
        .truncate(false)
        .write(true)
        .open(&p)
        .map_err(|e| io_err("open lock", &p, e))?;
    f.try_lock().map_err(|_| {
        KernelError::State(format!("instance {} is busy (lock held)", dir.display()))
    })?;
    Ok(f)
}

fn write_journal(dir: &Path, phase: &str, j: &Value) -> crate::Result<()> {
    let mut j = j.clone();
    j["phase"] = json!(phase);
    write_atomic(
        &dir.join(JOURNAL),
        format!("{}\n", canonical_json(&j)).as_bytes(),
    )
}

fn remove_journal(dir: &Path) -> crate::Result<()> {
    let p = dir.join(JOURNAL);
    match fs::remove_file(&p) {
        Ok(()) => crate::sync_dir(dir),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(e) => Err(io_err("remove", &p, e)),
    }
}

/// Whether Instance `id` has an open (interrupted) upgrade.
pub fn interrupted(home: &Path, id: &str) -> crate::Result<bool> {
    let dir = Instance::dir_of(home, id);
    Ok(dir.join(JOURNAL).is_file() || Instance::read_meta(home, id)?.status == "upgrading")
}

/// Restores `meta` to the snapshot: state, seq, chain, clock, turn, RNG and the old Build.
fn rollback_meta(
    dir: &Path,
    meta: &mut InstanceMeta,
    snap: &Snapshot,
    old: &Build,
) -> crate::Result<()> {
    let objects = ObjectStore::new(dir.join("objects"));
    meta.state_hash = objects.put(&snap.state.bytes())?;
    meta.build = old.hash.clone();
    meta.build_dir = old.dir.clone();
    meta.world_name = old.name.clone();
    meta.world_version = old.version.clone();
    meta.experimental = old.experimental.clone();
    meta.seed_visible = old.seed_visible;
    meta.seq = snap.seq;
    meta.chain = snap.chain.clone();
    meta.world_time = snap.world_time;
    meta.turn = snap.turn;
    meta.rng = snap.rng;
    meta.status = "idle".into();
    write_meta(dir, meta)
}

/// Cuts the ledger back to the snapshot's record when an uncommitted upgrade left anything after
/// it (a complete or truncated `upgrade` record at `seq`).
fn cut_ledger(dir: &Path, snap: &Snapshot, seq: u64) -> crate::Result<()> {
    let path = dir.join("ledger.jsonl");
    let scan = Ledger::scan_lenient(&path)?;
    if scan.records.len() as u64 > seq || scan.truncated_tail.is_some() {
        let prefix = scan.prefix_bytes(snap.seq).ok_or_else(|| {
            KernelError::Corrupt(format!("{}: no record {}", path.display(), snap.seq))
        })?;
        write_atomic(&path, prefix)?;
    }
    Ok(())
}

/// Resolves an interrupted upgrade of Instance `id` (see the module documentation). Returns None
/// when there is nothing to resolve.
pub fn resolve_interrupted(home: &Path, id: &str) -> Result<Option<Resolution>> {
    let dir = Instance::dir_of(home, id);
    if !dir.is_dir() {
        return Err(KernelError::Refused(format!("no instance {id}")).into());
    }
    let _lock = lock(&dir)?;
    let mut meta = Instance::read_meta(home, id)?;
    let jpath = dir.join(JOURNAL);
    let journal: Option<Value> = fs::read(&jpath)
        .ok()
        .and_then(|b| serde_json::from_slice(&b).ok());
    let Some(j) = journal else {
        if meta.status == "upgrading" {
            return Err(KernelError::Corrupt(format!(
                "instance {id} is upgrading but its upgrade journal is missing or unreadable"
            ))
            .into());
        }
        return Ok(None);
    };
    let s = |k: &str| j[k].as_str().unwrap_or_default().to_string();
    let seq = j["seq"]
        .as_u64()
        .ok_or_else(|| KernelError::Corrupt(format!("{}: seq missing", jpath.display())))?;
    let scan = Ledger::scan_lenient(&dir.join("ledger.jsonl"))?;
    let committed = scan.records.get(seq as usize).filter(|r| {
        r["kind"] == "upgrade" && r["new_build"].as_str() == Some(s("new_build").as_str())
    });
    if let Some(rec) = committed.cloned() {
        let new = Build::load(Path::new(&s("new_build_dir"))).map_err(UpgradeError::Kernel)?;
        meta.build = new.hash.clone();
        meta.build_dir = new.dir.clone();
        meta.world_name = new.name.clone();
        meta.world_version = new.version.clone();
        meta.experimental = new.experimental.clone();
        meta.seed_visible = new.seed_visible;
        meta.seq = seq;
        meta.chain = rec["chain"].as_str().unwrap_or_default().to_string();
        meta.state_hash = rec["state_hash"].as_str().unwrap_or_default().to_string();
        meta.status = "idle".into();
        write_meta(&dir, &meta)?;
        remove_journal(&dir)?;
        return Ok(Some(Resolution::Completed {
            seq,
            build: new.hash,
        }));
    }
    let snap_ref = s("snapshot");
    let (snap, snap_hash) = Snapshot::load(&Snapshot::resolve(&snap_ref, &dir.join("snapshots")))?;
    if snap.instance != id || snap.seq + 1 != seq {
        return Err(KernelError::Corrupt(format!(
            "upgrade journal snapshot {snap_hash} does not precede the upgrade at seq {seq}"
        ))
        .into());
    }
    cut_ledger(&dir, &snap, seq)?;
    let old = Build::load(Path::new(&s("old_build_dir")))?;
    rollback_meta(&dir, &mut meta, &snap, &old)?;
    notice::discard(&dir, seq)?;
    remove_journal(&dir)?;
    Ok(Some(Resolution::RolledBack {
        snapshot: snap_hash,
        failure: format!("interrupted in phase {}", s("phase")),
    }))
}

/// Upgrades Instance `id` under `home` to the Build at `target_dir` (see the module
/// documentation for the steps and the failure semantics).
pub fn upgrade(
    home: &Path,
    id: &str,
    target_dir: &Path,
    opts: UpgradeOptions,
) -> Result<UpgradeReport> {
    let resolved = match resolve_interrupted(home, id)? {
        None => None,
        Some(r @ Resolution::RolledBack { .. }) => return Err(UpgradeError::Interrupted(r)),
        Some(Resolution::Completed { seq, build }) => {
            let requested = Build::load(target_dir).is_ok_and(|t| t.hash == build);
            let r = Resolution::Completed { seq, build };
            if requested {
                return Err(UpgradeError::Interrupted(r));
            }
            Some(r)
        }
    };
    match upgrade_idle(home, id, target_dir, opts) {
        Ok(mut rep) => {
            rep.resolved = resolved;
            Ok(rep)
        }
        Err(error) => Err(match resolved {
            Some(resolution) => UpgradeError::AfterCompletion {
                resolution,
                error: Box::new(error),
            },
            None => error,
        }),
    }
}

/// Steps 2 to 8 of [`upgrade`], on an Instance with no interrupted upgrade left.
fn upgrade_idle(
    home: &Path,
    id: &str,
    target_dir: &Path,
    opts: UpgradeOptions,
) -> Result<UpgradeReport> {
    let mut inst = Instance::open(home, id)?;
    if inst.meta.status != "idle" {
        return Err(KernelError::State(format!(
            "upgrade needs an idle instance, not {}",
            inst.meta.status
        ))
        .into());
    }
    if inst.dir.join(SEAL_FILE).exists() {
        return Err(KernelError::State(format!(
            "instance {id} is sealed after an INVARIANT_VIOLATION; it cannot be upgraded"
        ))
        .into());
    }
    let target = Build::load(target_dir)
        .map_err(|e| UpgradeError::Refused(format!("target Build failed verification: {e}")))?;
    check_target(&inst.build, &target, opts)?;
    let plan =
        migrate::plan(&inst.build, &target).map_err(|e| UpgradeError::Refused(e.to_string()))?;

    // Step 6: snapshot, journal, `upgrading`.
    let snapshot = inst.snapshot()?;
    let seq = inst.records().len() as u64;
    let dir = inst.dir.clone();
    let journal = json!({
        "format": "cwr.upgrade-journal",
        "instance": id,
        "seq": seq,
        "snapshot": snapshot,
        "old_build": inst.build.hash,
        "old_build_dir": inst.build.dir.to_string_lossy(),
        "new_build": target.hash,
        "new_build_dir": target.dir.to_string_lossy(),
    });
    write_journal(&dir, "migrating", &journal)?;
    inst.meta.status = "upgrading".into();
    write_meta(&dir, &inst.meta)?;
    maybe_fault("upgrade", "after_snapshot");

    let result = (|| -> std::result::Result<(Value, Notice), String> {
        let migrated = migrate::apply(&target, &plan, &inst.state)?;
        maybe_fault("upgrade", "after_migrate");
        let n = notice::derive(&inst.build, &target, inst.records(), &plan, seq)
            .map_err(|e| e.to_string())?;
        notice::store(&dir, &n).map_err(|e| e.to_string())?;
        write_journal(&dir, "committing", &journal).map_err(|e| e.to_string())?;
        maybe_fault("upgrade", "after_notice");
        let state_hash = ObjectStore::new(dir.join("objects"))
            .put(&migrated.bytes())
            .map_err(|e| e.to_string())?;
        let (mut ledger, _) = Ledger::open(
            &dir.join("ledger.jsonl"),
            id,
            &target.hash,
            target.experimental.as_deref(),
        )
        .map_err(|e| e.to_string())?;
        if ledger.next_seq() != seq {
            return Err(format!(
                "ledger moved during the upgrade (next seq {} != {seq})",
                ledger.next_seq()
            ));
        }
        let draft = RecordDraft::new(Kind::Upgrade, inst.meta.turn, inst.clock.now())
            .with("old_build", json!(inst.build.hash))
            .with("new_build", json!(target.hash))
            .with("old_version", json!(inst.build.version))
            .with("new_version", json!(target.version))
            .with("migrations", json!(n.migrations))
            .with("snapshot", json!(snapshot))
            .with("state_hash", json!(state_hash))
            .with("notice_hash", json!(n.hash()));
        let rec = ledger.append(draft).map_err(|e| e.to_string())?;
        Ok((rec, n))
    })();

    let (record, n) = match result {
        Ok(v) => v,
        Err(detail) => {
            let (snap, _) = Snapshot::load(&Snapshot::resolve(&snapshot, &dir.join("snapshots")))?;
            cut_ledger(&dir, &snap, seq)?;
            let old = inst.build.clone();
            rollback_meta(&dir, &mut inst.meta, &snap, &old)?;
            notice::discard(&dir, seq)?;
            remove_journal(&dir)?;
            return Err(UpgradeError::RolledBack { detail, snapshot });
        }
    };
    maybe_fault("upgrade", "after_commit");

    // Commit point passed: rebind the metadata and return to `idle`.
    let meta = &mut inst.meta;
    meta.build = target.hash.clone();
    meta.build_dir = target.dir.clone();
    meta.world_name = target.name.clone();
    meta.world_version = target.version.clone();
    meta.experimental = target.experimental.clone();
    meta.seed_visible = target.seed_visible;
    meta.seq = seq;
    meta.chain = record["chain"].as_str().unwrap_or_default().to_string();
    meta.state_hash = record["state_hash"]
        .as_str()
        .unwrap_or_default()
        .to_string();
    meta.status = "idle".into();
    write_meta(&dir, meta)?;
    remove_journal(&dir)?;
    Ok(UpgradeReport {
        record,
        notice: n,
        snapshot,
        resolved: None,
    })
}

/// Step 4: world name, strictly greater version, not the bound Build, experimental gate.
pub fn check_target(current: &Build, target: &Build, opts: UpgradeOptions) -> Result<()> {
    if target.hash == current.hash {
        return Err(UpgradeError::Refused(format!(
            "the Instance is already bound to {}",
            target.hash
        )));
    }
    if target.name != current.name {
        return Err(UpgradeError::Refused(format!(
            "target world name {:?} differs from the running world {:?}",
            target.name, current.name
        )));
    }
    if target.experimental.is_some() && !opts.allow_experimental {
        return Err(UpgradeError::Refused(format!(
            "target Build {} is experimental ({}); pass --allow-experimental",
            target.hash,
            target.experimental.as_deref().unwrap_or_default()
        )));
    }
    let lifted = opts.allow_experimental && target.experimental.is_some();
    let (Some(old), Some(new)) = (semver(&current.version), semver(&target.version)) else {
        return Err(UpgradeError::Refused(format!(
            "versions {:?} and {:?} must both be MAJOR.MINOR.PATCH",
            current.version, target.version
        )));
    };
    if new <= old && !lifted {
        return Err(UpgradeError::Refused(format!(
            "target version {} is not greater than the running version {}",
            target.version, current.version
        )));
    }
    Ok(())
}

/// The directory of a Build reference for upgrade: an existing directory, or a hash resolved in
/// `roots`; a hash reference must name a directory whose contents hash to it.
pub fn resolve_target(reference: &str, roots: &[PathBuf]) -> Result<PathBuf> {
    Build::resolve(reference, roots).map_err(|e| UpgradeError::Refused(e.to_string()))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::clock::TickConfig;
    use std::collections::BTreeMap;

    fn build(name: &str, version: &str, hash: &str, experimental: Option<&str>) -> Build {
        Build {
            dir: PathBuf::from(format!("/nonexistent/{hash}")),
            hash: format!("sha256:{hash}"),
            name: name.into(),
            version: version.into(),
            experimental: experimental.map(str::to_string),
            modules: vec![],
            limits: BTreeMap::new(),
            clock: TickConfig::new(1, 10).unwrap(),
            seed_visible: true,
            primitives: BTreeMap::new(),
        }
    }

    fn refused(r: Result<()>) -> String {
        match r {
            Err(UpgradeError::Refused(m)) => m,
            other => panic!("expected a refusal, got {other:?}"),
        }
    }

    #[test]
    fn version_name_and_experimental_rules() {
        let cur = build("refworld", "0.2.0", "a", None);
        let plain = UpgradeOptions::default();
        let allow = UpgradeOptions {
            allow_experimental: true,
        };
        assert!(check_target(&cur, &build("refworld", "0.10.0", "b", None), plain).is_ok());
        assert!(refused(check_target(
            &cur,
            &build("refworld", "0.2.0", "b", None),
            plain
        ))
        .contains("not greater"));
        assert!(refused(check_target(
            &cur,
            &build("refworld", "0.1.9", "b", None),
            allow
        ))
        .contains("not greater"));
        assert!(refused(check_target(
            &cur,
            &build("other", "0.3.0", "b", None),
            allow
        ))
        .contains("world name"));
        assert!(refused(check_target(
            &cur,
            &build("refworld", "0.3.0", "a", None),
            plain
        ))
        .contains("already bound"));
        let x = build("refworld", "0.2.0", "x", Some("x-version-only"));
        assert!(refused(check_target(&cur, &x, plain)).contains("experimental"));
        assert!(check_target(&cur, &x, allow).is_ok());
        assert!(refused(check_target(
            &cur,
            &build("refworld", "0.3", "b", None),
            plain
        ))
        .contains("MAJOR.MINOR.PATCH"));
    }

    #[test]
    fn exit_codes_follow_kernel_md() {
        assert_eq!(UpgradeError::Refused(String::new()).exit_code(), 5);
        let rb = UpgradeError::RolledBack {
            detail: String::new(),
            snapshot: String::new(),
        };
        assert_eq!(rb.exit_code(), 6);
        let earlier = Resolution::RolledBack {
            snapshot: String::new(),
            failure: String::new(),
        };
        assert_eq!(UpgradeError::Interrupted(earlier).exit_code(), 6);
        let done = Resolution::Completed {
            seq: 3,
            build: String::new(),
        };
        assert_eq!(UpgradeError::Interrupted(done.clone()).exit_code(), 0);
        let then_refused = UpgradeError::AfterCompletion {
            resolution: done,
            error: Box::new(UpgradeError::Refused("version 0.3.0 is not greater".into())),
        };
        assert_eq!(then_refused.exit_code(), 5);
        assert!(then_refused.to_string().contains("had committed"));
        assert!(then_refused.to_string().ends_with("not greater"));
    }
}
