// Copyright Sidiora Labs Inc. CWR® 2026-2027
// SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0

//! The adapter gateway (kernel.md section 7; task 2.3; clauses 1.3, 7.1 to 7.6; case CWR-009).
//!
//! The gateway is the kernel's only network capability. The atomic contract check calls it at
//! step 3 of an external primitive, after parameter validation, the preconditions and the
//! `adapter_calls` budget, through the [`Gateway`] seam; the check then ticks `world_time` by
//! exactly `tick_per_primitive` (however long the host waited) and appends the ledger record.
//!
//! One call, in order:
//!
//! 1. Compute the canonical request hash ([`cassette::request_hash`]).
//! 2. `key` adapters: consult the per-Instance idempotency store ([`idempotency`]). An identical
//!    request is answered with the stored first outcome (ok or failure) and nothing is sent; a
//!    different payload under a stored key is `DUPLICATE_KEY` with the original receipt (or
//!    original failure); otherwise the key is written as pending, durably, before anything is sent.
//! 3. Run the adapter: the Build's `adapters/<module>/live.py` function in the sandboxed
//!    adapter-code worker ([`live::AdapterCode`]), whose `gateway.send(payload)` is served here,
//!    at most once per call; or, for an adapter without adapter code, one direct send of the
//!    parameters. A send goes to the declared endpoint in `live` and `record` mode
//!    ([`live::post`], under the contract's `timeout_ms` of host time), and is appended to the
//!    cassette in `record` mode; in `replay` mode it is served from the cassette by (adapter,
//!    request hash, sequence) and a miss is `UNAVAILABLE` without any socket.
//! 4. Normalise the raw response ([`normalise`]) into a body or a failure, let the adapter code
//!    reshape the body, and check the result against the declared return shape. Every failure
//!    lands on a declared code; a failure of the adapter code itself (a second send, an exception
//!    before sending, a missing function) is reported as a defect that the contract check
//!    records as `INVARIANT_VIOLATION`.
//! 5. `key` adapters: store the outcome, ok or declared failure, as the key's first outcome. Two
//!    endings release the pending key instead: a defect (`INVARIANT_VIOLATION`, never an
//!    outcome), and `RATE_LIMITED`, where the service declined to process the request and told
//!    the model when to repeat it, so an identical call after `retry_after` ticks is sent rather
//!    than answered with a stale rate limit (clause 7.3; the manual's same-key retry example).
//! 6. Leave the ledger evidence for the record ([`Gateway::evidence`]): idempotency class,
//!    request hash, whether the answer came from the service (or its recording) or from the key
//!    store, the sequence and the response hash. The evidence is identical in record and replay
//!    mode, so a replayed ledger equals the recorded one.
//!
//! The gateway never retries. Payloads are content, never guaranteed: the contract check marks
//! every adapter record and result `content_guaranteed: false`.

pub mod cassette;
pub mod idempotency;
pub mod live;
pub mod normalise;
pub mod timeout;

use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::path::{Path, PathBuf};
use std::time::Duration;

use cwr_contract::Code;
use serde_json::{json, Map, Value};

use crate::contract_check::{
    check_shape, has_bad_tag, Contract, ContractTable, Gateway, GatewayOutcome, Records,
};
use crate::state::{Build, Instance};
use crate::{canonical_json, KernelError, Result};

use cassette::{request_hash, Cassette, Entry, RawResponse};
use idempotency::{Idempotency, KeyStore, Lookup, Stored};
use live::{AdapterCode, AdapterConsts};
use normalise::{normalise, Normalised, Rules};
use timeout::{contract_timeout_ms, Deadline};

/// Directory of gateway state inside an Instance directory.
pub const GATEWAY_DIR: &str = "gateway";

/// Gateway mode (kernel.md section 7).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Mode {
    /// Calls the declared endpoint; records nothing.
    Live,
    /// Calls the declared endpoint and appends one cassette entry per call.
    Record,
    /// Serves cassette entries; a miss is `UNAVAILABLE` and no socket is opened.
    Replay,
}

impl std::str::FromStr for Mode {
    type Err = String;
    fn from_str(s: &str) -> std::result::Result<Mode, String> {
        match s {
            "live" => Ok(Mode::Live),
            "record" => Ok(Mode::Record),
            "replay" => Ok(Mode::Replay),
            o => Err(format!(
                "gateway mode {o:?}: expected live, record or replay"
            )),
        }
    }
}

impl Mode {
    /// The command-line spelling of the mode.
    pub fn as_str(self) -> &'static str {
        match self {
            Mode::Live => "live",
            Mode::Record => "record",
            Mode::Replay => "replay",
        }
    }
}

/// Operator configuration of the gateway (never chosen by the model).
#[derive(Debug, Clone)]
pub struct GatewayConfig {
    /// Live, record or replay.
    pub mode: Mode,
    /// Stub server name to base URL, e.g. `search` to `http://127.0.0.1:8001`.
    pub stubs: BTreeMap<String, String>,
    /// The Instance's cassette (required for record and replay, refused for live).
    pub cassette: Option<PathBuf>,
    /// Header label for a cassette created in record mode (`<task>/seed-<n>` by convention).
    pub label: Option<String>,
}

impl Default for GatewayConfig {
    fn default() -> Self {
        GatewayConfig {
            mode: Mode::Live,
            stubs: BTreeMap::new(),
            cassette: None,
            label: None,
        }
    }
}

