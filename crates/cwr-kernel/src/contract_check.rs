// Copyright Sidiora Labs Inc. CWR® 2026-2027
// SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0

//! The atomic contract check (kernel.md section 4; clauses 1.1, 1.3, 1.6, 2.2, 2.6 and 6.2).
//!
//! Every primitive call that crosses the sandbox boundary is processed by [`Checker::call`] in
//! the fixed order:
//!
//! 1. validate the parameters against the contract's typed `params` (`INVALID`; no tick);
//! 2. evaluate the preconditions in declared order with the precondition language of
//!    world-source.md section 5, calling read_only primitives; the first false one fails with its
//!    declared code, the primitive name and its one-based index (no tick, state unchanged);
//! 3. execute: a primitive runs its Build `lib/` function in the world-code worker against the
//!    Instance state; an adapter goes to the [`Gateway`];
//! 4. in debug mode, assert every declared effect;
//! 5. advance `world_time` by `tick_per_primitive`;
//! 6. append the ledger record (fsynced) together with the new state, and only then answer.
//!
//! A call the Build does not enumerate is refused with `INVALID` before step 1 (closure). The
//! per-turn `primitive_calls` budget is checked before anything else and `adapter_calls` just
//! before step 3; an exceeding call is not executed. A primitive that passes its preconditions and
//! then fails with a code its contract does not declare, raises a Python exception, returns the
//! wrong shape, changes state while read_only, or (debug mode) breaks an effect is recorded as
//! `INVARIANT_VIOLATION` with the defect text and the turn's trace; a mutating defect seals the
//! Instance.
//!
//! World code (`lib/*.py`) runs in [`LibWorker`], a Python 3.12 process in the same kind of
//! sandbox as scripts (see [`crate::sandbox`]) speaking JSON lines; it calls back into the kernel
//! for the facilities the state handle exposes (`random`, `history`, `propose`). Rule logic stays
//! here in Rust: the worker only runs world functions and reports what they did.

use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::io::{BufRead, BufReader, Write};
use std::path::PathBuf;
use std::process::{Child, ChildStdin, ChildStdout};
use std::time::Instant;

use cwr_contract::precondition::{self as pexpr, Arg, CmpOp, Expr, Term};
use cwr_contract::{Code, TypeExpr};
use serde_json::{json, Map, Value};

use crate::envelope::{self, call_error, call_ok, trace_item, Failure};
use crate::ledger::{Kind, Outcome, PreconditionEval, RecordDraft};
use crate::limits::{Budget, Limits};
use crate::rng::Pcg32;
use crate::state::{Build, Instance, WorldState};
use crate::{canonical_json, params_hash, sha256_hex, write_atomic, KernelError, Result};

// ---------------------------------------------------------------------------------------------
// The compiled contract table
// ---------------------------------------------------------------------------------------------

/// One primitive or adapter contract as the kernel enforces it.
#[derive(Debug, Clone)]
pub struct Contract {
    /// Qualified `module.name` of the primitive or adapter.
    pub name: String,
    /// Module that declares the contract.
    pub module: String,
    /// Whether the contract is an external adapter rather than a pure primitive.
    pub is_adapter: bool,
    /// Declared side effect: `read_only`, `mutating` or `external`.
    pub side_effect: String,
    /// Declared parameters in order, each with its parsed type.
    pub params: Vec<(String, TypeExpr)>,
    /// Parsed declared return type every ok result is checked against.
    pub returns: TypeExpr,
    /// Declared return type as written, recorded as the ledger `shape`.
    pub returns_text: String,
    /// (source, parsed, declared code).
    pub preconditions: Vec<(String, Expr, Code)>,
    /// Declared effects (source, parsed), checked after execution in debug mode only.
    pub effects: Vec<(String, Expr)>,
    /// Adapter failures (empty for primitives).
    pub failures: BTreeSet<Code>,
    /// Whether the result content is guaranteed by the world (false for adapter results).
    pub content_guaranteed: bool,
    /// The raw contract object, for the gateway (endpoint, idempotency, timeout_ms, ...).
    pub raw: Value,
}

impl Contract {
    /// Every code the contract can return: precondition codes, INVALID for parameter
    /// validation and, for adapters, the declared failures.
    pub fn declared_codes(&self) -> BTreeSet<Code> {
        let mut s: BTreeSet<Code> = self.preconditions.iter().map(|(_, _, c)| *c).collect();
        s.insert(Code::Invalid);
        s.extend(self.failures.iter().copied());
        s
    }
}

/// Named record types (`record Name` blocks of the manual's types section).
#[derive(Debug, Clone, Default)]
pub struct Records(pub BTreeMap<String, Vec<(String, Option<TypeExpr>)>>);

impl Records {
    /// Parses every `record Name` block: the header line, then indented `field: type` lines.
    pub fn parse(text: &str) -> Records {
        let mut out: BTreeMap<String, Vec<(String, Option<TypeExpr>)>> = BTreeMap::new();
        let mut current: Option<String> = None;
        for line in text.lines() {
            let t = line.trim();
            if let Some(name) = t.strip_prefix("record ") {
                let name = name.trim().to_string();
                if !name.is_empty() && name.chars().all(|c| c.is_ascii_alphanumeric() || c == '_') {
                    out.entry(name.clone()).or_default();
                    current = Some(name);
                    continue;
                }
            }
            if t.is_empty() || !(line.starts_with(' ') || line.starts_with('\t')) {
                current = None;
                continue;
            }
            let Some(rec) = &current else { continue };
            let Some((field, rest)) = t.split_once(':') else {
                continue;
            };
            let field = field.trim();
            if field.is_empty() || !field.chars().all(|c| c.is_ascii_alphanumeric() || c == '_') {
                continue;
            }
            let rest = rest.trim_start();
            let mut depth = 0i32;
            let mut end = rest.len();
            for (i, c) in rest.char_indices() {
                match c {
                    '[' => depth += 1,
                    ']' => depth -= 1,
                    c if c.is_whitespace() && depth == 0 => {
                        end = i;
                        break;
                    }
                    _ => {}
                }
            }
            out.entry(rec.clone())
                .or_default()
                .push((field.to_string(), rest[..end].parse().ok()));
        }
        Records(out)
    }
}

/// Everything the kernel enforces for one Build, read from `contracts.json` and `manual.md`.
#[derive(Debug, Clone)]
pub struct ContractTable {
    /// Every contract of the Build by qualified name.
    pub contracts: BTreeMap<String, Contract>,
    /// Record types declared by the manual, used for shape checks.
    pub records: Records,
    /// Modules of the Build in declared order.
    pub modules: Vec<String>,
    /// The World Source `runtime` the Build was compiled for.
    pub runtime: String,
    /// `lib/<module>.py` files present in the Build, by module.
    pub lib: BTreeMap<String, String>,
}

fn bad(build: &Build, m: impl std::fmt::Display) -> KernelError {
    KernelError::Refused(format!("Build {}: contracts.json: {m}", build.hash))
}

