// Copyright Sidiora Labs Inc. CWR® 2026-2027
// SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0

//! Crash recovery (kernel.md section 9, design.body.md section 8; clauses 6.6 and 8.3; task 2.4,
//! case CWR-010).
//!
//! A kernel crash is a host event: the process dies (SIGKILL) at an arbitrary point of a turn. On
//! the next open the kernel finds the Instance left `in_turn` (or with metadata behind the ledger),
//! recovers it from the last snapshot plus the ledger prefix and closes the interrupted turn:
//!
//! * the durable ledger prefix is authoritative; a truncated final record was never durable and is
//!   cut (the whole file is kept under `retained/` by [`Instance::restore`]);
//! * the recovery base is the kernel's own last consistent capture of the Instance (the metadata
//!   written after every durable record: state hash, seq, chain, world_time, turn and RNG state),
//!   or, when that does not match the ledger, the latest snapshot file in the Instance's lineage;
//! * records after the base are rolled forward. A record whose primitive changed the world state or
//!   advanced the RNG is preceded on disk by a commit intent ([`before_append`], written by
//!   [`Instance::append`] before the ledger append) naming the new state object and RNG state, so a
//!   primitive whose record is durable is committed and one whose record is not is absent; never half;
//! * the rolled-forward capture is written as a content-addressed snapshot and restored through
//!   [`Instance::restore`], which appends the `recovery` record (snapshot restored, seq resumed from,
//!   interrupted turn);
//! * an external call that was in flight (journalled by [`Journaled`] before the gateway sent
//!   anything) and has no ledger record is given a reconstructed record with code `UNAVAILABLE`; it
//!   is never re-executed, and an external primitive whose record exists is complete;
//! * idempotency keys a crash left pending are completed with `UNAVAILABLE` as their stored outcome,
//!   so the next identical call is answered from the store instead of being sent again;
//! * the interrupted turn is closed by a `turn` record with code `UNAVAILABLE` and status `partial`
//!   if a mutating or external call of the turn succeeded, else `error`; the rest of the script is
//!   not resumed. The result envelope of that turn is written for the model surface.
//!
//! Recovery is restartable: a crash during recovery leaves files from which the next attempt
//! reaches the same end state (its progress is journalled in `recovery.json`).
//!
//! Fault injection for the verification driver (an operator test seam, never reachable from a
//! script: the sandbox sees no host environment): `CWR_FAULT_INJECT=<point>:<selector>` makes the
//! kernel process SIGKILL itself at one of
//!
//! | point | selector | where |
//! |---|---|---|
//! | `before_execute` | adapter name | an external primitive passed steps 1 and 2; nothing journalled or sent |
//! | `after_execute` | adapter name | the gateway returned (the service was contacted); no ledger record yet |
//! | `before_append` | ledger seq | the new state object and commit intent are written; the record is not |
//! | `after_append` | ledger seq | the record is durable; the Instance metadata is not yet updated |

use std::fs::{self, File};
use std::path::{Path, PathBuf};

use cwr_contract::Code;
use serde_json::{json, Map, Value};

use crate::contract_check::{Contract, ContractTable, Gateway, GatewayOutcome};
use crate::envelope::{trace_item, turn_envelope, Failure, Resources};

use crate::gateway::cassette::request_hash;
use crate::gateway::idempotency::{KeyStore, Slot, Stored};
use crate::gateway::{ordinals_from_ledger, GATEWAY_DIR};
use crate::ledger::{Kind, Ledger, LedgerScan, Outcome, PreconditionEval, RecordDraft};
use crate::rng::RngState;
use crate::snapshot::Snapshot;
use crate::state::{Instance, InstanceMeta, ObjectStore, WorldState};
use crate::{canonical_json, io_err, params_hash, sha256_hex, write_atomic, KernelError, Result};