impl GatewayConfig {
    /// Parses one `NAME=URL` stub binding.
    pub fn add_stub(&mut self, binding: &str) -> std::result::Result<(), String> {
        let (name, url) = binding
            .split_once('=')
            .ok_or_else(|| format!("stub binding {binding:?}: expected NAME=URL"))?;
        if name.is_empty()
            || !name
                .chars()
                .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '_')
        {
            return Err(format!("stub binding {binding:?}: bad server name"));
        }
        live::resolve(
            &format!("stub://{name}/"),
            &[(name.to_string(), url.to_string())].into_iter().collect(),
        )?;
        self.stubs.insert(name.to_string(), url.to_string());
        Ok(())
    }
}

/// What the gateway did for one call (operator diagnostics; never shown to the model).
#[derive(Debug, Clone)]
pub struct CallNote {
    /// Qualified adapter name.
    pub adapter: String,
    /// Request hash of the call.
    pub request_hash: String,
    /// Who answered: `service` or `store`.
    pub via: &'static str,
    /// Whether anything was sent to the service.
    pub sent: bool,
}

/// The adapter gateway of one Instance for one kernel process.
pub struct AdapterGateway {
    mode: Mode,
    stubs: BTreeMap<String, String>,
    cassette: Option<Cassette>,
    store: KeyStore,
    instance: String,
    adapters_dir: PathBuf,
    live_modules: BTreeSet<String>,
    adapter_names: Vec<String>,
    records: Records,
    code: Option<AdapterCode>,
    code_error: Option<String>,
    ordinals: HashMap<(String, String), u64>,
    evidence: Option<Value>,
    /// Every call this gateway handled, in order.
    pub notes: Vec<CallNote>,
    /// The turn's wall-clock deadline; no host exchange outlasts it.
    turn_deadline: Option<std::time::Instant>,
    /// The last send of the current call found no cassette entry (replay mode). The service never
    /// saw the request, so a key adapter's slot is released rather than filled with UNAVAILABLE.
    replay_miss: bool,
}

/// Service-traffic ordinals per (adapter, request hash) already in a ledger: the sequence the
/// next identical request gets, so sequences are Instance-scoped across turns and processes.
pub fn ordinals_from_ledger(records: &[Value]) -> HashMap<(String, String), u64> {
    let mut m = HashMap::new();
    for r in records {
        if r.get("kind").and_then(Value::as_str) != Some("adapter") {
            continue;
        }
        let Some(g) = r.get("gateway") else { continue };
        if g.get("via").and_then(Value::as_str) != Some("service") {
            continue;
        }
        let (Some(p), Some(h)) = (
            r.get("primitive").and_then(Value::as_str),
            g.get("request_hash").and_then(Value::as_str),
        ) else {
            continue;
        };
        *m.entry((p.to_string(), h.to_string())).or_insert(0) += 1;
    }
    m
}

impl AdapterGateway {
    /// The gateway for `inst` under `cfg`.
    pub fn new(cfg: GatewayConfig, inst: &Instance) -> Result<AdapterGateway> {
        AdapterGateway::for_build(cfg, &inst.build, &inst.meta.id, &inst.dir, inst.records())
    }

    /// The gateway for an Instance given by parts (used by replay and recovery tooling).
    pub fn for_build(
        cfg: GatewayConfig,
        build: &Build,
        instance_id: &str,
        instance_dir: &Path,
        ledger: &[Value],
    ) -> Result<AdapterGateway> {
        let table = ContractTable::load(build)?;
        let adapter_names: Vec<String> = table
            .contracts
            .values()
            .filter(|c| c.is_adapter)
            .map(|c| c.name.clone())
            .collect();
        let ordinals = ordinals_from_ledger(ledger);
        let refuse = |m: String| KernelError::Refused(format!("adapter gateway: {m}"));
        let cassette = match (cfg.mode, &cfg.cassette) {
            (Mode::Live, Some(_)) => {
                return Err(refuse(
                    "live mode records nothing; a cassette is used only in record or replay mode"
                        .into(),
                ))
            }
            (Mode::Live, None) => None,
            (_, None) => {
                return Err(refuse(format!(
                    "{} mode needs --cassette",
                    cfg.mode.as_str()
                )))
            }
            (Mode::Record, Some(p)) => {
                let c = Cassette::open_for_record(p, cfg.label.as_deref()).map_err(refuse)?;
                let mut want: BTreeMap<(String, String), u64> = BTreeMap::new();
                for (k, v) in &ordinals {
                    want.insert(k.clone(), *v);
                }
                if c.counts() != want {
                    return Err(refuse(format!("cassette {} does not hold exactly this Instance's earlier adapter traffic; record into a new cassette from the Instance's first adapter call", p.display())));
                }
                Some(c)
            }
            (Mode::Replay, Some(p)) => Some(Cassette::load(p).map_err(refuse)?),
        };
        let store = KeyStore::open(&instance_dir.join(GATEWAY_DIR)).map_err(refuse)?;
        let adapters_dir = build.dir.join("adapters");
        Ok(AdapterGateway {
            mode: cfg.mode,
            stubs: cfg.stubs,
            cassette,
            store,
            instance: instance_id.to_string(),
            live_modules: live::live_modules(&adapters_dir),
            adapters_dir,
            adapter_names,
            records: table.records.clone(),
            code: None,
            code_error: None,
            ordinals,
            evidence: None,
            notes: Vec::new(),
            turn_deadline: None,
            replay_miss: false,
        })
    }