impl ContractTable {
    /// Reads `contracts.json`, `manual.md` and `lib/` of `build`; any malformed contract is [`KernelError::Refused`].
    pub fn load(build: &Build) -> Result<ContractTable> {
        let path = build.dir.join("contracts.json");
        let v: Value = serde_json::from_slice(
            &fs::read(&path)
                .map_err(|e| KernelError::Io(format!("read {}: {e}", path.display())))?,
        )
        .map_err(|e| bad(build, e))?;
        let mut contracts = BTreeMap::new();
        for (list, is_adapter) in [("primitives", false), ("adapters", true)] {
            for c in v.get(list).and_then(Value::as_array).into_iter().flatten() {
                let s = |k: &str| {
                    c.get(k)
                        .and_then(Value::as_str)
                        .map(str::to_string)
                        .ok_or_else(|| bad(build, format!("a {list} entry lacks {k}")))
                };
                let name = s("name")?;
                let mut params = Vec::new();
                for p in c
                    .get("params")
                    .and_then(Value::as_array)
                    .into_iter()
                    .flatten()
                {
                    let (Some(n), Some(t)) = (
                        p.get(0).and_then(Value::as_str),
                        p.get(1).and_then(Value::as_str),
                    ) else {
                        return Err(bad(build, format!("{name}: malformed params")));
                    };
                    params.push((
                        n.to_string(),
                        t.parse::<TypeExpr>()
                            .map_err(|e| bad(build, format!("{name}: param {n}: {e:?}")))?,
                    ));
                }
                let returns_text = s("returns")?;
                let returns = returns_text
                    .parse::<TypeExpr>()
                    .map_err(|e| bad(build, format!("{name}: returns: {e:?}")))?;
                let mut preconditions = Vec::new();
                for pc in c
                    .get("preconditions")
                    .and_then(Value::as_array)
                    .into_iter()
                    .flatten()
                {
                    let (Some(src), Some(code)) = (
                        pc.get(0).and_then(Value::as_str),
                        pc.get(1).and_then(Value::as_str),
                    ) else {
                        return Err(bad(build, format!("{name}: malformed precondition")));
                    };
                    let ast = pexpr::parse(src)
                        .map_err(|e| bad(build, format!("{name}: precondition {src:?}: {e}")))?;
                    let code: Code = code.parse().map_err(|_| {
                        bad(
                            build,
                            format!("{name}: code {code} is outside the envelope"),
                        )
                    })?;
                    preconditions.push((src.to_string(), ast, code));
                }
                let mut effects = Vec::new();
                for e in c
                    .get("effects")
                    .and_then(Value::as_array)
                    .into_iter()
                    .flatten()
                    .filter_map(Value::as_str)
                {
                    effects.push((
                        e.to_string(),
                        pexpr::parse(e)
                            .map_err(|err| bad(build, format!("{name}: effect {e:?}: {err}")))?,
                    ));
                }
                let mut failures = BTreeSet::new();
                for f in c
                    .get("failures")
                    .and_then(Value::as_array)
                    .into_iter()
                    .flatten()
                    .filter_map(Value::as_str)
                {
                    failures.insert(f.parse::<Code>().map_err(|_| {
                        bad(
                            build,
                            format!("{name}: failure {f} is outside the envelope"),
                        )
                    })?);
                }
                let side_effect = s("side_effect")?;
                contracts.insert(
                    name.clone(),
                    Contract {
                        module: c
                            .get("module")
                            .and_then(Value::as_str)
                            .map(str::to_string)
                            .unwrap_or_else(|| name.split('.').next().unwrap_or("").to_string()),
                        is_adapter: is_adapter || side_effect == "external",
                        content_guaranteed: c
                            .get("content_guaranteed")
                            .and_then(Value::as_bool)
                            .unwrap_or(!is_adapter)
                            && side_effect != "external",
                        side_effect,
                        params,
                        returns,
                        returns_text,
                        preconditions,
                        effects,
                        failures,
                        raw: c.clone(),
                        name,
                    },
                );
            }
        }
        let world = v
            .get("world")
            .ok_or_else(|| bad(build, "no world section"))?;
        let modules: Vec<String> = world
            .get("modules")
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
            .filter_map(|m| m.as_str().map(str::to_string))
            .collect();
        let runtime = world
            .get("runtime")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string();
        let manual = fs::read_to_string(build.dir.join("manual.md")).unwrap_or_default();
        let mut lib = BTreeMap::new();
        for m in &modules {
            if build.dir.join("lib").join(format!("{m}.py")).is_file() {
                lib.insert(m.clone(), format!("{}/{m}.py", crate::sandbox::LIB_MOUNT));
            }
        }
        Ok(ContractTable {
            contracts,
            records: Records::parse(&manual),
            modules,
            runtime,
            lib,
        })
    }

    /// The contract named `name`, or `None` when the Build enumerates no such primitive.
    pub fn get(&self, name: &str) -> Option<&Contract> {
        self.contracts.get(name)
    }
}

// ---------------------------------------------------------------------------------------------
// Values in codec form (sdk/python/cwr/_codec.py)
// ---------------------------------------------------------------------------------------------

fn bytes_b64(v: &Value) -> Option<&str> {
    match v {
        Value::Object(m) if m.len() == 1 => m.get("$bytes").and_then(Value::as_str),
        _ => None,
    }
}

fn b64_len(s: &str) -> usize {
    let pad = s.bytes().rev().take_while(|b| *b == b'=').count().min(2);
    (s.len() / 4) * 3 - pad
}

/// Whether a codec value carries a tag the codec does not define (`$invalid`, or any single-`$`
/// key other than a well-formed `$bytes` object).
pub fn has_bad_tag(v: &Value) -> bool {
    match v {
        Value::Array(a) => a.iter().any(has_bad_tag),
        Value::Object(m) => {
            if bytes_b64(v).is_some() {
                return false;
            }
            m.iter()
                .any(|(k, x)| (k.starts_with('$') && !k.starts_with("$$")) || has_bad_tag(x))
        }
        Value::Number(n) => {
            !(n.is_i64() || n.is_u64()) && n.as_f64().map(|f| !f.is_finite()).unwrap_or(true)
        }
        _ => false,
    }
}

/// Python truthiness.
pub fn truthy(v: &Value) -> bool {
    match v {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Number(n) => n.as_f64().map(|f| f != 0.0).unwrap_or(true),
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(m) => match bytes_b64(v) {
            Some(b) => !b.is_empty(),
            None => !m.is_empty(),
        },
    }
}

fn type_name(v: &Value) -> &'static str {
    if bytes_b64(v).is_some() {
        return "bytes";
    }
    match v {
        Value::Null => "None",
        Value::Bool(_) => "bool",
        Value::Number(_) => "int",
        Value::String(_) => "str",
        Value::Array(_) => "list",
        Value::Object(_) => "dict",
    }
}

fn as_int(v: &Value) -> Option<i128> {
    match v {
        Value::Number(n) => n
            .as_i64()
            .map(i128::from)
            .or_else(|| n.as_u64().map(i128::from)),
        _ => None,
    }
}