/// The in-flight external call journal (one file, overwritten per call).
pub const INFLIGHT_FILE: &str = "inflight.json";
/// The commit intent of the record being appended.
pub const COMMIT_FILE: &str = "commit.json";
/// Progress of a recovery, removed when it completes.
pub const JOURNAL_FILE: &str = "recovery.json";
/// Fault injection variable (see the module documentation).
pub const FAULT_ENV: &str = "CWR_FAULT_INJECT";
/// The pseudo-primitive named in the failure of an interrupted turn.
pub const RECOVERY_PRIMITIVE: &str = "kernel.recovery";

// ---------------------------------------------------------------------------------------------
// Fault injection
// ---------------------------------------------------------------------------------------------

extern "C" {
    fn kill(pid: i32, sig: i32) -> i32;
    fn getpid() -> i32;
}

/// SIGKILLs this process if `CWR_FAULT_INJECT` is exactly `<point>:<selector>`.
pub(crate) fn maybe_fault(point: &str, selector: &str) {
    let Ok(spec) = std::env::var(FAULT_ENV) else {
        return;
    };
    if spec.split_once(':') == Some((point, selector)) {
        // SAFETY: kill(2) on our own pid with SIGKILL; no memory is touched.
        unsafe {
            kill(getpid(), 9);
        }
        std::process::abort();
    }
}

// ---------------------------------------------------------------------------------------------
// Commit intent (called by Instance::append around the ledger append)
// ---------------------------------------------------------------------------------------------

/// Called by [`Instance::append`] after the new state object is in the object store and before
/// the record is appended. If the record commits a state change or an RNG advance, the intent
/// `{seq, prev_chain, state_hash, rng}` is written durably first, so recovery can tell what the
/// record committed even if the process dies before the metadata follows the ledger.
pub(crate) fn before_append(
    dir: &Path,
    seq: u64,
    staged_state: Option<&str>,
    meta: &InstanceMeta,
    rng: RngState,
) -> Result<()> {
    if staged_state.is_some() || rng != meta.rng {
        let intent = json!({
            "format": "cwr.commit-intent",
            "seq": seq,
            "prev_chain": meta.chain,
            "state_hash": staged_state.unwrap_or(&meta.state_hash),
            "rng": rng.to_json(),
        });
        write_atomic(
            &dir.join(COMMIT_FILE),
            format!("{}\n", canonical_json(&intent)).as_bytes(),
        )?;
    }
    maybe_fault("before_append", &seq.to_string());
    Ok(())
}

/// Called by [`Instance::append`] right after the record is durable, before the metadata write.
pub(crate) fn after_append(seq: u64) {
    maybe_fault("after_append", &seq.to_string());
}

// ---------------------------------------------------------------------------------------------
// The in-flight journal around the adapter gateway
// ---------------------------------------------------------------------------------------------

/// Wraps a [`Gateway`] so that every external call is journalled in `inflight.json`, durably,
/// before the inner gateway may send anything, and again with its evidence once it returns. A
/// crash between the journal and the ledger record leaves the call identifiable for recovery,
/// which reconstructs its record as `UNAVAILABLE` instead of re-executing it.
pub struct Journaled<G: Gateway> {
    inner: G,
    dir: PathBuf,
    turn: u64,
    evidence: Option<Value>,
}

impl<G: Gateway> Journaled<G> {
    /// The journal for the next turn of `inst`.
    pub fn new(inner: G, inst: &Instance) -> Self {
        Journaled {
            inner,
            dir: inst.dir.clone(),
            turn: inst.meta.turn + 1,
            evidence: None,
        }
    }

    /// The wrapped gateway, for reading its counters or evidence after a turn.
    pub fn inner(&self) -> &G {
        &self.inner
    }

    /// Unwraps the journal; the in-flight file stays until the next call or recovery removes it.
    pub fn into_inner(self) -> G {
        self.inner
    }
}