    /// The deadline of one host exchange: `timeout_ms` from now, but never past the turn's
    /// wall-clock deadline.
    fn exchange_deadline(&self, timeout_ms: u64) -> Deadline {
        let d = Deadline::after_ms(timeout_ms);
        match self.turn_deadline {
            Some(t) => d.capped(t),
            None => d,
        }
    }

    /// The mode the gateway was opened in.
    pub fn mode(&self) -> Mode {
        self.mode
    }

    /// Keys a crash left pending in the idempotency store (adapter, key, request hash).
    pub fn pending_keys(&self) -> Vec<(String, String, String)> {
        self.store.pending()
    }

    /// The cassette as it stands (record mode: everything written so far).
    pub fn cassette(&self) -> Option<&Cassette> {
        self.cassette.as_ref()
    }

    fn ensure_code(&mut self) {
        if self.code.is_some() || self.code_error.is_some() || self.live_modules.is_empty() {
            return;
        }
        let modules: Vec<String> = self.live_modules.iter().cloned().collect();
        match AdapterCode::spawn(&self.adapters_dir, &modules, &self.adapter_names) {
            Ok(c) => self.code = Some(c),
            Err(e) => self.code_error = Some(e),
        }
    }

    fn consts(&mut self, c: &Contract) -> AdapterConsts {
        if !self.live_modules.contains(&c.module) {
            return AdapterConsts::default();
        }
        self.ensure_code();
        self.code
            .as_ref()
            .and_then(|w| w.constants.get(&c.name).cloned())
            .unwrap_or_default()
    }

    /// One send: the declared endpoint (live, record) or the cassette (replay).
    fn transport(
        &mut self,
        c: &Contract,
        params: &Value,
        payload: &Value,
        world_time: u64,
        ev: &mut Map<String, Value>,
    ) -> std::result::Result<Option<RawResponse>, String> {
        let rh = request_hash(&c.name, params);
        let slot = self
            .ordinals
            .entry((c.name.clone(), rh.clone()))
            .or_insert(0);
        let seq = *slot;
        *slot += 1;
        ev.insert("via".into(), json!("service"));
        ev.insert("seq".into(), json!(seq));
        let raw = match self.mode {
            Mode::Replay => {
                let found = self
                    .cassette
                    .as_ref()
                    .and_then(|k| k.get(&c.name, &rh, seq))
                    .map(|e| e.response.clone());
                self.replay_miss = found.is_none();
                found
            }
            Mode::Live | Mode::Record => {
                let endpoint = c.raw.get("endpoint").and_then(Value::as_str).unwrap_or("");
                let timeout_ms = contract_timeout_ms(&c.raw).ok_or_else(|| {
                    format!("{}: the contract has no positive timeout_ms", c.name)
                })?;
                let raw = match live::resolve(endpoint, &self.stubs) {
                    Err(e) => RawResponse::TransportError {
                        error: format!("EndpointUnavailable: {e}"),
                    },
                    Ok(target) => {
                        let headers = [
                            ("X-CWR-Instance", self.instance.clone()),
                            ("X-CWR-World-Time", world_time.to_string()),
                            ("X-CWR-Adapter", c.name.clone()),
                        ];
                        live::post(
                            &target,
                            canonical_json(payload).as_bytes(),
                            &headers,
                            &self.exchange_deadline(timeout_ms),
                        )
                    }
                };
                if self.mode == Mode::Record {
                    let entry = Entry {
                        adapter: c.name.clone(),
                        params: params.clone(),
                        request_hash: rh.clone(),
                        seq,
                        world_time: Some(world_time),
                        response: raw.clone(),
                    };
                    if let Some(k) = self.cassette.as_mut() {
                        k.append(entry).map_err(|e| format!("record mode: {e}"))?;
                    }
                }
                Some(raw)
            }
        };
        ev.insert(
            "response_hash".into(),
            raw.as_ref().map(|r| json!(r.hash())).unwrap_or(Value::Null),
        );
        self.notes.push(CallNote {
            adapter: c.name.clone(),
            request_hash: rh,
            via: "service",
            sent: self.mode != Mode::Replay,
        });
        Ok(raw)
    }

    fn defect(name: &str, why: impl Into<String>) -> GatewayOutcome {
        // INVARIANT_VIOLATION is never in an adapter's declared failures, so the contract check
        // records this as a defect with the full trace (clause 1.6).
        GatewayOutcome::Fail {
            code: Code::InvariantViolation,
            retry_after: None,
            detail: format!("{name}: {}", why.into()),
        }
    }