fn len_of(v: &Value) -> std::result::Result<i64, String> {
    if let Some(b) = bytes_b64(v) {
        return Ok(b64_len(b) as i64);
    }
    match v {
        Value::String(s) => Ok(s.chars().count() as i64),
        Value::Array(a) => Ok(a.len() as i64),
        Value::Object(m) => Ok(m.len() as i64),
        other => Err(format!("len() of a {} value", type_name(other))),
    }
}

fn values_equal(a: &Value, b: &Value) -> bool {
    match (as_int(a), as_int(b)) {
        (Some(x), Some(y)) => x == y,
        _ => a == b,
    }
}

fn compare(op: CmpOp, a: &Value, b: &Value) -> std::result::Result<bool, String> {
    match op {
        CmpOp::Eq => Ok(values_equal(a, b)),
        CmpOp::Ne => Ok(!values_equal(a, b)),
        CmpOp::Lt => Ok(order(a, b)?.is_lt()),
        CmpOp::Le => Ok(order(a, b)?.is_le()),
        CmpOp::Gt => Ok(order(a, b)?.is_gt()),
        CmpOp::Ge => Ok(order(a, b)?.is_ge()),
    }
}

/// Orders two integers numerically or two strings lexically; any other pair is an error.
fn order(a: &Value, b: &Value) -> std::result::Result<std::cmp::Ordering, String> {
    match (as_int(a), as_int(b), a, b) {
        (Some(x), Some(y), _, _) => Ok(x.cmp(&y)),
        (_, _, Value::String(x), Value::String(y)) => Ok(x.cmp(y)),
        _ => Err(format!(
            "cannot order a {} and a {}",
            type_name(a),
            type_name(b)
        )),
    }
}

/// Checks a codec value against a contract type.
pub fn check_shape(v: &Value, ty: &TypeExpr, records: &Records) -> std::result::Result<(), String> {
    let bad = |want: &str| Err(format!("expected {want}, got {}", type_name(v)));
    match ty {
        TypeExpr::Str | TypeExpr::Path => {
            if v.is_string() {
                Ok(())
            } else {
                bad("str")
            }
        }
        TypeExpr::Int => {
            if as_int(v).is_some() {
                Ok(())
            } else {
                bad("int")
            }
        }
        TypeExpr::Bool => {
            if v.is_boolean() {
                Ok(())
            } else {
                bad("bool")
            }
        }
        TypeExpr::Bytes => {
            if bytes_b64(v).is_some() {
                Ok(())
            } else {
                bad("bytes")
            }
        }
        TypeExpr::None => {
            if v.is_null() {
                Ok(())
            } else {
                bad("None")
            }
        }
        TypeExpr::List(t) => match v {
            Value::Array(items) => {
                for (i, x) in items.iter().enumerate() {
                    check_shape(x, t, records).map_err(|e| format!("[{i}]: {e}"))?;
                }
                Ok(())
            }
            _ => bad("list"),
        },
        TypeExpr::Dict(t) => match v {
            Value::Object(m) if bytes_b64(v).is_none() => {
                for (k, x) in m {
                    check_shape(x, t, records).map_err(|e| format!("[{k:?}]: {e}"))?;
                }
                Ok(())
            }
            _ => bad("dict"),
        },
        TypeExpr::Record(name) => {
            let Value::Object(m) = v else {
                return bad(name);
            };
            if bytes_b64(v).is_some() {
                return bad(name);
            }
            let Some(fields) = records.0.get(name) else {
                return Err(format!("record type {name} is not declared in the manual"));
            };
            let want: BTreeSet<&str> = fields.iter().map(|(f, _)| f.as_str()).collect();
            let got: BTreeSet<&str> = m.keys().map(String::as_str).collect();
            if want != got {
                return Err(format!(
                    "record {name} needs exactly the keys {:?}, got {:?}",
                    want, got
                ));
            }
            for (f, t) in fields {
                if let Some(t) = t {
                    check_shape(&m[f], t, records).map_err(|e| format!("{name}.{f}: {e}"))?;
                }
            }
            Ok(())
        }
    }
}

/// Step 1: the call's parameters must be an object with exactly the declared names, each value of
/// its declared type and free of codec tags the codec does not define. Returns the arguments in
/// declared order.
pub fn validate_params(
    c: &Contract,
    params: &Value,
    records: &Records,
) -> std::result::Result<Vec<Value>, String> {
    let Some(obj) = params.as_object() else {
        return Err("parameters must be an object".into());
    };
    let declared: BTreeSet<&str> = c.params.iter().map(|(n, _)| n.as_str()).collect();
    let given: BTreeSet<&str> = obj.keys().map(String::as_str).collect();
    if declared != given {
        let missing: Vec<&&str> = declared.difference(&given).collect();
        let extra: Vec<&&str> = given.difference(&declared).collect();
        return Err(format!(
            "parameters must be exactly {:?}; missing {missing:?}, unknown {extra:?}",
            c.params.iter().map(|(n, _)| n).collect::<Vec<_>>()
        ));
    }
    let mut args = Vec::new();
    for (n, ty) in &c.params {
        let v = &obj[n];
        if has_bad_tag(v) {
            return Err(format!("parameter {n} holds a value outside the SDK codec"));
        }
        check_shape(v, ty, records).map_err(|e| format!("parameter {n}: {e}"))?;
        args.push(v.clone());
    }
    Ok(args)
}

// ---------------------------------------------------------------------------------------------
// The world-code worker
// ---------------------------------------------------------------------------------------------

/// The kernel-side world-code worker. Runs the Build's `lib/` functions; holds a working copy of
/// the Instance state that it commits only for mutating calls that return normally.
pub struct LibWorker {
    child: Child,
    stdin: ChildStdin,
    stdout: BufReader<ChildStdout>,
}

impl LibWorker {
    /// Starts the worker for `build` over the current Instance state.
    pub fn spawn(
        build: &Build,
        table: &ContractTable,
        state: &WorldState,
        seed: u64,
    ) -> Result<LibWorker> {
        let lib_dir: Option<PathBuf> = Some(build.dir.join("lib")).filter(|d| d.is_dir());
        let mut child = crate::sandbox::spawn_worker(lib_dir.as_deref(), WORKER)?;
        let stdin = child
            .stdin
            .take()
            .ok_or_else(|| KernelError::Io("worker stdin".into()))?;
        let stdout = BufReader::new(
            child
                .stdout
                .take()
                .ok_or_else(|| KernelError::Io("worker stdout".into()))?,
        );
        let mut w = LibWorker {
            child,
            stdin,
            stdout,
        };
        let init = json!({"op": "init", "lib": table.lib, "modules": table.modules, "state": state.to_json(), "version": build.version, "seed": seed});
        let r = w
            .request(&init, &mut |_| json!({"error": "no callbacks during init"}))
            .map_err(|e| KernelError::Refused(format!("world-code worker: {e}")))?;
        if let Some(e) = r.get("error").and_then(Value::as_str) {
            return Err(KernelError::Refused(format!("world-code worker: {e}")));
        }
        Ok(w)
    }