impl<G: Gateway> Gateway for Journaled<G> {
    fn call(
        &mut self,
        adapter: &Contract,
        params: &Map<String, Value>,
        world_time: u64,
    ) -> GatewayOutcome {
        self.evidence = None;
        maybe_fault("before_execute", &adapter.name);
        let pv = Value::Object(params.clone());
        let mut marker = json!({
            "format": "cwr.inflight",
            "turn": self.turn,
            "world_time": world_time,
            "adapter": adapter.name,
            "params_hash": params_hash(&pv),
            "request_hash": request_hash(&adapter.name, &pv),
            "idempotency": adapter.raw.get("idempotency").cloned().unwrap_or(Value::Null),
            "phase": "dispatched",
            "evidence": Value::Null,
        });
        let path = self.dir.join(INFLIGHT_FILE);
        if let Err(e) = write_atomic(&path, format!("{}\n", canonical_json(&marker)).as_bytes()) {
            return GatewayOutcome::Fail {
                code: Code::Unavailable,
                retry_after: None,
                detail: format!(
                    "{}: the kernel could not journal the call; nothing was sent ({e})",
                    adapter.name
                ),
            };
        }
        let out = self.inner.call(adapter, params, world_time);
        self.evidence = self.inner.evidence();
        marker["phase"] = json!("returned");
        marker["evidence"] = self.evidence.clone().unwrap_or(Value::Null);
        let _ = write_atomic(&path, format!("{}\n", canonical_json(&marker)).as_bytes());
        maybe_fault("after_execute", &adapter.name);
        out
    }

    fn evidence(&mut self) -> Option<Value> {
        self.evidence.take()
    }

    fn set_deadline(&mut self, deadline: std::time::Instant) {
        self.inner.set_deadline(deadline);
    }
}

// ---------------------------------------------------------------------------------------------
// Recovery

// ---------------------------------------------------------------------------------------------

/// What one recovery did (operator report; the ledger records are the evidence).
#[derive(Debug, Clone)]
pub struct RecoveryReport {
    /// The recovered Instance id.
    pub instance: String,
    /// The snapshot restored (the rolled-forward capture at the last durable record).
    pub snapshot: String,
    /// Where the base capture came from: `metadata` or `snapshot sha256:...`.
    pub base: String,
    /// Seq of the last durable record the Instance resumed from.
    pub resumed_from: u64,
    /// Records after the base whose effects were rolled forward.
    pub rolled_forward: Vec<u64>,
    /// Bytes of a truncated final record that were cut.
    pub truncated_bytes: usize,
    /// The turn the crash interrupted and recovery closed; `None` when the crash was between turns.
    pub interrupted_turn: Option<u64>,
    /// Status of the interrupted turn's closing record.
    pub turn_status: Option<String>,
    /// Seq of the reconstructed record of an in-flight external call.
    pub reconstructed: Option<u64>,
    /// Idempotency keys completed with UNAVAILABLE: `adapter key`.
    pub resolved_keys: Vec<String>,
    /// Every record recovery appended, in order.
    pub appended: Vec<Value>,
    /// Content hash of the recovered state: the state after the last durable record.
    pub state_hash: String,
}

impl RecoveryReport {
    /// The report as one JSON object; appended records are summarised by seq, kind and turn.
    pub fn to_json(&self) -> Value {
        json!({
            "instance": self.instance,
            "snapshot": self.snapshot,
            "base": self.base,
            "resumed_from": self.resumed_from,
            "rolled_forward": self.rolled_forward,
            "truncated_bytes": self.truncated_bytes,
            "interrupted_turn": self.interrupted_turn,
            "turn_status": self.turn_status,
            "reconstructed": self.reconstructed,
            "resolved_keys": self.resolved_keys,
            "appended": self.appended.iter().map(|r| json!({"seq": r["seq"], "kind": r["kind"], "turn": r["turn"]})).collect::<Vec<_>>(),
            "state_hash": self.state_hash,
        })
    }
}

/// Takes the Instance's operation lock if it is free. `None` means another process holds it, so a
/// kernel is alive in that Instance and nothing must be recovered.
fn try_lock(dir: &Path) -> Result<Option<File>> {
    let p = dir.join("lock");
    let f = fs::OpenOptions::new()
        .create(true)
        .truncate(false)
        .write(true)
        .open(&p)
        .map_err(|e| io_err("open lock", &p, e))?;
    Ok(f.try_lock().ok().map(|_| f))
}

fn read_json(path: &Path) -> Option<Value> {
    fs::read(path)
        .ok()
        .and_then(|b| serde_json::from_slice(&b).ok())
}