    fn run(
        &mut self,
        c: &Contract,
        rules: &Rules,
        consts: &AdapterConsts,
        pmap: &Map<String, Value>,
        world_time: u64,
        ev: &mut Map<String, Value>,
    ) -> GatewayOutcome {
        let params = Value::Object(pmap.clone());
        let use_code = self.live_modules.contains(&c.module);
        if !use_code {
            return match self.transport(c, &params, &params, world_time, ev) {
                Err(e) => GatewayOutcome::Fail {
                    code: Code::Unavailable,
                    retry_after: None,
                    detail: e,
                },
                Ok(raw) => match normalise(rules, raw.as_ref()) {
                    Normalised::Body(v) => GatewayOutcome::Ok(v),
                    Normalised::Fail {
                        code,
                        retry_after,
                        detail,
                    } => GatewayOutcome::Fail {
                        code,
                        retry_after,
                        detail,
                    },
                },
            };
        }
        if let Some(e) = &self.code_error {
            return GatewayOutcome::Fail {
                code: Code::Unavailable,
                retry_after: None,
                detail: format!("the adapter code could not be started; nothing was sent ({e})"),
            };
        }
        if !consts.has_function {
            return Self::defect(
                &c.name,
                "adapters/<module>/live.py has no function for this adapter",
            );
        }
        let Some(mut worker) = self.code.take() else {
            return GatewayOutcome::Fail {
                code: Code::Unavailable,
                retry_after: None,
                detail: "the adapter code is not running; nothing was sent".into(),
            };
        };
        let args: Vec<Value> = c
            .params
            .iter()
            .map(|(n, _)| pmap.get(n).cloned().unwrap_or(Value::Null))
            .collect();
        let mut sends = 0u32;
        let mut answer: Option<Normalised> = None;
        let mut send_error: Option<String> = None;
        let mut budget = Duration::from_millis(contract_timeout_ms(&c.raw).unwrap_or(1000) + 5000);
        if let Some(t) = self.turn_deadline {
            budget = budget.min(t.saturating_duration_since(std::time::Instant::now()));
        }
        let reply = {
            let this = &mut *self;
            worker.request(&json!({"op": "call", "adapter": c.name, "args": args}), budget, &mut |cb| {
                sends += 1;
                if sends > 1 {
                    return json!({"code": "INVARIANT_VIOLATION", "detail": "one send per adapter call: the gateway never retries"});
                }
                let payload = cb.get("payload").cloned().unwrap_or(Value::Null);
                match this.transport(c, &params, &payload, world_time, ev) {
                    Err(e) => {
                        send_error = Some(e);
                        json!({"code": "UNAVAILABLE"})
                    }
                    Ok(raw) => {
                        let n = normalise(rules, raw.as_ref());
                        let r = match &n {
                            Normalised::Body(b) => json!({"body": b}),
                            Normalised::Fail { code, .. } => json!({"code": code.as_str()}),
                        };
                        answer = Some(n);
                        r
                    }
                }
            })
        };
        let worker_alive = reply.is_ok();
        if worker_alive {
            self.code = Some(worker);
        } else {
            drop(worker);
        }
        if sends > 1 {
            return Self::defect(
                &c.name,
                "the adapter code called gateway.send more than once in one call",
            );
        }
        if let Some(e) = send_error {
            return GatewayOutcome::Fail {
                code: Code::Unavailable,
                retry_after: None,
                detail: e,
            };
        }
        // A failure the service produced is the outcome, whatever the adapter code did with it.
        if let Some(Normalised::Fail {
            code,
            retry_after,
            detail,
        }) = &answer
        {
            return GatewayOutcome::Fail {
                code: *code,
                retry_after: *retry_after,
                detail: detail.clone(),
            };
        }
        let sent_ok = matches!(answer, Some(Normalised::Body(_)));
        match reply {
            Err(e) if e.starts_with("timeout") => {
                self.code_error =
                    Some("the adapter-code worker was stopped after exceeding its budget".into());
                rules
                    .own(
                        Code::Timeout,
                        format!(
                            "{e}; timeout_ms {}",
                            contract_timeout_ms(&c.raw).unwrap_or(0)
                        ),
                    )
                    .into_outcome()
            }
            Err(e) => {
                self.code_error = Some(e.clone());
                Self::defect(&c.name, format!("adapter-code worker failed: {e}"))
            }
            Ok(r) => {
                if r.get("missing").is_some() {
                    Self::defect(
                        &c.name,
                        "adapters/<module>/live.py has no function for this adapter",
                    )
                } else if let Some(x) = r.get("exception").and_then(Value::as_str) {
                    if sent_ok {
                        rules.fail(Code::Invalid, None, format!("the service body could not be read into the declared shape: malformed response ({x})")).into_outcome()
                    } else {
                        Self::defect(
                            &c.name,
                            format!("the adapter code raised before any service answer: {x}"),
                        )
                    }
                } else if r.get("failed").is_some() {
                    Self::defect(
                        &c.name,
                        "the adapter code raised a gateway failure the gateway did not produce",
                    )
                } else if let Some(v) = r.get("result") {
                    GatewayOutcome::Ok(v.clone())
                } else {
                    Self::defect(&c.name, "the adapter-code worker gave no result")
                }
            }
        }
    }
}

impl Normalised {
    fn into_outcome(self) -> GatewayOutcome {
        match self {
            Normalised::Body(v) => GatewayOutcome::Ok(v),
            Normalised::Fail {
                code,
                retry_after,
                detail,
            } => GatewayOutcome::Fail {
                code,
                retry_after,
                detail,
            },
        }
    }
}