    fn send(&mut self, v: &Value) -> std::result::Result<(), String> {
        let mut line = serde_json::to_string(v).map_err(|e| e.to_string())?;
        line.push('\n');
        self.stdin
            .write_all(line.as_bytes())
            .and_then(|_| self.stdin.flush())
            .map_err(|e| format!("world-code worker: {e}"))
    }

    /// One request; callback lines (`{"cb": ...}`) are answered with `cb` until the response.
    pub fn request(
        &mut self,
        req: &Value,
        cb: &mut dyn FnMut(&Value) -> Value,
    ) -> std::result::Result<Value, String> {
        self.send(req)?;
        loop {
            let mut line = String::new();
            let n = self
                .stdout
                .read_line(&mut line)
                .map_err(|e| format!("world-code worker: {e}"))?;
            if n == 0 {
                return Err("world-code worker exited unexpectedly".into());
            }
            let v: Value = serde_json::from_str(&line)
                .map_err(|e| format!("world-code worker sent malformed output: {e}"))?;
            if v.get("cb").is_some() {
                let reply = cb(&v);
                self.send(&reply)?;
                continue;
            }
            if let Some(f) = v.get("fatal").and_then(Value::as_str) {
                return Err(f.to_string());
            }
            return Ok(v);
        }
    }
}

impl Drop for LibWorker {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

// ---------------------------------------------------------------------------------------------
// The adapter gateway seam
// ---------------------------------------------------------------------------------------------

/// What the gateway reports for one external call, before normalisation against the contract.
#[derive(Debug, Clone, PartialEq)]
pub enum GatewayOutcome {
    /// The service answered with this body.
    Ok(Value),
    /// The service failed with an envelope code.
    Fail {
        /// Envelope code of the failure.
        code: Code,
        /// World ticks to wait before retrying (RATE_LIMITED only).
        retry_after: Option<u64>,
        /// Operator-readable explanation of the failure.
        detail: String,
    },
}

/// The adapter gateway (task 2.3 provides the implementation). It is the only network
/// capability of the kernel; the checker calls it at step 3 of an external primitive, after the
/// preconditions passed and the `adapter_calls` budget was taken, and normalises its answer
/// against the contract's declared failures and return shape.
pub trait Gateway {
    /// Performs one external call of `adapter` with `params` at `world_time`; never panics, every failure is an outcome.
    fn call(
        &mut self,
        adapter: &Contract,
        params: &Map<String, Value>,
        world_time: u64,
    ) -> GatewayOutcome;
    /// Ledger evidence of the last call (task 2.3: idempotency class, request and response
    /// hashes, store or service), stamped into its adapter record under `gateway`.
    fn evidence(&mut self) -> Option<Value> {
        None
    }
}

/// The gateway of a kernel without network capability: every external call is `UNAVAILABLE`
/// (every adapter declares it, CWR-F002), the world clock still ticks and no socket is opened.
#[derive(Debug, Default, Clone, Copy)]
pub struct NoGateway;

impl Gateway for NoGateway {
    fn call(
        &mut self,
        adapter: &Contract,
        _params: &Map<String, Value>,
        _world_time: u64,
    ) -> GatewayOutcome {
        GatewayOutcome::Fail {
            code: Code::Unavailable,
            retry_after: None,
            detail: format!(
                "{}: no adapter gateway is configured in this kernel; nothing was sent",
                adapter.name
            ),
        }
    }
}

// ---------------------------------------------------------------------------------------------
// The checker
// ---------------------------------------------------------------------------------------------

/// The answer to one call: the per-call result to send back, and whether the Instance must be
/// sealed (a mutating defect) so the turn ends.
#[derive(Debug, Clone)]
pub struct CallReply {
    /// The per-call result sent back to the script.
    pub result: Value,
    /// Whether a mutating defect sealed the Instance, so the turn must end.
    pub seal: bool,
}

/// The identity of one call as its ledger record carries it: record kind, reported name, params
/// hash, the step-2 precondition evaluations made so far and the instant the call started.
struct CallHead<'n> {
    kind: Kind,
    name: &'n str,
    phash: String,
    evals: Vec<PreconditionEval>,
    started: Instant,
}

impl<'n> CallHead<'n> {
    fn new(
        kind: Kind,
        name: &'n str,
        phash: String,
        evals: Vec<PreconditionEval>,
        started: Instant,
    ) -> Self {
        CallHead {
            kind,
            name,
            phash,
            evals,
            started,
        }
    }
}

struct Env<'e> {
    params: &'e Map<String, Value>,
    result: Option<&'e Value>,
}

/// Per-turn state of the atomic contract check.
pub struct Checker<'a> {
    /// The Instance the turn runs on.
    pub inst: &'a mut Instance,
    /// The worker that executes the Build's `lib/` implementations.
    pub worker: &'a mut LibWorker,
    /// The Build's contracts.
    pub table: &'a ContractTable,
    /// The adapter gateway external calls go through.
    pub gateway: &'a mut dyn Gateway,
    /// The Build's per-turn limits.
    pub limits: Limits,
    /// Counters of the calls taken this turn.
    pub budget: Budget,
    /// Whether declared effects are evaluated after each executed call.
    pub debug_effects: bool,
    /// Number of the turn being executed.
    pub turn: u64,
    /// Trace items of the turn, in order.
    pub trace: Vec<Value>,
    /// Every failure the kernel returned this turn (the script's uncaught exception must be one).
    pub failures: Vec<Failure>,
    /// A mutating or external primitive succeeded this turn.
    pub mutated: bool,
    /// The sealing defect, if any.
    pub sealed: Option<Failure>,
    proposals: Vec<String>,
    /// Proposals committed earlier in this turn; ids continue from here so that every
    /// `world.propose` of a turn gets a distinct id.
    proposed: usize,
    draws: u64,
}

/// The `LedgerRecord` view `world.history` returns (manual record LedgerRecord): exactly the
/// documented keys, empty strings where the ledger holds null.
pub fn ledger_record_view(r: &Value) -> Value {
    let s = |v: Option<&Value>| v.and_then(Value::as_str).unwrap_or("").to_string();
    let o = r.get("outcome").filter(|o| o.is_object());
    let pcs: Vec<Value> = r
        .get("preconditions")
        .and_then(Value::as_array)
        .map(|a| a.iter().map(|p| json!({"index": p.get("index").cloned().unwrap_or(json!(0)), "expr_hash": s(p.get("expr_hash")), "passed": p.get("passed").and_then(Value::as_bool).unwrap_or(false)})).collect())
        .unwrap_or_default();
    json!({
        "seq": r.get("seq").cloned().unwrap_or(json!(0)),
        "turn": r.get("turn").cloned().unwrap_or(json!(0)),
        "world_time": r.get("world_time").cloned().unwrap_or(json!(0)),
        "kind": s(r.get("kind")),
        "primitive": s(r.get("primitive")),
        "params_hash": s(r.get("params_hash")),
        "preconditions": pcs,
        "outcome": {
            "status": s(o.and_then(|o| o.get("status"))),
            "code": s(o.and_then(|o| o.get("code"))),
            "shape": s(o.and_then(|o| o.get("shape"))),
            "side_effect": s(o.and_then(|o| o.get("side_effect"))),
        },
    })
}