fn write_meta(dir: &Path, meta: &InstanceMeta) -> Result<()> {
    let mut s = serde_json::to_string_pretty(&meta.to_json())
        .map_err(|e| KernelError::Io(format!("serialise instance metadata: {e}")))?;
    s.push('\n');
    write_atomic(&dir.join("instance.json"), s.as_bytes())
}

/// Whether an Instance needs recovery: it was left in a turn or mid-recovery, its ledger has a
/// truncated final record, or its metadata is not at the ledger's last record. An Instance whose
/// lock is held by a live process never needs recovery.
pub fn needs_recovery(home: &Path, id: &str) -> Result<bool> {
    let dir = Instance::checked_dir(home, id)?;
    if !dir.is_dir() {
        return Err(KernelError::Refused(format!("no instance {id}")));
    }
    let Some(_lock) = try_lock(&dir)? else {
        return Ok(false);
    };
    let meta = Instance::read_meta(home, id)?;
    if meta.status == "destroyed" {
        return Ok(false);
    }
    if matches!(meta.status.as_str(), "in_turn" | "recovering") || dir.join(JOURNAL_FILE).is_file()
    {
        return Ok(true);
    }
    let scan = Ledger::scan_lenient(&dir.join("ledger.jsonl"))?;
    Ok(scan.truncated_tail.is_some()
        || scan.last_seq() != Some(meta.seq)
        || scan.last_chain() != Some(meta.chain.as_str()))
}

/// True when the ledger of `id` ends in a truncated record while the Instance is not in a state a
/// kernel crash leaves behind (`in_turn`, `recovering`, `upgrading` or a recovery journal).
fn torn_without_crash_state(home: &Path, id: &str) -> Result<bool> {
    let dir = Instance::checked_dir(home, id)?;
    let meta = Instance::read_meta(home, id)?;
    if matches!(meta.status.as_str(), "in_turn" | "recovering" | "upgrading")
        || dir.join(JOURNAL_FILE).is_file()
    {
        return Ok(false);
    }
    Ok(Ledger::scan_lenient(&dir.join("ledger.jsonl"))?
        .truncated_tail
        .is_some())
}

/// A record that may have changed the world state: an ok mutating primitive.
fn may_change_state(r: &Value) -> bool {
    matches!(r["kind"].as_str(), Some("primitive") | Some("adapter"))
        && r["outcome"]["status"] == "ok"
        && r["outcome"]["side_effect"] == "mutating"
}

/// The latest snapshot of `id` whose (seq, chain) is a record of `scan`.
fn latest_snapshot_in(dir: &Path, scan: &LedgerScan, id: &str) -> Option<(Snapshot, String)> {
    let mut best: Option<(Snapshot, String)> = None;
    for e in fs::read_dir(dir).ok()?.flatten() {
        let Ok((snap, hash)) = Snapshot::load(&e.path()) else {
            continue;
        };
        if snap.instance != id {
            continue;
        }
        let in_lineage = scan
            .records
            .get(snap.seq as usize)
            .and_then(|r| r["chain"].as_str())
            == Some(snap.chain.as_str());
        if in_lineage && best.as_ref().map(|(b, _)| snap.seq > b.seq).unwrap_or(true) {
            best = Some((snap, hash));
        }
    }
    best
}

/// Recovers Instance `id` if it needs it and returns it open, with the report of what recovery did.
pub fn open_recovering(home: &Path, id: &str) -> Result<(Instance, Option<RecoveryReport>)> {
    if needs_recovery(home, id)? {
        // kernel.md section 8: a truncated final record is refused until restore or an explicit
        // recovery has run. Only an Instance a crash left mid-operation (section 9: `in_turn`,
        // `recovering`, or a recovery journal) is recovered implicitly on open.
        if torn_without_crash_state(home, id)? {
            return Err(KernelError::Truncated(format!(
                "instance {id}: the ledger's final record is truncated and the Instance is not in a \
                 crash state; run `cwr instance recover {id}` or `cwr instance restore` first"
            )));
        }
        let (inst, rep) = recover(home, id)?;
        return Ok((inst, Some(rep)));
    }
    Ok((Instance::open(home, id)?, None))
}