impl Gateway for AdapterGateway {
    fn call(&mut self, c: &Contract, pmap: &Map<String, Value>, world_time: u64) -> GatewayOutcome {
        self.evidence = None;
        let params = Value::Object(pmap.clone());
        let rh = request_hash(&c.name, &params);
        let idem = Idempotency::of(&c.raw);
        let mut ev = Map::new();
        ev.insert("idempotency".into(), json!(idem.map(Idempotency::as_str)));
        ev.insert("request_hash".into(), json!(rh));
        ev.insert("via".into(), Value::Null);
        ev.insert("seq".into(), Value::Null);
        ev.insert("response_hash".into(), Value::Null);
        let consts = self.consts(c);
        // The contract's own normalisation keys (kernel.md section 7); absent means the defaults.
        let ratio = c.raw.get("retry_after_ratio").and_then(Value::as_f64);
        let undeclared = c
            .raw
            .get("undeclared_status")
            .and_then(Value::as_str)
            .map(str::to_string);
        let rules = Rules::new(
            &c.name,
            c.failures.clone(),
            consts.status_codes.clone(),
            consts.retry_after_field.clone(),
            ratio,
            undeclared.as_deref(),
        );

        let Some(idem) = idem else {
            self.evidence = Some(Value::Object(ev));
            return Self::defect(
                &c.name,
                "the adapter contract declares no valid idempotency class",
            );
        };
        // Step 2: the key store.
        let mut key_slot: Option<(String, String)> = None;
        if idem == Idempotency::Key {
            let kp = c
                .raw
                .get("idempotency_key")
                .and_then(Value::as_str)
                .map(str::to_string)
                .or(consts.key_param.clone())
                .unwrap_or_else(|| "key".into());
            let Some(key) = idempotency::key_of(pmap, &kp) else {
                self.evidence = Some(Value::Object(ev));
                return Self::defect(
                    &c.name,
                    format!("key adapter without its key parameter {kp:?}"),
                );
            };
            let ph = idempotency::payload_hash(pmap, &kp);
            match self.store.lookup(&c.name, &key, &ph) {
                Lookup::Replay(stored) => {
                    ev.insert("via".into(), json!("store"));
                    self.evidence = Some(Value::Object(ev));
                    self.notes.push(CallNote {
                        adapter: c.name.clone(),
                        request_hash: rh,
                        via: "store",
                        sent: false,
                    });
                    return match stored {
                        Stored::Ok(v) => GatewayOutcome::Ok(v),
                        Stored::Fail { code, retry_after, detail } => match code.parse::<Code>() {
                            Ok(code) => GatewayOutcome::Fail { code, retry_after, detail: format!("{detail} (the stored first outcome under idempotency key {key}; nothing was sent)") },
                            Err(_) => Self::defect(&c.name, format!("the idempotency store holds an outcome outside the envelope: {code:?}")),
                        },
                    };
                }
                Lookup::Duplicate { original } => {
                    ev.insert("via".into(), json!("store"));
                    self.evidence = Some(Value::Object(ev));
                    self.notes.push(CallNote {
                        adapter: c.name.clone(),
                        request_hash: rh,
                        via: "store",
                        sent: false,
                    });
                    return rules.own(Code::DuplicateKey, format!("idempotency key {key} was already used with a different payload; nothing was sent; {}", original.describe())).into_outcome();
                }
                Lookup::Miss | Lookup::Pending => {
                    if let Err(e) = self.store.begin(&c.name, &key, &ph, &rh) {
                        self.evidence = Some(Value::Object(ev));
                        return GatewayOutcome::Fail { code: Code::Unavailable, retry_after: None, detail: format!("the idempotency store could not record the key; nothing was sent ({e})") };
                    }
                    key_slot = Some((key, ph));
                }
            }
        }
        // Steps 3 and 4: run, normalise, shape.
        self.replay_miss = false;
        let mut outcome = self.run(c, &rules, &consts, pmap, world_time, &mut ev);
        if let GatewayOutcome::Ok(v) = &outcome {
            if check_shape(v, &c.returns, &self.records).is_err() || has_bad_tag(v) {
                outcome = rules.fail(Code::Invalid, None, format!("the service answered with a body that does not match the declared return shape {}: malformed response", c.returns_text)).into_outcome();
            }
        }
        // Step 5: store the first outcome, or release the key after a defect.
        if let Some((key, ph)) = key_slot {
            let r = match &outcome {
                // A replay request with no cassette entry never reached the service: the key was
                // not consumed, so the slot is released and a corrected call can still use it.
                _ if self.replay_miss => self.store.clear(&c.name, &key),
                GatewayOutcome::Ok(v) => {
                    self.store
                        .complete(&c.name, &key, &ph, &rh, &Stored::Ok(v.clone()))
                }
                GatewayOutcome::Fail {
                    code: Code::InvariantViolation,
                    ..
                } => self.store.clear(&c.name, &key),
                // The service refused to process the request and named when to repeat it: the key
                // was not consumed, so the same call after retry_after is sent (clause 7.3).
                GatewayOutcome::Fail {
                    code: Code::RateLimited,
                    ..
                } => self.store.clear(&c.name, &key),
                GatewayOutcome::Fail {
                    code,
                    retry_after,
                    detail,
                } => {
                    let stored = Stored::Fail {
                        code: code.as_str().to_string(),
                        retry_after: *retry_after,
                        detail: detail.clone(),
                    };
                    self.store.complete(&c.name, &key, &ph, &rh, &stored)
                }
            };
            if let Err(e) = r {
                // The call happened; only the store write failed. Keep the outcome honest about it.
                if let GatewayOutcome::Fail { detail, .. } = &mut outcome {
                    detail.push_str(&format!(" (idempotency store: {e})"));
                }
            }
        }
        if ev.get("via") == Some(&Value::Null) {
            ev.insert("via".into(), json!("none"));
        }
        self.evidence = Some(Value::Object(ev));
        outcome
    }