fn expr_hash(src: &str) -> String {
    format!("sha256:{}", sha256_hex(src.as_bytes()))
}

/// A name the Build does not enumerate, as recorded: printable and bounded.
fn reported_name(name: &str) -> String {
    let clean: String = name
        .chars()
        .filter(|c| c.is_ascii_graphic())
        .take(120)
        .collect();
    if clean.is_empty() {
        "<unnamed>".into()
    } else {
        clean
    }
}

/// The sealing marker of an Instance (see [`crate::executor`]).
pub const SEAL_FILE: &str = "sealed.json";

impl<'a> Checker<'a> {
    /// A checker for `turn` on `inst` with an empty budget, trace and failure list.
    pub fn new(
        inst: &'a mut Instance,
        worker: &'a mut LibWorker,
        table: &'a ContractTable,
        gateway: &'a mut dyn Gateway,
        limits: Limits,
        debug_effects: bool,
        turn: u64,
    ) -> Self {
        Checker {
            inst,
            worker,
            table,
            gateway,
            limits,
            budget: Budget::default(),
            debug_effects,
            turn,
            trace: Vec::new(),
            failures: Vec::new(),
            mutated: false,
            sealed: None,
            proposals: Vec::new(),
            proposed: 0,
            draws: 0,
        }
    }

    fn limits_record(&self) -> Value {
        let c = self.inst.build.clock;
        self.budget
            .limits_record(&self.limits, c.tick_per_primitive, c.tick_per_turn)
    }

    /// Calls a lib function in the worker, answering its callbacks.
    fn lib_call(
        &mut self,
        name: &str,
        args: Vec<Value>,
        commit: bool,
    ) -> std::result::Result<Value, String> {
        let req = json!({"op": "call", "fn": name, "args": args, "world_time": self.inst.clock.now(), "limits": self.limits_record(), "commit": commit});
        let turn = self.turn;
        let inst = &mut *self.inst;
        let proposals = &mut self.proposals;
        let proposed = self.proposed;
        let draws = &mut self.draws;
        let mut cb = |v: &Value| -> Value {
            match v.get("cb").and_then(Value::as_str) {
                Some("random") => {
                    *draws += 1;
                    json!({"value": inst.rng.next_u32()})
                }
                Some("history") => {
                    let n = v.get("n").and_then(Value::as_u64).unwrap_or(0).min(100_000) as usize;
                    json!({"value": inst.history(n).iter().map(ledger_record_view).collect::<Vec<_>>()})
                }
                Some("propose") => {
                    let text = v
                        .get("text")
                        .and_then(Value::as_str)
                        .unwrap_or("")
                        .to_string();
                    proposals.push(text);
                    json!({"value": format!("p{turn}.{}", proposed + proposals.len())})
                }
                other => json!({"error": format!("unknown kernel facility {other:?}")}),
            }
        };
        self.worker.request(&req, &mut cb)
    }

    /// A read_only call made while evaluating a precondition or an effect.
    fn read_only(&mut self, target: &str, args: Vec<Value>) -> std::result::Result<Value, String> {
        let c = self
            .table
            .get(target)
            .ok_or_else(|| format!("{target} is not a primitive of this Build"))?;
        if c.side_effect != "read_only" || c.is_adapter {
            return Err(format!("{target} is not read_only"));
        }
        let draws_before = self.draws;
        let r = self.lib_call(target, args, false)?;
        if r.get("changed")
            .and_then(Value::as_array)
            .map(|a| !a.is_empty())
            .unwrap_or(false)
            || self.draws != draws_before
        {
            return Err(format!(
                "read_only primitive {target} changed state while evaluating an expression"
            ));
        }
        if let Some(v) = r.get("result") {
            return Ok(v.clone());
        }
        if let Some(c) = r.get("raised").and_then(Value::as_str) {
            return Err(format!("{target} raised {c}"));
        }
        if let Some(e) = r.get("exception").and_then(Value::as_str) {
            return Err(format!("{target} raised a Python exception: {e}"));
        }
        Err(format!("{target} has no lib/ implementation"))
    }

    fn eval_expr(&mut self, e: &Expr, env: &Env) -> std::result::Result<Value, String> {
        Ok(match e {
            Expr::Or(a, b) => {
                Value::Bool(truthy(&self.eval_expr(a, env)?) || truthy(&self.eval_expr(b, env)?))
            }
            Expr::And(a, b) => {
                Value::Bool(truthy(&self.eval_expr(a, env)?) && truthy(&self.eval_expr(b, env)?))
            }
            Expr::Not(a) => Value::Bool(!truthy(&self.eval_expr(a, env)?)),
            Expr::Compare(op, a, b) => {
                let x = self.eval_term(a, env)?;
                let y = self.eval_term(b, env)?;
                Value::Bool(compare(*op, &x, &y)?)
            }
            Expr::Term(t) => self.eval_term(t, env)?,
        })
    }

    fn name_value(name: &str, env: &Env) -> std::result::Result<Value, String> {
        if let Some(v) = env.params.get(name) {
            return Ok(v.clone());
        }
        if name == "result" {
            if let Some(r) = env.result {
                return Ok(r.clone());
            }
        }
        Err(format!("name {name} is not a parameter"))
    }

    fn eval_term(&mut self, t: &Term, env: &Env) -> std::result::Result<Value, String> {
        Ok(match t {
            Term::Name(n) => Self::name_value(n, env)?,
            Term::Int(i) => json!(i),
            Term::Str(s) => Value::String(s.clone()),
            Term::Call { target, args } => {
                let mut vals = Vec::new();
                for a in args {
                    vals.push(match a {
                        Arg::Name(n) => Self::name_value(n, env)?,
                        Arg::Int(i) => json!(i),
                        Arg::Str(s) => Value::String(s.clone()),
                    });
                }
                self.read_only(target, vals)?
            }
            Term::Len(x) => json!(len_of(&self.eval_term(x, env)?)?),
            Term::Add(a, b) => {
                let (x, y) = (self.eval_term(a, env)?, self.eval_term(b, env)?);
                match (as_int(&x), as_int(&y)) {
                    (Some(p), Some(q)) => {
                        json!(i64::try_from(p + q).map_err(|_| "integer overflow".to_string())?)
                    }
                    _ => {
                        return Err(format!(
                            "cannot add a {} and a {}",
                            type_name(&x),
                            type_name(&y)
                        ))
                    }
                }
            }
            Term::Group(e) => self.eval_expr(e, env)?,
        })
    }

    fn record(
        &mut self,
        head: CallHead<'_>,
        outcome: Outcome,
        extra: Vec<(&str, Value)>,
        new_state: Option<WorldState>,
    ) -> Result<Value> {
        let mut d = RecordDraft::new(head.kind, self.turn, self.inst.clock.now());
        d.primitive = Some(head.name.to_string());
        d.params_hash = Some(head.phash);
        d.preconditions = head.evals;
        d.outcome = Some(outcome);
        for (k, v) in extra {
            d.extra.insert(k.to_string(), v);
        }
        d.duration_us = head.started.elapsed().as_micros() as u64;
        self.inst.append(d, new_state)
    }