/// Recovers Instance `id` after a kernel crash (see the module documentation). Refuses an
/// Instance whose lock is held, a destroyed one, an interrupted upgrade (rolled back by the upgrade
/// transaction) and a ledger whose complete records fail the seq or chain check.
pub fn recover(home: &Path, id: &str) -> Result<(Instance, RecoveryReport)> {
    let dir = Instance::checked_dir(home, id)?;
    if !dir.is_dir() {
        return Err(KernelError::Refused(format!("no instance {id}")));
    }
    // Phase 1, under the lock: compute the capture at the last durable record.
    let lock = try_lock(&dir)?.ok_or_else(|| {
        KernelError::Conflict(format!(
            "instance {id} is busy (lock held by a live kernel); nothing to recover"
        ))
    })?;
    let mut meta = Instance::read_meta(home, id)?;
    match meta.status.as_str() {
        "destroyed" => return Err(KernelError::State(format!("instance {id} is destroyed"))),
        "upgrading" => return Err(KernelError::Refused(format!("instance {id} was interrupted during an upgrade; the upgrade transaction restores its pre-upgrade snapshot"))),
        "created" => return Err(KernelError::Refused(format!("instance {id} never became idle (crash during create); create a new Instance"))),
        _ => {}
    }
    let ledger_path = dir.join("ledger.jsonl");
    let scan = Ledger::scan_lenient(&ledger_path)?;
    if scan.records.is_empty() {
        return Err(KernelError::Corrupt(format!(
            "{}: no durable header",
            ledger_path.display()
        )));
    }
    let truncated_bytes = scan.truncated_tail.as_ref().map(Vec::len).unwrap_or(0);
    let journal = read_json(&dir.join(JOURNAL_FILE));
    let objects = ObjectStore::new(dir.join("objects"));

    let meta_ok = scan
        .records
        .get(meta.seq as usize)
        .and_then(|r| r["chain"].as_str())
        == Some(meta.chain.as_str());
    let (base, base_seq, mut state_hash, mut rng, mut world_time, mut turn, mut in_turn) =
        if meta_ok {
            (
                "metadata".to_string(),
                meta.seq,
                meta.state_hash.clone(),
                meta.rng,
                meta.world_time,
                meta.turn,
                meta.status == "in_turn",
            )
        } else {
            let (snap, hash) = latest_snapshot_in(&dir.join("snapshots"), &scan, id)
            .ok_or_else(|| KernelError::Corrupt(format!("instance {id}: metadata (seq {}) is not in the ledger and no snapshot is in its lineage", meta.seq)))?;
            let h = objects.put(&snap.state.bytes())?;
            (
                format!("snapshot {hash}"),
                snap.seq,
                h,
                snap.rng,
                snap.world_time,
                snap.turn,
                false,
            )
        };

    // Roll forward the records after the base.
    let intent = read_json(&dir.join(COMMIT_FILE));
    let mut rolled_forward = Vec::new();
    let after_base = scan
        .records
        .get(base_seq as usize + 1..)
        .unwrap_or_default();
    for r in after_base {
        let seq = r["seq"].as_u64().unwrap_or(0);
        match r["kind"].as_str().unwrap_or("") {
            "upgrade" => return Err(KernelError::Refused(format!("instance {id}: an upgrade record (seq {seq}) follows the recovery base; the upgrade transaction owns that recovery"))),
            "turn" | "recovery" => {
                turn = r["turn"].as_u64().unwrap_or(turn);
                in_turn = false;
            }
            _ => in_turn = true,
        }
        world_time = r["world_time"].as_u64().unwrap_or(world_time);
        let prev_chain = seq
            .checked_sub(1)
            .and_then(|p| scan.records.get(p as usize))
            .and_then(|p| p["chain"].as_str())
            .unwrap_or("");
        let covered = intent.as_ref().filter(|i| {
            i["seq"].as_u64() == Some(seq) && i["prev_chain"].as_str() == Some(prev_chain)
        });
        if let Some(i) = covered {
            let h = i["state_hash"].as_str().unwrap_or("").to_string();
            objects.get(&h)?;
            state_hash = h;
            rng = RngState::from_json(&i["rng"])?;
        } else if may_change_state(r) && !(meta_ok && seq == base_seq + 1) {
            // Without an intent, only the record right after the metadata capture is known not to
            // have changed state (the intent is written before every state-changing append).
            return Err(KernelError::Refused(format!(
                "instance {id}: record {seq} ({}) changed state after the recovery base ({base}) and no commit intent covers it; replay the Instance instead",
                r["primitive"].as_str().unwrap_or("?")
            )));
        }
        rolled_forward.push(seq);
    }
    // A recovery interrupted after it closed nothing: its journal names the interrupted turn.
    if let Some(n) = journal
        .as_ref()
        .and_then(|j| j["interrupted_turn"].as_u64())
    {
        let closed = scan
            .records
            .iter()
            .any(|r| r["kind"] == "turn" && r["turn"].as_u64() == Some(n));
        if !closed && turn + 1 == n {
            in_turn = true;
        }
    }
    let interrupted = in_turn.then_some(turn + 1);

    let Some(last) = scan.records.last() else {
        return Err(KernelError::Corrupt(format!(
            "{}: no durable header",
            ledger_path.display()
        )));
    };
    let resumed_from = last["seq"].as_u64().unwrap_or(0);
    let state_bytes = objects.get(&state_hash)?;
    let state = WorldState::from_json(
        &serde_json::from_slice(&state_bytes)
            .map_err(|e| KernelError::Corrupt(format!("state object {state_hash}: {e}")))?,
    )?;
    let build = last["build"].as_str().unwrap_or(&meta.build).to_string();
    if build != meta.build {
        return Err(KernelError::Refused(format!("instance {id}: the ledger's last record names Build {build}, the metadata {}; the upgrade transaction owns that recovery", meta.build)));
    }
    let snap = Snapshot {
        instance: meta.id.clone(),
        build,
        seq: resumed_from,
        chain: last["chain"].as_str().unwrap_or("").to_string(),
        world_time,
        turn,
        rng,
        tail_size: meta.tail_size,
        seed: meta.seed,
        state,
    };
    let snap_hash = snap.write(&dir.join("snapshots"))?;
    write_atomic(&dir.join(JOURNAL_FILE), format!("{}\n", canonical_json(&json!({"format": "cwr.recovery-journal", "interrupted_turn": interrupted, "snapshot": snap_hash}))).as_bytes())?;
    meta.seq = snap.seq;
    meta.chain = snap.chain.clone();
    meta.world_time = world_time;
    meta.turn = turn;
    meta.rng = rng;
    meta.state_hash = state_hash;
    meta.status = if interrupted.is_some() {
        "in_turn".into()
    } else {
        "idle".into()
    };
    write_meta(&dir, &meta)?;
    drop(lock);

    // Phase 2: restore the capture (appends the recovery record), then close the turn.
    let (mut inst, rec) = Instance::restore(home, id, &snap_hash)?;
    let mut appended = vec![rec];
    let mut reconstructed = None;
    let mut turn_status = None;
    if let Some(n) = interrupted {
        let t = inst.begin_turn()?;
        if t != n {
            return Err(KernelError::State(format!(
                "instance {id}: interrupted turn {n}, but the next turn is {t}"
            )));
        }
        if let Some(rec) = reconstruct_in_flight(&mut inst, n)? {
            reconstructed = rec["seq"].as_u64();
            appended.push(rec);
        }
        let rec = close_turn(&mut inst, n)?;
        turn_status = rec["status"].as_str().map(str::to_string);
        appended.push(rec);
    }
    let resolved_keys = resolve_pending_keys(&inst.dir)?;
    for f in [INFLIGHT_FILE, COMMIT_FILE, JOURNAL_FILE] {
        let _ = fs::remove_file(inst.dir.join(f));
    }
    let report = RecoveryReport {
        instance: id.to_string(),
        snapshot: snap_hash,
        base,
        resumed_from,
        rolled_forward,
        truncated_bytes,
        interrupted_turn: interrupted,
        turn_status,
        reconstructed,
        resolved_keys,
        appended,
        state_hash: inst.meta.state_hash.clone(),
    };
    Ok((inst, report))
}