    fn set_deadline(&mut self, deadline: std::time::Instant) {
        self.turn_deadline = Some(deadline);
    }

    fn evidence(&mut self) -> Option<Value> {
        self.evidence.take()
    }
}

// ---------------------------------------------------------------------------------------------
// Network seal for world-code processes started outside bubblewrap (clause 7.1)
// ---------------------------------------------------------------------------------------------

#[repr(C)]
struct SockFilter {
    code: u16,
    jt: u8,
    jf: u8,
    k: u32,
}

#[repr(C)]
struct SockFprog {
    len: u16,
    filter: *const SockFilter,
}

extern "C" {
    fn prctl(option: i32, arg2: u64, arg3: u64, arg4: u64, arg5: u64) -> i32;
}

const PR_SET_NO_NEW_PRIVS: i32 = 38;
const PR_SET_SECCOMP: i32 = 22;
const SECCOMP_MODE_FILTER: u64 = 2;
const BPF_LD_W_ABS: u16 = 0x20;
const BPF_JEQ_K: u16 = 0x15;
const BPF_JGE_K: u16 = 0x35;
const BPF_RET_K: u16 = 0x06;
const SECCOMP_RET_ALLOW: u32 = 0x7fff_0000;
const SECCOMP_RET_EPERM: u32 = 0x0005_0000 | 1;

/// (audit arch, socket, io_uring_setup, ptrace, process_vm_writev, pidfd_getfd) for this target.
#[cfg(target_arch = "x86_64")]
const SECCOMP_NRS: Option<(u32, u32, u32, u32, u32, u32)> =
    Some((0xC000_003E, 41, 425, 101, 311, 438));
#[cfg(target_arch = "aarch64")]
const SECCOMP_NRS: Option<(u32, u32, u32, u32, u32, u32)> =
    Some((0xC000_00B7, 198, 425, 117, 271, 438));
#[cfg(not(any(target_arch = "x86_64", target_arch = "aarch64")))]
const SECCOMP_NRS: Option<(u32, u32, u32, u32, u32, u32)> = None;

const fn ins(code: u16, jt: u8, jf: u8, k: u32) -> SockFilter {
    SockFilter { code, jt, jf, k }
}

/// The seccomp program: a foreign syscall ABI (i386 or x32 on x86_64) and `socket`,
/// `io_uring_setup` (io_uring can open sockets without `socket`), `ptrace`, `process_vm_writev`
/// and `pidfd_getfd` (borrowing another process's capability) all fail with `EPERM`; everything
/// else is allowed. `seccomp_data` is `{nr: u32, arch: u32, ...}`.
static SECCOMP_FILTER: std::sync::OnceLock<Vec<SockFilter>> = std::sync::OnceLock::new();

fn seccomp_filter() -> Option<&'static [SockFilter]> {
    let (arch, socket, uring, ptrace, vm_writev, getfd) = SECCOMP_NRS?;
    Some(SECCOMP_FILTER.get_or_init(|| {
        vec![
            ins(BPF_LD_W_ABS, 0, 0, 4),              // 0: A = arch
            ins(BPF_JEQ_K, 1, 0, arch),              // 1: native ABI -> 3
            ins(BPF_RET_K, 0, 0, SECCOMP_RET_EPERM), // 2
            ins(BPF_LD_W_ABS, 0, 0, 0),              // 3: A = nr
            ins(BPF_JGE_K, 6, 0, 0x4000_0000),       // 4: x32 ABI -> 11
            ins(BPF_JEQ_K, 5, 0, socket),            // 5 -> 11
            ins(BPF_JEQ_K, 4, 0, uring),             // 6 -> 11
            ins(BPF_JEQ_K, 3, 0, ptrace),            // 7 -> 11
            ins(BPF_JEQ_K, 2, 0, vm_writev),         // 8 -> 11
            ins(BPF_JEQ_K, 1, 0, getfd),             // 9 -> 11
            ins(BPF_RET_K, 0, 0, SECCOMP_RET_ALLOW), // 10
            ins(BPF_RET_K, 0, 0, SECCOMP_RET_EPERM), // 11
        ]
    }))
}