    /// A call refused before execution (closure, budget, step 1 or step 2): state unchanged, no
    /// tick, one ledger record.
    fn refuse(
        &mut self,
        head: CallHead<'_>,
        f: Failure,
        shape: Option<String>,
        side: Option<String>,
        cg: bool,
    ) -> Result<CallReply> {
        let name = head.name;
        let outcome = Outcome {
            ok: false,
            code: Some(f.code),
            shape,
            side_effect: side,
        };
        let mut extra = Vec::new();
        if let Some(l) = &f.limit {
            extra.push(("limit", json!(l)));
        }
        if f.code == Code::InvariantViolation {
            extra.push(("defect", json!(f.detail)));
            extra.push(("trace", Value::Array(self.trace.clone())));
        }
        let rec = self.record(head, outcome, extra, None)?;
        let wt = rec["world_time"].as_u64().unwrap_or(0);
        self.trace.push(trace_item(name, Some(f.code), wt, cg));
        self.failures.push(f.clone());
        Ok(CallReply {
            result: call_error(&f, cg),
            seal: false,
        })
    }

    /// A closure refusal reported by the runner: recorded as `INVALID` under the facility name.
    pub fn closure(&mut self, facility: &str, detail: &str) -> Result<CallReply> {
        let started = Instant::now();
        self.budget.take_call(&self.limits);
        let fac = if envelope::CLOSURE_FACILITIES.contains(&facility) {
            facility
        } else {
            "sandbox.interpreter"
        };
        let f = Failure::new(
            Code::Invalid,
            fac,
            format!("closure: {detail} is not enumerated by this Build and is refused"),
        );
        let phash = params_hash(&json!({"detail": detail}));
        let outcome = Outcome {
            ok: false,
            code: Some(Code::Invalid),
            shape: None,
            side_effect: None,
        };
        let rec = self.record(
            CallHead::new(Kind::Primitive, fac, phash, Vec::new(), started),
            outcome,
            vec![("closure", json!(detail))],
            None,
        )?;
        let wt = rec["world_time"].as_u64().unwrap_or(0);
        self.trace
            .push(trace_item(fac, Some(Code::Invalid), wt, true));
        self.failures.push(f.clone());
        Ok(CallReply {
            result: call_error(&f, true),
            seal: false,
        })
    }