/// The record of the in-flight external call of turn `n`, if the journal names one that has no
/// ledger record: code `UNAVAILABLE`, never re-executed.
fn reconstruct_in_flight(inst: &mut Instance, n: u64) -> Result<Option<Value>> {
    let Some(m) = read_json(&inst.dir.join(INFLIGHT_FILE)) else {
        return Ok(None);
    };
    if m["turn"].as_u64() != Some(n) {
        return Ok(None);
    }
    let adapter = m["adapter"].as_str().unwrap_or("").to_string();
    let wt = m["world_time"].as_u64().unwrap_or(0);
    let tick = inst.build.clock.tick_per_primitive;
    let recorded = inst.records().iter().any(|r| {
        r["kind"] == "adapter"
            && r["turn"].as_u64() == Some(n)
            && r["primitive"].as_str() == Some(adapter.as_str())
            && r["params_hash"] == m["params_hash"]
            && r["world_time"].as_u64() == Some(wt + tick)
    });
    if recorded {
        return Ok(None);
    }
    let table = ContractTable::load(&inst.build)?;
    let c = table
        .get(&adapter)
        .filter(|c| c.is_adapter)
        .ok_or_else(|| {
            KernelError::Corrupt(format!(
                "in-flight journal names {adapter:?}, which is not an adapter of this Build"
            ))
        })?;
    if inst.clock.now() != wt {
        return Err(KernelError::Corrupt(format!("in-flight journal of {adapter} at world_time {wt} does not follow the last durable record (world_time {})", inst.clock.now())));
    }
    let rh = m["request_hash"].as_str().unwrap_or("").to_string();
    let evidence = if m["phase"] == "returned" && m["evidence"].is_object() {
        m["evidence"].clone()
    } else {
        // Dispatched to the service path; whether a request left the host is unknown.
        let seq = ordinals_from_ledger(inst.records())
            .get(&(adapter.clone(), rh.clone()))
            .copied()
            .unwrap_or(0);
        json!({"idempotency": m["idempotency"], "request_hash": rh, "via": "service", "seq": seq, "response_hash": null})
    };
    inst.clock.advance_primitive()?;
    let mut d = RecordDraft::new(Kind::Adapter, n, inst.clock.now());
    d.primitive = Some(adapter.clone());
    d.params_hash = m["params_hash"].as_str().map(str::to_string);
    d.preconditions = c
        .preconditions
        .iter()
        .enumerate()
        .map(|(i, (src, _, _))| PreconditionEval {
            index: (i + 1) as u32,
            expr_hash: format!("sha256:{}", sha256_hex(src.as_bytes())),
            passed: true,
        })
        .collect();
    d.outcome = Some(Outcome::error(
        Code::Unavailable,
        &c.returns_text,
        &c.side_effect,
    ));
    d.extra.insert("content_guaranteed".into(), json!(false));
    d.extra.insert("gateway".into(), evidence);
    d.extra.insert("reconstructed".into(), json!(true));
    d.extra.insert(
        "detail".into(),
        json!(format!("the kernel stopped while {adapter} was in flight; its outcome is unknown and it is not executed again; the external effect may have happened")),
    );
    Ok(Some(inst.append(d, None)?))
}