/// Seals a world-code process that cannot be given an empty network namespace (no bubblewrap on
/// the host) so it has no network capability (clause 7.1; kernel.md section 7: "Neither `lib/`
/// code nor the sandbox process has a socket"). Before `exec` the child sets `no_new_privs` and
/// installs a seccomp filter under which `socket(2)` of every family, `io_uring_setup(2)`,
/// `ptrace(2)`, `process_vm_writev(2)`, `pidfd_getfd(2)` and any foreign syscall ABI fail with
/// `EPERM`. The filter is inherited by every descendant across `fork` and `exec` and cannot be
/// removed, so it holds whatever the interpreter-level guard misses (a process spawned through a
/// path that raises no audit event, a native binary). Refused (so the caller fails closed) on a
/// target without a filter; the spawn itself fails if the kernel rejects the filter.
pub fn seal_network(cmd: &mut std::process::Command) -> std::result::Result<(), String> {
    use std::os::unix::process::CommandExt;
    let filter = seccomp_filter().ok_or_else(|| {
        format!(
            "no seccomp network seal for target architecture {}",
            std::env::consts::ARCH
        )
    })?;
    let ptr = filter.as_ptr() as usize;
    let len = filter.len() as u16;
    // SAFETY: the closure runs in the forked child before exec and only calls prctl(2), which is
    // async-signal-safe; the filter is a 'static allocation made before the fork.
    unsafe {
        cmd.pre_exec(move || {
            let prog = SockFprog {
                len,
                filter: ptr as *const SockFilter,
            };
            if prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0
                || prctl(
                    PR_SET_SECCOMP,
                    SECCOMP_MODE_FILTER,
                    &prog as *const SockFprog as u64,
                    0,
                    0,
                ) != 0
            {
                return Err(std::io::Error::last_os_error());
            }
            Ok(())
        });
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ordinals_count_only_service_traffic() {
        let recs = vec![
            json!({"kind": "adapter", "primitive": "search.query", "gateway": {"via": "service", "request_hash": "sha256:a"}}),
            json!({"kind": "adapter", "primitive": "search.query", "gateway": {"via": "service", "request_hash": "sha256:a"}}),
            json!({"kind": "adapter", "primitive": "publish.post", "gateway": {"via": "store", "request_hash": "sha256:b"}}),
            json!({"kind": "primitive", "primitive": "fs.read"}),
        ];
        let m = ordinals_from_ledger(&recs);
        assert_eq!(
            m.get(&("search.query".to_string(), "sha256:a".to_string())),
            Some(&2)
        );
        assert_eq!(m.len(), 1);
    }

    /// A replay gateway for one `key` adapter with no adapter code, over a cassette that holds
    /// exactly one recorded `publish.post` request.
    fn replay_gateway(dir: &Path, recorded: &Value) -> (AdapterGateway, Contract) {
        let _ = std::fs::remove_dir_all(dir);
        std::fs::create_dir_all(dir).unwrap();
        let path = dir.join("cassette.jsonl");
        let mut k = Cassette::open_for_record(&path, Some("replay-miss")).unwrap();
        k.append(Entry {
            adapter: "publish.post".into(),
            params: recorded.clone(),
            request_hash: request_hash("publish.post", recorded),
            seq: 0,
            world_time: Some(1),
            response: RawResponse::Http {
                status: 200,
                headers: BTreeMap::new(),
                body: r#"{"result": {"post_id": "p1"}}"#.into(),
            },
        })
        .unwrap();
        let contract = Contract {
            name: "publish.post".into(),
            module: "publish".into(),
            is_adapter: true,
            side_effect: "external".into(),
            params: Vec::new(),
            returns: cwr_contract::TypeExpr::Dict(Box::new(cwr_contract::TypeExpr::Str)),
            returns_text: "dict[str, str]".into(),
            preconditions: Vec::new(),
            effects: Vec::new(),
            failures: [Code::Unavailable, Code::Invalid, Code::DuplicateKey]
                .into_iter()
                .collect(),
            content_guaranteed: false,
            raw: json!({"idempotency": "key", "idempotency_key": "key", "timeout_ms": 1000}),
        };
        let gateway = AdapterGateway {
            mode: Mode::Replay,
            stubs: BTreeMap::new(),
            cassette: Some(Cassette::load(&path).unwrap()),
            store: KeyStore::open(&dir.join(GATEWAY_DIR)).unwrap(),
            instance: "i-replay-miss".into(),
            adapters_dir: dir.join("adapters"),
            live_modules: BTreeSet::new(),
            adapter_names: vec!["publish.post".into()],
            records: Records(BTreeMap::new()),
            code: None,
            code_error: None,
            ordinals: HashMap::new(),
            evidence: None,
            notes: Vec::new(),
            turn_deadline: None,
            replay_miss: false,
        };
        (gateway, contract)
    }

    fn params(v: Value) -> Map<String, Value> {
        v.as_object().cloned().unwrap()
    }

    #[test]
    fn replay_miss_releases_the_key_and_a_served_call_fills_it() {
        let dir = std::env::temp_dir().join(format!("cwr-replay-miss-{}", std::process::id()));
        let recorded = json!({"key": "k1", "title": "T", "body": "a"});
        let other = json!({"key": "k1", "title": "T", "body": "b"});
        let (mut gw, c) = replay_gateway(&dir, &recorded);
        let ph_other = idempotency::payload_hash(&params(other.clone()), "key");
        let ph_recorded = idempotency::payload_hash(&params(recorded.clone()), "key");
        let key = idempotency::key_of(&params(other.clone()), "key").unwrap();

        // A request the cassette does not hold never reached the service: UNAVAILABLE, nothing
        // stored under the key, and the gateway notes that nothing was sent.
        let miss = gw.call(&c, &params(other.clone()), 1);
        assert!(
            matches!(
                &miss,
                GatewayOutcome::Fail {
                    code: Code::Unavailable,
                    ..
                }
            ),
            "{miss:?}"
        );
        assert_eq!(
            gw.store.lookup("publish.post", &key, &ph_other),
            Lookup::Miss
        );
        assert!(gw.store.pending().is_empty());
        assert!(!gw.notes.last().unwrap().sent);

        // The corrected call under the same key is served from the cassette and stored.
        let served = gw.call(&c, &params(recorded.clone()), 2);
        assert!(
            matches!(&served, GatewayOutcome::Ok(v) if v["post_id"] == "p1"),
            "{served:?}"
        );
        assert!(matches!(
            gw.store.lookup("publish.post", &key, &ph_recorded),
            Lookup::Replay(Stored::Ok(_))
        ));

        // Now the key is consumed: the first payload is a duplicate, answered from the store.
        let duplicate = gw.call(&c, &params(other), 3);
        assert!(
            matches!(
                &duplicate,
                GatewayOutcome::Fail {
                    code: Code::DuplicateKey,
                    ..
                }
            ),
            "{duplicate:?}"
        );
        assert_eq!(gw.notes.last().unwrap().via, "store");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn a_served_failure_still_fills_the_key_in_replay() {
        let dir = std::env::temp_dir().join(format!("cwr-replay-fail-{}", std::process::id()));
        let recorded = json!({"key": "k2", "title": "T", "body": "a"});
        let (mut gw, c) = replay_gateway(&dir, &recorded);
        let path = dir.join("cassette.jsonl");
        // Re-record the single entry as a declared failure the service answered.
        std::fs::remove_file(&path).unwrap();
        let mut k = Cassette::open_for_record(&path, Some("replay-fail")).unwrap();
        k.append(Entry {
            adapter: "publish.post".into(),
            params: recorded.clone(),
            request_hash: request_hash("publish.post", &recorded),
            seq: 0,
            world_time: Some(1),
            response: RawResponse::Timeout,
        })
        .unwrap();
        gw.cassette = Some(Cassette::load(&path).unwrap());
        let mut c = c;
        c.failures.insert(Code::Timeout);
        let key = idempotency::key_of(&params(recorded.clone()), "key").unwrap();
        let ph = idempotency::payload_hash(&params(recorded.clone()), "key");
        let first = gw.call(&c, &params(recorded.clone()), 1);
        assert!(
            matches!(
                &first,
                GatewayOutcome::Fail {
                    code: Code::Timeout,
                    ..
                }
            ),
            "{first:?}"
        );
        assert!(matches!(
            gw.store.lookup("publish.post", &key, &ph),
            Lookup::Replay(Stored::Fail { .. })
        ));
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn modes_and_stub_bindings() {
        assert_eq!("replay".parse::<Mode>().unwrap(), Mode::Replay);
        assert!("retry".parse::<Mode>().is_err());
        let mut cfg = GatewayConfig::default();
        assert!(cfg.add_stub("search=http://127.0.0.1:9").is_ok());
        assert!(cfg.add_stub("search").is_err());
        assert!(cfg.add_stub("search=https://x").is_err());
    }

    #[test]
    fn seal_network_denies_sockets_to_the_process_and_its_descendants() {
        use std::io::Read as _;
        use std::sync::atomic::{AtomicUsize, Ordering};
        use std::sync::Arc;
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        listener.set_nonblocking(true).unwrap();
        let hits = Arc::new(AtomicUsize::new(0));
        // Plain children of a sealed process, with no interpreter guard at all: a socket in the
        // process itself, one through _posixsubprocess.fork_exec (no audit event) and one in a
        // shell-spawned grandchild.
        let probe = format!(
            r#"
import os, socket, subprocess, sys
res = []
for fam in (socket.AF_INET, socket.AF_INET6, socket.AF_UNIX):
    try:
        socket.socket(fam, socket.SOCK_STREAM).close(); res.append("open")
    except PermissionError:
        res.append("EPERM")
c = "import socket\ntry:\n socket.create_connection(('127.0.0.1', {port}), 1); print('connected')\nexcept PermissionError: print('EPERM')"
res.append(subprocess.run([sys.executable, "-c", c], capture_output=True, text=True).stdout.strip())
res.append(subprocess.run(["/bin/sh", "-c", sys.executable + " -c \"$0\"", c], capture_output=True, text=True).stdout.strip())
print(" ".join(res))
"#
        );
        let mut cmd = std::process::Command::new("python3");
        cmd.args(["-I", "-c", &probe])
            .stdout(std::process::Stdio::piped());
        seal_network(&mut cmd).unwrap();
        let mut child = cmd.spawn().unwrap();
        let mut out = String::new();
        let deadline = std::time::Instant::now() + Duration::from_secs(20);
        while child.try_wait().unwrap().is_none() && std::time::Instant::now() < deadline {
            if listener.accept().is_ok() {
                hits.fetch_add(1, Ordering::SeqCst);
            }
            std::thread::sleep(Duration::from_millis(10));
        }
        child
            .stdout
            .take()
            .unwrap()
            .read_to_string(&mut out)
            .unwrap();
        assert_eq!(out.trim(), "EPERM EPERM EPERM EPERM EPERM", "probe output");
        assert_eq!(hits.load(Ordering::SeqCst), 0);
        // The same probe unsealed reaches the listener (the test is not vacuous).
        let out = std::process::Command::new("python3")
            .args(["-I", "-c", &probe])
            .output()
            .unwrap();
        assert!(
            String::from_utf8_lossy(&out.stdout).contains("connected"),
            "{}",
            String::from_utf8_lossy(&out.stdout)
        );
    }
}