    /// One primitive call through the six steps.
    pub fn call(&mut self, name: &str, params: &Value) -> Result<CallReply> {
        let started = Instant::now();
        let within = self.budget.take_call(&self.limits);
        let table = self.table;
        let contract = table.get(name);
        let phash = params_hash(params);
        let rname = if contract.is_some() {
            name.to_string()
        } else {
            reported_name(name)
        };
        let kind = if contract.map(|c| c.is_adapter).unwrap_or(false) {
            Kind::Adapter
        } else {
            Kind::Primitive
        };
        let shape = contract.map(|c| c.returns_text.clone());
        let side = contract.map(|c| c.side_effect.clone());
        let cg = contract.map(|c| c.content_guaranteed).unwrap_or(true);
        if !within {
            let f = Failure::limit(
                &rname,
                "primitive_calls",
                format!(
                    "the turn budget of {} primitive calls is spent; this call was not executed",
                    self.limits.primitive_calls
                ),
            );
            return self.refuse(
                CallHead::new(kind, &rname, phash, Vec::new(), started),
                f,
                shape,
                side,
                cg,
            );
        }
        let Some(c) = contract else {
            let f = Failure::new(
                Code::Invalid,
                &rname,
                "closure: this Build enumerates no such primitive or adapter",
            );
            return self.refuse(
                CallHead::new(kind, &rname, phash, Vec::new(), started),
                f,
                None,
                None,
                true,
            );
        };
        // Step 1: parameters.
        let args = match validate_params(c, params, &table.records) {
            Ok(a) => a,
            Err(why) => {
                return self.refuse(
                    CallHead::new(kind, name, phash, Vec::new(), started),
                    Failure::new(Code::Invalid, name, why),
                    shape,
                    side,
                    cg,
                )
            }
        };
        let pmap = params.as_object().cloned().unwrap_or_default();
        // Step 2: preconditions in declared order.
        let mut evals = Vec::new();
        for (i, (src, ast, code)) in c.preconditions.iter().enumerate() {
            let env = Env {
                params: &pmap,
                result: None,
            };
            match self.eval_expr(ast, &env) {
                Ok(v) => {
                    let passed = truthy(&v);
                    evals.push(PreconditionEval {
                        index: (i + 1) as u32,
                        expr_hash: expr_hash(src),
                        passed,
                    });
                    if !passed {
                        let f = Failure::precondition(
                            *code,
                            name,
                            i + 1,
                            format!("precondition {} `{src}` was false", i + 1),
                        );
                        return self.refuse(
                            CallHead::new(kind, name, phash, evals, started),
                            f,
                            shape,
                            side,
                            cg,
                        );
                    }
                }
                Err(m) => {
                    let f = Failure::new(
                        Code::InvariantViolation,
                        name,
                        format!("precondition {} `{src}` could not be evaluated: {m}", i + 1),
                    );
                    return self.refuse(
                        CallHead::new(kind, name, phash, evals, started),
                        f,
                        shape,
                        side,
                        cg,
                    );
                }
            }
        }
        if c.is_adapter {
            if !self.budget.adapter_fits(&self.limits) {
                let f = Failure::limit(
                    name,
                    "adapter_calls",
                    format!(
                        "the turn budget of {} adapter calls is spent; this call was not executed",
                        self.limits.adapter_calls
                    ),
                );
                return self.refuse(
                    CallHead::new(kind, name, phash, evals, started),
                    f,
                    shape,
                    side,
                    cg,
                );
            }
            self.budget.take_adapter();
        }
        // Step 3: execute.
        let rng_before = self.inst.rng.state();
        self.proposals.clear();
        self.draws = 0;
        let declared = c.declared_codes();
        let mutating = c.side_effect == "mutating";
        let mut new_modules: Option<Map<String, Value>> = None;
        let mut defect: Option<String> = None;
        let mut fail: Option<Failure> = None;
        let mut value = Value::Null;
        if c.is_adapter {
            match self.gateway.call(c, &pmap, self.inst.clock.now()) {
                GatewayOutcome::Ok(v) => match check_shape(&v, &c.returns, &table.records) {
                    Ok(()) if !has_bad_tag(&v) => value = v,
                    _ if c.failures.contains(&Code::Invalid) => fail = Some(Failure::new(Code::Invalid, name, "the service answered with a body that does not match the declared return shape")),
                    _ => defect = Some(format!("{name}: body of the wrong shape and INVALID is not among its declared failures")),
                },
                GatewayOutcome::Fail { code, retry_after, detail } => {
                    if !c.failures.contains(&code) {
                        defect = Some(format!("{name}: the gateway produced {}, which the adapter contract does not declare", code.as_str()));
                    } else if code == Code::RateLimited && retry_after.is_none() {
                        defect = Some(format!("{name}: RATE_LIMITED without retry_after in ticks"));
                    } else {
                        fail = Some(Failure { retry_after, ..Failure::new(code, name, detail) });
                    }
                }
            }
        } else {
            match self.lib_call(name, args, mutating) {
                Err(m) => defect = Some(format!("{name}: {m}")),
                Ok(r) => {
                    if let Some(v) = r.get("result") {
                        let changed = r
                            .get("changed")
                            .and_then(Value::as_array)
                            .map(|a| !a.is_empty())
                            .unwrap_or(false);
                        if !mutating && (changed || self.draws > 0) {
                            defect =
                                Some(format!("read_only primitive {name} changed Instance state"));
                        } else if let Err(e) = check_shape(v, &c.returns, &table.records) {
                            defect = Some(format!("{name} returned a value that does not match its declared return type {}: {e}", c.returns_text));
                        } else {
                            value = v.clone();
                            new_modules = r.get("state").and_then(Value::as_object).cloned();
                        }
                    } else if let Some(code) = r.get("raised").and_then(Value::as_str) {
                        let detail = r.get("detail").and_then(Value::as_str).unwrap_or("");
                        match code.parse::<Code>() {
                            Err(_) => defect = Some(format!("{name} raised {code:?}, which is outside the eleven-code failure envelope {detail}")),
                            Ok(k) if !declared.contains(&k) => defect = Some(format!("{name} emitted {code} after its preconditions passed; its contract does not declare {code}")),
                            Ok(k) if mutating => defect = Some(format!("mutating primitive {name} failed with {} in its implementation after its preconditions passed", k.as_str())),
                            Ok(k) => fail = Some(Failure::new(k, name, detail.to_string())),
                        }
                    } else if let Some(e) = r.get("exception").and_then(Value::as_str) {
                        defect = Some(format!("a Python exception escaped lib/ in {name}: {e}"));
                    } else {
                        defect = Some(format!("{name} has no lib/ implementation in this Build"));
                    }
                }
            }
        }
        // Step 4: effects (debug mode).
        if defect.is_none() && fail.is_none() && self.debug_effects && !c.effects.is_empty() {
            for (src, ast) in &c.effects {
                let env = Env {
                    params: &pmap,
                    result: Some(&value),
                };
                match self.eval_expr(ast, &env) {
                    Ok(v) if truthy(&v) => {}
                    Ok(_) => {
                        defect = Some(format!(
                            "effect `{src}` of {name} does not hold after an ok outcome"
                        ));
                        break;
                    }
                    Err(m) => {
                        defect = Some(format!(
                            "effect `{src}` of {name} could not be evaluated: {m}"
                        ));
                        break;
                    }
                }
            }
        }
        // Only a mutating primitive can leave a defect behind in state (step 3 or step 4), so only
        // that seals the Instance; a read_only or gateway defect changed nothing.
        let seal = defect.is_some() && mutating;
        if let Some(d) = &defect {
            fail = Some(Failure::new(Code::InvariantViolation, name, d.clone()));
        }
        // Step 5: the executed call ticks.
        self.inst.clock.advance_primitive()?;
        // Step 6: ledger record, committed together with the new state.
        let ok = fail.is_none();
        let mut new_state = None;
        if ok {
            if let Some(mods) = new_modules.filter(|m| !m.is_empty()) {
                let mut s = self.inst.state.clone();
                for (m, v) in mods {
                    s.set(&m, v)?;
                }
                new_state = Some(s);
            }
        } else {
            self.inst.rng = Pcg32::from_state(rng_before);
        }
        let outcome = match &fail {
            None => Outcome::ok(&c.returns_text, &c.side_effect),
            Some(f) => Outcome::error(f.code, &c.returns_text, &c.side_effect),
        };
        let mut extra = Vec::new();
        if let Some(f) = &fail {
            if f.code == Code::InvariantViolation {
                extra.push(("defect", json!(f.detail)));
                extra.push(("trace", Value::Array(self.trace.clone())));
                extra.push(("sealed", json!(seal)));
            }
            if let Some(ra) = f.retry_after {
                extra.push(("retry_after", json!(ra)));
            }
        }
        if c.is_adapter {
            extra.push(("content_guaranteed", json!(false)));
            if let Some(ev) = self.gateway.evidence() {
                extra.push(("gateway", ev));
            }
        }
        let rec = self.record(
            CallHead::new(kind, name, phash, evals, started),
            outcome,
            extra,
            new_state,
        )?;
        let wt = rec["world_time"].as_u64().unwrap_or(0);
        self.trace.push(trace_item(
            name,
            fail.as_ref().map(|f| f.code),
            wt,
            c.content_guaranteed,
        ));
        let Some(f) = fail else {
            if c.side_effect != "read_only" {
                self.mutated = true;
            }
            let proposals = std::mem::take(&mut self.proposals);
            for text in &proposals {
                self.proposed += 1;
                self.write_proposal(self.proposed, text)?;
            }
            return Ok(CallReply {
                result: call_ok(value, c.content_guaranteed),
                seal: false,
            });
        };
        self.failures.push(f.clone());
        if seal {
            let marker = json!({"seq": rec["seq"], "turn": self.turn, "primitive": name, "defect": f.detail, "build": self.inst.build.hash});
            write_atomic(
                &self.inst.dir.join(SEAL_FILE),
                canonical_json(&marker).as_bytes(),
            )?;
            self.sealed = Some(f.clone());
        }
        Ok(CallReply {
            result: call_error(&f, c.content_guaranteed),
            seal,
        })
    }

    /// `world.propose`: a `proposal` record plus one line in the outbox file outside the Build
    /// and the state store (kernel.md section 11). Nothing in the kernel reads the outbox.
    fn write_proposal(&mut self, index: usize, text: &str) -> Result<()> {
        let id = format!("p{}.{index}", self.turn);
        let d = RecordDraft::new(Kind::Proposal, self.turn, self.inst.clock.now())
            .with("id", json!(id))
            .with(
                "text_hash",
                json!(format!("sha256:{}", sha256_hex(text.as_bytes()))),
            );
        self.inst.append(d, None)?;
        crate::outbox::append(&self.inst.dir, &id, self.turn, text)
    }
}

/// The world-code worker: executes `lib/` functions for the kernel. Python 3.12, standard
/// library only; started with `-P -s -S -B` inside the worker sandbox.
pub const WORKER: &str = r#####"
import base64, copy, importlib.util, json, keyword, signal, sys, types

PROTO_IN = sys.stdin
PROTO = sys.stdout
sys.stdout = sys.stderr
STEP_SECONDS = 20