/// Closes interrupted turn `n` with code UNAVAILABLE and writes its result envelope.
fn close_turn(inst: &mut Instance, n: u64) -> Result<Value> {
    let calls: Vec<Value> = inst
        .records()
        .iter()
        .filter(|r| {
            r["turn"].as_u64() == Some(n)
                && matches!(r["kind"].as_str(), Some("primitive") | Some("adapter"))
        })
        .cloned()
        .collect();
    let mutated = calls.iter().any(|r| {
        r["outcome"]["status"] == "ok"
            && matches!(
                r["outcome"]["side_effect"].as_str(),
                Some("mutating") | Some("external")
            )
    });
    let status = if mutated { "partial" } else { "error" };
    let adapter_calls = calls
        .iter()
        .filter(|r| r["kind"] == "adapter" && r.get("gateway").is_some())
        .count() as u64;
    let limit = |k: &str| inst.build.limits.get(k).copied().unwrap_or(0);
    let res = Resources {
        cpu_ms: 0,
        wall_ms: 0,
        primitive_calls: calls.len() as u64,
        adapter_calls,
        remaining_primitive_calls: limit("primitive_calls").saturating_sub(calls.len() as u64),
        remaining_adapter_calls: limit("adapter_calls").saturating_sub(adapter_calls),
    };
    let rec = inst.end_turn(
        status,
        Some(Code::Unavailable),
        calls.len() as u64,
        &Value::Null,
        res.ledger_json(),
    )?;
    let trace: Vec<Value> = calls
        .iter()
        .map(|r| {
            let code = r["outcome"]["code"]
                .as_str()
                .and_then(|c| c.parse::<Code>().ok());
            let cg = r["content_guaranteed"]
                .as_bool()
                .unwrap_or(r["kind"] != "adapter");
            trace_item(
                r["primitive"].as_str().unwrap_or(""),
                code,
                r["world_time"].as_u64().unwrap_or(0),
                cg,
            )
        })
        .collect();
    let failure = Failure::new(
        Code::Unavailable,
        RECOVERY_PRIMITIVE,
        "the world stopped during this turn and was recovered; every call in the trace completed and stands, an external call without an outcome was not repeated, and the rest of the script did not run",
    );
    let world = json!({"version": inst.build.version, "build": inst.build.hash, "time": rec["world_time"], "turn": rec["turn"]});
    let env = turn_envelope(status, Value::Null, Some(&failure), trace, world, &res);
    crate::surface::envelope_render::store(&inst.dir, &rec, &env)?;
    Ok(rec)
}

/// Completes every idempotency key a crash left pending with UNAVAILABLE as its stored outcome.
fn resolve_pending_keys(dir: &Path) -> Result<Vec<String>> {
    let gw = dir.join(GATEWAY_DIR);
    if !gw.is_dir() {
        return Ok(Vec::new());
    }
    let mut store = KeyStore::open(&gw).map_err(KernelError::Io)?;
    let mut done = Vec::new();
    for (adapter, key, _) in store.pending() {
        let Some(Slot::Pending {
            payload_hash,
            request_hash,
        }) = store.slot(&adapter, &key).cloned()
        else {
            continue;
        };
        let stored = Stored::Fail {
            code: Code::Unavailable.as_str().to_string(),
            retry_after: None,
            detail: "the kernel stopped while this request was in flight; whether the service applied it is unknown; check its status and use a new key".into(),
        };
        store
            .complete(&adapter, &key, &payload_hash, &request_hash, &stored)
            .map_err(KernelError::Io)?;
        done.push(format!("{adapter} {key}"));
    }
    Ok(done)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn state_change_classification() {
        let ok_mut =
            json!({"kind": "primitive", "outcome": {"status": "ok", "side_effect": "mutating"}});
        let ok_ro =
            json!({"kind": "primitive", "outcome": {"status": "ok", "side_effect": "read_only"}});
        let err_mut =
            json!({"kind": "primitive", "outcome": {"status": "error", "side_effect": "mutating"}});
        let ext =
            json!({"kind": "adapter", "outcome": {"status": "ok", "side_effect": "external"}});
        let turn = json!({"kind": "turn", "outcome": null});
        assert!(may_change_state(&ok_mut));
        assert!(!may_change_state(&ok_ro));
        assert!(!may_change_state(&err_mut));
        assert!(!may_change_state(&ext));
        assert!(!may_change_state(&turn));
    }

    #[test]
    fn fault_spec_must_match_exactly() {
        // The variable is unset in unit tests; a non-matching spec must never fire.
        std::env::remove_var(FAULT_ENV);
        maybe_fault("before_append", "3");
    }
}