def out(obj):
    PROTO.write(json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n")
    PROTO.flush()

def read():
    line = PROTO_IN.readline()
    if not line:
        sys.exit(0)
    return json.loads(line)

def enc(v):
    if v is None or isinstance(v, (bool, str)):
        return v
    if isinstance(v, int):
        return int(v)
    if isinstance(v, (bytes, bytearray)):
        return {"$bytes": base64.b64encode(bytes(v)).decode("ascii")}
    if isinstance(v, (list, tuple)):
        return [enc(x) for x in v]
    if isinstance(v, dict):
        if not all(isinstance(k, str) for k in v):
            raise TypeError("dict with non-str keys is outside the codec")
        return {("$" + k if k.startswith("$") else k): enc(x) for k, x in v.items()}
    raise TypeError("value of type %s is outside the codec" % type(v).__name__)

def dec(v):
    if isinstance(v, list):
        return [dec(x) for x in v]
    if isinstance(v, dict):
        if len(v) == 1 and "$bytes" in v and isinstance(v["$bytes"], str):
            return base64.b64decode(v["$bytes"])
        return {(k[1:] if k.startswith("$$") else k): dec(x) for k, x in v.items()}
    return v

class Failure(Exception):
    def __init__(self, code, detail=""):
        super().__init__("%s: %s" % (code, detail) if detail else str(code))
        self.code = code
        self.detail = detail

class StepTimeout(Exception):
    pass

def _alarm(signum, frame):
    raise StepTimeout("world code exceeded %d s of host time" % STEP_SECONDS)

signal.signal(signal.SIGALRM, _alarm)

def callback(name, **kw):
    msg = {"cb": name}
    msg.update(kw)
    out(msg)
    r = read()
    if "error" in r:
        raise RuntimeError(r["error"])
    return dec(r["value"])

class G:
    lib = {}
    state = {}
    work = {}
    world_time = 0
    limits = {}
    version = ""
    seed = 0

class Handle:
    def module(self, name):
        return G.work[name]
    def world_time(self):
        return G.world_time
    def random(self):
        return callback("random")
    def seed(self):
        return G.seed
    def build_version(self):
        return G.version
    def limits(self):
        return dict(G.limits)
    def history(self, n):
        return callback("history", n=int(n))
    def propose(self, text):
        return callback("propose", text=str(text))

SHIM = types.ModuleType("cwr_state")
SHIM.Failure = Failure
SHIM.world_time = lambda: G.world_time
SHIM.random = lambda: callback("random")
sys.modules["cwr_state"] = SHIM

def pyname(member):
    return member + "_" if keyword.iskeyword(member) else member

def load(path, tag):
    spec = importlib.util.spec_from_file_location("_cwr_lib_" + tag, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

def describe(e):
    text = "%s: %s" % (type(e).__name__, e)
    return text if len(text) <= 400 else text[:400] + "..."

def op_init(req):
    try:
        G.lib = {m: load(p, m) for m, p in sorted(req["lib"].items())}
    except Exception as e:
        return {"error": "world code failed to load: " + describe(e)}
    state = {}
    for m in req["modules"]:
        base = copy.deepcopy(getattr(G.lib[m], "INITIAL_STATE", {})) if m in G.lib else {}
        stored = dec(req["state"].get(m, {}))
        if isinstance(base, dict) and isinstance(stored, dict):
            base.update(stored)
        else:
            base = stored
        state[m] = base
    G.state = state
    G.work = state
    G.version = req["version"]
    G.seed = req["seed"]
    return {"python": "%d.%d.%d" % sys.version_info[:3]}

def op_call(req):
    name = req["fn"]
    module, _, member = name.partition(".")
    fn = getattr(G.lib.get(module), pyname(member), None)
    if fn is None:
        return {"missing": True}
    G.world_time = req["world_time"]
    G.limits = req["limits"]
    work = copy.deepcopy(G.state)
    G.work = work
    signal.alarm(STEP_SECONDS)
    try:
        result = fn(Handle(), *[dec(a) for a in req["args"]])
    except Failure as e:
        G.work = G.state
        return {"raised": str(e.code), "detail": str(e.detail)}
    except Exception as e:
        G.work = G.state
        code = getattr(e, "code", None)
        if isinstance(code, str):
            return {"raised": code, "detail": describe(e)}
        return {"exception": describe(e)}
    finally:
        signal.alarm(0)
    changed = sorted(m for m in work if work[m] != G.state.get(m))
    try:
        encoded = enc(result)
        state = {m: enc(work[m]) for m in changed} if (req.get("commit") and changed) else None
    except TypeError as e:
        G.work = G.state
        return {"exception": describe(e), "changed": changed}
    resp = {"result": encoded, "changed": changed}
    if state is not None:
        G.state = work
        resp["state"] = state
    G.work = G.state
    return resp

OPS = {"init": op_init, "call": op_call}

while True:
    try:
        req = read()
        out(OPS[req["op"]](req))
    except SystemExit:
        raise
    except Exception as e:
        out({"fatal": "world-code worker: " + describe(e)})
"#####;

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn records_from_manual_text() {
        let r = Records::parse("```text\nrecord Limits\n  cpu_ms: int\n  remaining: int    calls left\n\nrecord Hit\n  tags: list[str]  content\n```\n");
        assert_eq!(r.0["Limits"].len(), 2);
        assert_eq!(
            r.0["Hit"][0].1,
            Some(TypeExpr::List(Box::new(TypeExpr::Str)))
        );
        assert!(check_shape(
            &json!({"cpu_ms": 1, "remaining": 2}),
            &TypeExpr::Record("Limits".into()),
            &r
        )
        .is_ok());
        assert!(check_shape(
            &json!({"cpu_ms": 1}),
            &TypeExpr::Record("Limits".into()),
            &r
        )
        .is_err());
    }

    #[test]
    fn codec_tags_and_values() {
        assert!(has_bad_tag(&json!({"$invalid": "set"})));
        assert!(has_bad_tag(&json!([1, {"a": {"$x": 1}}])));
        assert!(!has_bad_tag(&json!({"$bytes": "AAE="})));
        assert!(!has_bad_tag(&json!({"$$x": 1})));
        assert!(
            !has_bad_tag(&json!(1.5)),
            "finite floats are codec values; types refuse them"
        );
        assert_eq!(len_of(&json!({"$bytes": "AAE="})).unwrap(), 2);
        assert!(check_shape(&json!(1.5), &TypeExpr::Int, &Records::default()).is_err());
        assert!(check_shape(&json!(true), &TypeExpr::Int, &Records::default()).is_err());
        assert!(compare(CmpOp::Le, &json!(3), &json!(3)).unwrap());
        assert!(truthy(&json!({"$bytes": "AA=="})));
    }

    #[test]
    fn ledger_view_has_exactly_the_documented_keys() {
        let v = ledger_record_view(
            &json!({"seq": 0, "turn": 0, "world_time": 0, "kind": "header", "primitive": null, "params_hash": null, "preconditions": [], "outcome": null, "seed": 3, "chain": "x"}),
        );
        let keys: BTreeSet<&str> = v.as_object().unwrap().keys().map(String::as_str).collect();
        assert_eq!(
            keys,
            [
                "kind",
                "outcome",
                "params_hash",
                "preconditions",
                "primitive",
                "seq",
                "turn",
                "world_time"
            ]
            .into_iter()
            .collect()
        );
        assert_eq!(v["primitive"], json!(""));
    }
}
