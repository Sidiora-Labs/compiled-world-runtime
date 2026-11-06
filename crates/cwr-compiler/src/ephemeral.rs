// Copyright Sidiora Labs Inc. CWR® 2026-2027
// SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0

//! The ephemeral Instance used by contract tests (compiler.md section 4, clauses 1.6 and 4.2).
//!
//! It is created and discarded inside the compiler process and runs the atomic contract check
//! of kernel.md section 4 against the World Source's own `lib/` and `adapters/*/live.py`:
//!
//! 1. validate parameters against the contract's typed `params` (`INVALID`, no precondition);
//! 2. evaluate preconditions in declared order with the precondition language of
//!    world-source.md section 5, calling read_only primitives; the first false one fails with
//!    its declared code and one-based index;
//! 3. execute: a primitive runs its `lib/` function against the Instance state through the
//!    kernel state handle; an adapter runs its `live.py` function through a replay-only gateway
//!    over the fixture's cassette entries (no network connection is ever opened);
//! 4. assert every declared effect after ok (always, not only in debug mode);
//! 5. advance world time by `tick_per_primitive`;
//! 6. append an in-memory ledger record (the shape of manual `LedgerRecord`).
//!
//! Rule logic (parameter validation, precondition order, effects, the closed envelope, shape
//! checks, the clock and the RNG draws) lives here in Rust; world code runs in one Python 3.12
//! worker process per suite run, spoken to over JSON lines on stdin/stdout. The worker holds
//! the Instance state, implements the state handle methods the refworld convention defines
//! (`module(name)`, `world_time()`, `random()`, `seed()`, `build_version()`, `limits()`,
//! `history(n)`, `propose(text)`) and a `cwr_state` module with `Failure(code, detail)` for
//! world code that raises an envelope code. Module state is committed only when a `lib/`
//! function returns normally. RNG draws come from the kernel's PCG32 stream
//! ([`cwr_kernel::Pcg32`]) seeded from the fixture's `seed` (default 0). Bytes travel as
//! `{"$bytes": "<base64>"}`. The worker is started with `-I -B` (isolated mode: no `PYTHON*`
//! variables, no user site, no script directory on the path) and never writes bytecode into
//! the World Source. World code executed by contract tests has no network capability (clause
//! 7.1; compiler.md: a compiler never opens a network connection), in two layers: where
//! bubblewrap is installed the worker runs in its own empty network namespace (the host
//! filesystem view is unchanged), so there is no route to any host; and in every case the worker
//! installs a Python audit hook before any world code loads that refuses socket creation and
//! name resolution, ctypes, subprocesses, exec/fork/spawn, importing the modules that start
//! processes or interpreters without raising an audit event (`_posixsubprocess`, the
//! subinterpreter and test-API modules) and writing native extension files. On a host without
//! bubblewrap the audit hook is not the only layer: the worker is started under
//! [`cwr_kernel::gateway::seal_network`], a seccomp filter inherited by every descendant under
//! which `socket(2)` and the other network paths fail, so a process that slips past the hook
//! still has no network.
//!
//! Outcomes that are defects (an undeclared code after the preconditions passed, any code from
//! a mutating primitive's implementation, a Python exception escaping world code, an effect
//! that does not hold, a read_only primitive that changed state, a return value of the wrong
//! shape) are reported as `INVARIANT_VIOLATION` with the defect text.

use std::cmp::Ordering;
use std::collections::{BTreeMap, BTreeSet};
use std::io::{BufRead, BufReader, Write};
use std::path::Path;
use std::process::{Child, ChildStdin, ChildStdout, Command, Stdio};

use cwr_contract::precondition::{Arg, CmpOp, Term};
use cwr_contract::{Adapter, Code, Expr, Expression, Primitive, TypeExpr};
use serde_json::{json, Map, Value};

/// Named record types from `manual/types.md`: record name to ordered `(field, type)` pairs.
/// A field whose type does not parse is recorded with `None` and accepts any value.
#[derive(Debug, Clone, Default)]
pub struct Records(pub BTreeMap<String, Vec<(String, Option<TypeExpr>, String)>>);

impl Records {
    /// Parses the `record Name` blocks of `manual/types.md`.
    pub fn parse(text: &str) -> Records {
        let mut out: BTreeMap<String, Vec<(String, Option<TypeExpr>, String)>> = BTreeMap::new();
        let mut current: Option<String> = None;
        for line in text.lines() {
            let t = line.trim();
            if let Some(name) = t.strip_prefix("record ") {
                let name = name.trim().to_string();
                out.entry(name.clone()).or_default();
                current = Some(name);
                continue;
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
            let ty = rest[..end].to_string();
            out.entry(rec.clone())
                .or_default()
                .push((field.to_string(), ty.parse().ok(), ty));
        }
        Records(out)
    }

    fn to_value(&self) -> Value {
        let mut m = Map::new();
        for (name, fields) in &self.0 {
            let mut f = Map::new();
            for (k, _, t) in fields {
                f.insert(k.clone(), Value::String(t.clone()));
            }
            m.insert(name.clone(), Value::Object(f));
        }
        Value::Object(m)
    }
}

// ---------------------------------------------------------------------------------------------
// Values
// ---------------------------------------------------------------------------------------------

fn bytes_b64(v: &Value) -> Option<&str> {
    match v {
        Value::Object(m) if m.len() == 1 => m.get("$bytes").and_then(Value::as_str),
        _ => None,
    }
}

fn b64_len(s: &str) -> usize {
    let pad = s.bytes().rev().take_while(|b| *b == b'=').count();
    ((s.len() / 4) * 3).saturating_sub(pad.min(2))
}

/// Python truthiness of a value.
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

fn len_of(v: &Value) -> Result<i64, String> {
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

fn compare(op: CmpOp, a: &Value, b: &Value) -> Result<bool, String> {
    match op {
        CmpOp::Eq => Ok(values_equal(a, b)),
        CmpOp::Ne => Ok(!values_equal(a, b)),
        CmpOp::Lt => order(a, b).map(Ordering::is_lt),
        CmpOp::Le => order(a, b).map(Ordering::is_le),
        CmpOp::Gt => order(a, b).map(Ordering::is_gt),
        CmpOp::Ge => order(a, b).map(Ordering::is_ge),
    }
}

/// Python ordering of two ints or two strings; any other pair is a TypeError message.
fn order(a: &Value, b: &Value) -> Result<Ordering, String> {
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

fn values_equal(a: &Value, b: &Value) -> bool {
    match (as_int(a), as_int(b)) {
        (Some(x), Some(y)) => x == y,
        _ => a == b,
    }
}

/// Checks a tagged value against a contract type.
pub fn check_shape(v: &Value, ty: &TypeExpr, records: &Records) -> Result<(), String> {
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
                return Ok(());
            };
            let want: BTreeSet<&str> = fields.iter().map(|(f, _, _)| f.as_str()).collect();
            let got: BTreeSet<&str> = m.keys().map(String::as_str).collect();
            if want != got {
                return Err(format!(
                    "record {name} needs exactly the keys {:?}, got {:?}",
                    want.iter().collect::<Vec<_>>(),
                    got.iter().collect::<Vec<_>>()
                ));
            }
            for (f, t, _) in fields {
                if let Some(t) = t {
                    check_shape(&m[f], t, records).map_err(|e| format!("{name}.{f}: {e}"))?;
                }
            }
            Ok(())
        }
    }
}

// ---------------------------------------------------------------------------------------------
// Worker process
// ---------------------------------------------------------------------------------------------

/// The Python worker that executes world code for one suite run.
pub struct Worker {
    child: Child,
    stdin: ChildStdin,
    stdout: BufReader<ChildStdout>,
}

/// The interpreter: `CWR_PYTHON` if set, else `python3`.
pub fn python() -> String {
    std::env::var("CWR_PYTHON")
        .ok()
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| "python3".to_string())
}

impl Worker {
    /// Starts a worker over a World Source root with the given `lib/` and `live.py` files.
    pub fn spawn(
        root: &Path,
        lib: &BTreeMap<String, String>,
        live: &BTreeMap<String, String>,
        records: &Records,
        version: &str,
    ) -> Result<Worker, String> {
        let root = std::fs::canonicalize(root)
            .map_err(|e| format!("cannot resolve {}: {e}", root.display()))?;
        // No network capability for world code (clause 7.1): a fresh, empty network namespace
        // where bubblewrap exists, else the kernel's seccomp network seal (fail closed if it
        // cannot be applied); the worker's audit hook (see WORKER) applies in every case.
        let mut cmd = match cwr_kernel::sandbox::bwrap() {
            Ok(bwrap) => {
                let mut c = Command::new(bwrap);
                c.args([
                    "--unshare-user",
                    "--unshare-net",
                    "--unshare-ipc",
                    "--die-with-parent",
                    "--new-session",
                    "--bind",
                    "/",
                    "/",
                    "--dev",
                    "/dev",
                    "--chdir",
                ])
                .arg(&root)
                .arg("--")
                .arg(python());
                c
            }
            Err(_) => {
                let mut c = Command::new(python());
                cwr_kernel::gateway::seal_network(&mut c).map_err(|e| {
                    format!("cannot start the contract-test worker without bubblewrap: {e}")
                })?;
                c
            }
        };
        let mut child = cmd
            .args(["-I", "-B", "-c", WORKER])
            .current_dir(&root)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()
            .map_err(|e| format!("cannot start the contract-test worker {}: {e}", python()))?;
        let (Some(stdin), Some(stdout)) = (child.stdin.take(), child.stdout.take()) else {
            let _ = child.kill();
            let _ = child.wait();
            return Err("cannot open the contract-test worker's stdin and stdout pipes".into());
        };
        let stdout = BufReader::new(stdout);
        let mut w = Worker {
            child,
            stdin,
            stdout,
        };
        let init = json!({"op": "init", "root": root.display().to_string(), "lib": lib, "live": live, "records": records.to_value(), "version": version});
        let r = w.request(&init)?;
        if r.get("python")
            .and_then(Value::as_str)
            .map(|v| !v.starts_with("3."))
            .unwrap_or(true)
        {
            return Err(format!("contract-test worker is not Python 3: {r}"));
        }
        Ok(w)
    }

    /// One request and its response. A `fatal` response is an error.
    pub fn request(&mut self, req: &Value) -> Result<Value, String> {
        let mut line = serde_json::to_string(req).map_err(|e| e.to_string())?;
        line.push('\n');
        self.stdin
            .write_all(line.as_bytes())
            .and_then(|_| self.stdin.flush())
            .map_err(|e| format!("contract-test worker: {e}"))?;
        let mut resp = String::new();
        let n = self
            .stdout
            .read_line(&mut resp)
            .map_err(|e| format!("contract-test worker: {e}"))?;
        if n == 0 {
            return Err("contract-test worker exited unexpectedly".into());
        }
        let v: Value = serde_json::from_str(&resp)
            .map_err(|e| format!("contract-test worker sent malformed output: {e}"))?;
        if let Some(f) = v.get("fatal").and_then(Value::as_str) {
            return Err(f.to_string());
        }
        Ok(v)
    }
}

impl Drop for Worker {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

// ---------------------------------------------------------------------------------------------
// Targets and outcomes
// ---------------------------------------------------------------------------------------------

/// A primitive or adapter contract under test.
#[derive(Debug, Clone, Copy)]
pub enum Target<'a> {
    /// A primitive implemented in `lib/`.
    Primitive(&'a Primitive),
    /// An adapter exercised through the gateway in replay mode.
    Adapter(&'a Adapter),
}

impl<'a> Target<'a> {
    /// The contract name.
    pub fn name(&self) -> &'a str {
        match self {
            Target::Primitive(p) => &p.name,
            Target::Adapter(a) => &a.name,
        }
    }
    /// Declared parameters in declared order.
    pub fn params(&self) -> &'a [(String, TypeExpr)] {
        match self {
            Target::Primitive(p) => &p.params.0,
            Target::Adapter(a) => &a.params.0,
        }
    }
    /// Declared return type.
    pub fn returns(&self) -> &'a TypeExpr {
        match self {
            Target::Primitive(p) => &p.returns,
            Target::Adapter(a) => &a.returns,
        }
    }
    /// Preconditions with their failure codes, in declared order.
    pub fn preconditions(&self) -> Vec<(&'a Expression, Code)> {
        match self {
            Target::Primitive(p) => p
                .preconditions
                .iter()
                .map(|c| (c.expr(), c.code()))
                .collect(),
            Target::Adapter(a) => a
                .preconditions
                .iter()
                .map(|c| (c.expr(), c.code()))
                .collect(),
        }
    }
    /// Declared effects; adapters declare none.
    pub fn effects(&self) -> &'a [Expression] {
        match self {
            Target::Primitive(p) => &p.effects,
            Target::Adapter(_) => &[],
        }
    }
    /// Declared side-effect class.
    pub fn side_effect(&self) -> &'static str {
        match self {
            Target::Primitive(p) => p.side_effect.as_str(),
            Target::Adapter(a) => a.side_effect.as_str(),
        }
    }
    /// Whether this target is an adapter.
    pub fn is_adapter(&self) -> bool {
        matches!(self, Target::Adapter(_))
    }
    /// Every code the contract declares: precondition codes, adapter failures, and INVALID
    /// for parameter validation.
    pub fn declared_codes(&self) -> BTreeSet<Code> {
        let mut s: BTreeSet<Code> = self.preconditions().into_iter().map(|(_, c)| c).collect();
        s.insert(Code::Invalid);
        if let Target::Adapter(a) = self {
            s.extend(a.failures.iter().copied());
        }
        s
    }
}

/// The outcome of one call.
#[derive(Debug, Clone, PartialEq)]
pub struct CallOutcome {
    /// `None` for ok.
    pub code: Option<Code>,
    /// One-based index of the failing precondition.
    pub precondition: Option<usize>,
    /// Returned value of an ok call.
    pub returned: Option<Value>,
    /// `retry_after` in ticks carried by a RATE_LIMITED outcome.
    pub retry_after: Option<Value>,
    /// Defects observed; non-empty implies `code == Some(INVARIANT_VIOLATION)`.
    pub defects: Vec<String>,
    /// Whether an ok result matched the declared return shape.
    pub shape_ok: bool,
}

impl CallOutcome {
    /// `ok` or the envelope code.
    pub fn status(&self) -> String {
        match self.code {
            None => "ok".into(),
            Some(c) => c.as_str().into(),
        }
    }
    fn fail(code: Code, precondition: Option<usize>) -> Self {
        CallOutcome {
            code: Some(code),
            precondition,
            returned: None,
            retry_after: None,
            defects: Vec::new(),
            shape_ok: true,
        }
    }
    fn defect(msg: String) -> Self {
        CallOutcome {
            code: Some(Code::InvariantViolation),
            precondition: None,
            returned: None,
            retry_after: None,
            defects: vec![msg],
            shape_ok: false,
        }
    }
}

// ---------------------------------------------------------------------------------------------
// The ephemeral Instance
// ---------------------------------------------------------------------------------------------

/// Settings the ephemeral Instance needs from `world.kvx`.
#[derive(Debug, Clone)]
pub struct WorldSettings {
    /// `limits.<k>` values.
    pub limits: BTreeMap<String, u64>,
    /// World ticks added per primitive call.
    pub tick_per_primitive: u64,
    /// World ticks added per turn.
    pub tick_per_turn: u64,
}

/// The ephemeral Instance: state lives in a worker process created and discarded inside the compiler.
pub struct Ephemeral<'a> {
    worker: Worker,
    targets: &'a BTreeMap<String, Target<'a>>,
    records: &'a Records,
    settings: &'a WorldSettings,
    /// World time in ticks.
    pub world_time: u64,
    primitive_calls: u64,
    adapter_calls: u64,
    ledger: Vec<Value>,
}

/// Number of RNG draws made available to one case.
const DRAWS: usize = 64;

struct Env<'e> {
    params: &'e Map<String, Value>,
    result: Option<&'e Value>,
}

impl<'a> Ephemeral<'a> {
    /// Creates an ephemeral Instance over a fresh worker.
    pub fn new(
        worker: Worker,
        targets: &'a BTreeMap<String, Target<'a>>,
        records: &'a Records,
        settings: &'a WorldSettings,
    ) -> Self {
        Ephemeral {
            worker,
            targets,
            records,
            settings,
            world_time: 0,
            primitive_calls: 0,
            adapter_calls: 0,
            ledger: Vec::new(),
        }
    }

    /// Discards the previous Instance and creates a fresh one from a fixture state (raw
    /// fixture JSON keyed by module; modules not named start from their `INITIAL_STATE`).
    /// `cassette` entries are `{"adapter", "params" (tagged), "sequence", "response"}`.
    pub fn reset(
        &mut self,
        state: &Value,
        world_time: u64,
        seed: u64,
        cassette: &[Value],
    ) -> Result<(), String> {
        let mut rng = cwr_kernel::Pcg32::seeded(seed);
        let draws: Vec<u32> = (0..DRAWS).map(|_| rng.next_u32()).collect();
        self.world_time = world_time;
        self.primitive_calls = 0;
        self.adapter_calls = 0;
        self.ledger.clear();
        let r = self.worker.request(&json!({"op": "reset", "state": state, "seed": seed, "randoms": draws, "cassette": cassette}))?;
        match r.get("error").and_then(Value::as_str) {
            Some(e) => Err(e.to_string()),
            None => Ok(()),
        }
    }

    /// Decodes raw fixture values by contract type strings (`{"$repeat": [s, n]}` expansion,
    /// UTF-8 strings in bytes positions).
    pub fn decode(&mut self, items: &[(Value, String)]) -> Result<Vec<Value>, String> {
        let req = json!({"op": "decode", "items": items.iter().map(|(v, t)| json!([v, t])).collect::<Vec<_>>()});
        let r = self.worker.request(&req)?;
        r.get("values")
            .and_then(Value::as_array)
            .cloned()
            .ok_or_else(|| "decode: malformed response".to_string())
    }

    /// Runs one migration fixture case; returns the list of problems (empty on success).
    pub fn migrate(
        &mut self,
        path: &str,
        before: &Value,
        after: &Value,
    ) -> Result<Vec<String>, String> {
        let r = self
            .worker
            .request(&json!({"op": "migrate", "path": path, "before": before, "after": after}))?;
        Ok(r.get("errors")
            .and_then(Value::as_array)
            .map(|a| {
                a.iter()
                    .filter_map(|x| x.as_str().map(str::to_string))
                    .collect()
            })
            .unwrap_or_default())
    }

    fn limits_record(&self) -> Value {
        let l = &self.settings.limits;
        let g = |k: &str| l.get(k).copied().unwrap_or(0);
        json!({
            "cpu_ms": g("cpu_ms"),
            "wall_ms": g("wall_ms"),
            "memory_mb": g("memory_mb"),
            "primitive_calls": g("primitive_calls"),
            "adapter_calls": g("adapter_calls"),
            "remaining_primitive_calls": g("primitive_calls").saturating_sub(self.primitive_calls),
            "remaining_adapter_calls": g("adapter_calls").saturating_sub(self.adapter_calls),
            "tick_per_primitive": self.settings.tick_per_primitive,
            "tick_per_turn": self.settings.tick_per_turn,
        })
    }

    fn lib_call(&mut self, name: &str, args: Vec<Value>) -> Result<Value, String> {
        let req = json!({"op": "call", "fn": name, "args": args, "world_time": self.world_time, "limits": self.limits_record(), "ledger": self.ledger});
        self.worker.request(&req)
    }

    /// A read_only call made while evaluating a precondition or effect.
    fn read_only(&mut self, target: &str, args: Vec<Value>) -> Result<Value, String> {
        let t = self
            .targets
            .get(target)
            .ok_or_else(|| format!("{target} is not a primitive of this Build"))?;
        if t.side_effect() != "read_only" {
            return Err(format!("{target} is not read_only"));
        }
        let r = self.lib_call(target, args)?;
        if r.get("changed").and_then(Value::as_bool) == Some(true) {
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

    fn eval_expr(&mut self, e: &Expr, env: &Env) -> Result<Value, String> {
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

    fn name_value(name: &str, env: &Env) -> Result<Value, String> {
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

    fn eval_term(&mut self, t: &Term, env: &Env) -> Result<Value, String> {
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
        t: &Target,
        params: &Map<String, Value>,
        evaluated: Vec<Value>,
        out: &CallOutcome,
    ) {
        let seq = self.ledger.len() as u64;
        let rec = json!({
            "seq": seq,
            "turn": 0,
            "world_time": self.world_time,
            "kind": if t.is_adapter() { "adapter" } else { "primitive" },
            "primitive": t.name(),
            "params_hash": cwr_kernel::params_hash(&Value::Object(params.clone())),
            "preconditions": evaluated,
            "outcome": {
                "status": if out.code.is_none() { "ok" } else { "error" },
                "code": out.code.map(|c| c.as_str()).unwrap_or(""),
                "shape": t.returns().to_string(),
                "side_effect": t.side_effect(),
            },
        });
        self.ledger.push(rec);
    }

    /// Calls a primitive or adapter through the atomic contract check.
    pub fn call(&mut self, name: &str, params: &Map<String, Value>) -> CallOutcome {
        let Some(t) = self.targets.get(name).copied() else {
            return CallOutcome::fail(Code::Invalid, None);
        };
        // Step 1: parameters.
        let declared: BTreeSet<&str> = t.params().iter().map(|(n, _)| n.as_str()).collect();
        let given: BTreeSet<&str> = params.keys().map(String::as_str).collect();
        let mut invalid = declared != given;
        if !invalid {
            for (n, ty) in t.params() {
                if check_shape(&params[n], ty, self.records).is_err() {
                    invalid = true;
                }
            }
        }
        if invalid {
            let out = CallOutcome::fail(Code::Invalid, None);
            self.record(&t, params, Vec::new(), &out);
            return out;
        }
        // Step 2: preconditions in declared order.
        let mut evaluated = Vec::new();
        for (i, (expr, code)) in t.preconditions().into_iter().enumerate() {
            let env = Env {
                params,
                result: None,
            };
            match self.eval_expr(expr.ast(), &env) {
                Ok(v) => {
                    let passed = truthy(&v);
                    evaluated.push(json!({"index": i + 1, "expr_hash": format!("sha256:{}", cwr_kernel::sha256_hex(expr.source().as_bytes())), "passed": passed}));
                    if !passed {
                        let out = CallOutcome::fail(code, Some(i + 1));
                        self.record(&t, params, evaluated, &out);
                        return out;
                    }
                }
                Err(m) => {
                    let out = CallOutcome::defect(format!(
                        "precondition {} `{}` of {name} could not be evaluated: {m}",
                        i + 1,
                        expr.source()
                    ));
                    self.record(&t, params, evaluated, &out);
                    return out;
                }
            }
        }
        // Step 3: execute.
        let args: Vec<Value> = t.params().iter().map(|(n, _)| params[n].clone()).collect();
        let declared_codes = t.declared_codes();
        let mut out = match t {
            Target::Primitive(p) => {
                self.primitive_calls += 1;
                match self.lib_call(name, args) {
                    Err(m) => CallOutcome::defect(format!("{name}: {m}")),
                    Ok(r) => self.primitive_result(p, &r, &declared_codes),
                }
            }
            Target::Adapter(a) => {
                self.adapter_calls += 1;
                let req = json!({
                    "op": "adapter", "name": name, "args": args, "params": params,
                    "timeout_ms": a.timeout_ms.get(), "idempotency": a.idempotency.as_str(),
                    "undeclared_status": a.undeclared_code().as_str(), "retry_after_ratio": a.retry_ratio(),
                    "world_time": self.world_time,
                });
                match self.worker.request(&req) {
                    Err(m) => CallOutcome::defect(format!("{name}: {m}")),
                    Ok(r) => self.adapter_result(a, &r, &declared_codes),
                }
            }
        };
        // Step 4: effects after ok.
        if out.code.is_none() {
            let result = out.returned.clone().unwrap_or(Value::Null);
            for e in t.effects() {
                let env = Env {
                    params,
                    result: Some(&result),
                };
                match self.eval_expr(e.ast(), &env) {
                    Ok(v) if truthy(&v) => {}
                    Ok(_) => out.defects.push(format!(
                        "effect `{}` of {name} does not hold after an ok outcome",
                        e.source()
                    )),
                    Err(m) => out.defects.push(format!(
                        "effect `{}` of {name} could not be evaluated: {m}",
                        e.source()
                    )),
                }
            }
            if !out.defects.is_empty() {
                out.code = Some(Code::InvariantViolation);
            }
        }
        // Step 5: tick; step 6: ledger record.
        self.world_time += self.settings.tick_per_primitive;
        self.record(&t, params, evaluated, &out);
        out
    }

    fn primitive_result(&self, p: &Primitive, r: &Value, declared: &BTreeSet<Code>) -> CallOutcome {
        let name = &p.name;
        if let Some(v) = r.get("result") {
            let mut out = CallOutcome {
                code: None,
                precondition: None,
                returned: Some(v.clone()),
                retry_after: None,
                defects: Vec::new(),
                shape_ok: true,
            };
            if p.side_effect.as_str() == "read_only"
                && r.get("changed").and_then(Value::as_bool) == Some(true)
            {
                out.defects
                    .push(format!("read_only primitive {name} changed Instance state"));
            }
            if let Err(e) = check_shape(v, &p.returns, self.records) {
                out.shape_ok = false;
                out.defects.push(format!(
                    "{name} returned a value that does not match its declared return type {}: {e}",
                    p.returns
                ));
            }
            if !out.defects.is_empty() {
                out.code = Some(Code::InvariantViolation);
            }
            return out;
        }
        if let Some(c) = r.get("raised").and_then(Value::as_str) {
            let detail = r.get("detail").and_then(Value::as_str).unwrap_or("");
            return match c.parse::<Code>() {
                Err(_) => CallOutcome::defect(format!("{name} raised {c:?}, which is outside the eleven-code failure envelope (INVARIANT_VIOLATION) {detail}")),
                Ok(code) if !declared.contains(&code) => CallOutcome::defect(format!(
                    "{name} emitted {c} after its preconditions passed; its contract does not declare {c} (INVARIANT_VIOLATION)"
                )),
                Ok(code) if p.side_effect.as_str() != "read_only" => CallOutcome::defect(format!(
                    "mutating primitive {name} failed with {} in its implementation after its preconditions passed (INVARIANT_VIOLATION)",
                    code.as_str()
                )),
                Ok(code) => CallOutcome::fail(code, None),
            };
        }
        if let Some(e) = r.get("exception").and_then(Value::as_str) {
            return CallOutcome::defect(format!(
                "a Python exception escaped lib/ in {name}: {e} (INVARIANT_VIOLATION)"
            ));
        }
        CallOutcome::defect(format!("{name} has no lib/ implementation"))
    }

    fn adapter_result(&self, a: &Adapter, r: &Value, declared: &BTreeSet<Code>) -> CallOutcome {
        let name = &a.name;
        if let Some(v) = r.get("result") {
            if check_shape(v, &a.returns, self.records).is_err() {
                // The gateway validates the shape and reports INVALID when it does not hold.
                let mut out = CallOutcome::fail(Code::Invalid, None);
                out.shape_ok = false;
                if !a.failures.contains(&Code::Invalid) {
                    // Clause 7.2, as the kernel gateway applies it: a malformed body is INVALID
                    // where declared, else the contract's undeclared_status (a declared code).
                    out.code = Some(a.undeclared_code());
                }
                return out;
            }
            return CallOutcome {
                code: None,
                precondition: None,
                returned: Some(v.clone()),
                retry_after: None,
                defects: Vec::new(),
                shape_ok: true,
            };
        }
        if let Some(c) = r.get("code").and_then(Value::as_str) {
            let retry_after = r.get("retry_after").cloned().filter(|v| !v.is_null());
            return match c.parse::<Code>() {
                Err(_) => CallOutcome::defect(format!("{name}: the gateway mapped a response to {c:?}, outside the failure envelope")),
                Ok(code) if !declared.contains(&code) => {
                    CallOutcome::defect(format!("{name} emitted {c}, which its contract does not declare among its failures (INVARIANT_VIOLATION)"))
                }
                Ok(Code::RateLimited) if retry_after.as_ref().and_then(as_int).map(|n| n < 0).unwrap_or(true) => {
                    CallOutcome::defect(format!("{name}: RATE_LIMITED without an integer retry_after in ticks"))
                }
                Ok(code) => {
                    let mut out = CallOutcome::fail(code, None);
                    out.retry_after = retry_after;
                    out
                }
            };
        }
        if let Some(e) = r.get("exception").and_then(Value::as_str) {
            return CallOutcome::defect(format!("a Python exception escaped adapters/{}/live.py in {name}: {e} (INVARIANT_VIOLATION)", name.split('.').next().unwrap_or("")));
        }
        CallOutcome::defect(format!("{name} has no live.py implementation"))
    }
}

/// The contract-test worker: executes world code on behalf of the ephemeral Instance.
pub const WORKER: &str = r#####"
import base64, copy, hashlib, importlib.util, json, keyword, signal, sys, types, typing

PROTO = sys.stdout
sys.stdout = sys.stderr
STEP_SECONDS = 20

# World code has no network capability (clause 7.1). Audit hooks cannot be removed once added.
_DENIED = ("socket.", "ctypes.", "subprocess.", "os.system", "os.exec", "os.posix_spawn", "os.spawn", "os.fork", "os.forkpty", "_winapi.")
_NATIVE = (".so", ".pyd", ".dylib", ".dll")
# Modules that start processes or interpreters without raising any audit event of their own.
_NO_IMPORT = ("_posixsubprocess", "_xxsubinterpreters", "_xxinterpchannels", "_interpreters", "_interpchannels", "_testcapi", "_testinternalcapi", "_testmultiphase", "_testsinglephase", "_testimportmultiple")

def _native(path):
    if isinstance(path, bytes):
        path = path.decode("utf-8", "replace")
    return isinstance(path, str) and path.lower().endswith(_NATIVE)

def _guard(event, args):
    if event.startswith(_DENIED):
        raise PermissionError("contract tests: world code has no network or process capability (%s)" % event)
    if event == "import" and args and str(args[0]).split(".")[0] in _NO_IMPORT:
        raise PermissionError("contract tests: world code has no process capability (import %s)" % args[0])
    if event == "open" and len(args) >= 3 and _native(args[0]):
        mode, flags = args[1], args[2] or 0
        if (isinstance(mode, str) and any(c in mode for c in "wax+")) or (mode is None and flags & 3):
            raise PermissionError("contract tests: world code may not write native extension files")
    if event in ("os.rename", "os.link", "os.symlink") and len(args) >= 2 and _native(args[1]):
        raise PermissionError("contract tests: world code may not create native extension files")

sys.addaudithook(_guard)

def out(obj):
    PROTO.write(json.dumps(obj, sort_keys=True, separators=(",", ":")) + "\n")
    PROTO.flush()

def enc(v):
    if isinstance(v, (bytes, bytearray)):
        return {"$bytes": base64.b64encode(bytes(v)).decode("ascii")}
    if isinstance(v, bool) or v is None or isinstance(v, (int, str)):
        return v
    if isinstance(v, dict):
        return {str(k): enc(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [enc(x) for x in v]
    raise TypeError("value of type %s is outside the codec" % type(v).__name__)

def dec(v):
    if isinstance(v, dict):
        if set(v) == {"$bytes"}:
            return base64.b64decode(v["$bytes"])
        return {k: dec(x) for k, x in v.items()}
    if isinstance(v, list):
        return [dec(x) for x in v]
    return v

def expand(v):
    if isinstance(v, dict):
        if set(v) == {"$repeat"} and isinstance(v["$repeat"], list) and len(v["$repeat"]) == 2:
            s, n = v["$repeat"]
            return s * n
        return {k: expand(x) for k, x in v.items()}
    if isinstance(v, list):
        return [expand(x) for x in v]
    return v

class Failure(Exception):
    def __init__(self, code, detail=""):
        super().__init__("%s: %s" % (code, detail) if detail else str(code))
        self.code = code
        self.detail = detail

class StepTimeout(Exception):
    pass

def _alarm(signum, frame):
    raise StepTimeout("world code exceeded %d s of host time in contract tests" % STEP_SECONDS)

signal.signal(signal.SIGALRM, _alarm)

CURRENT = [None]
SHIM = types.ModuleType("cwr_state")
SHIM.Failure = Failure
SHIM.world_time = lambda: CURRENT[0].world_time()
SHIM.random = lambda: CURRENT[0].random()
sys.modules["cwr_state"] = SHIM

def pyname(member):
    return member + "_" if keyword.iskeyword(member) else member

class Inst:
    pass

I = Inst()
I.root = "."
I.libpaths = {}
I.livepaths = {}
I.records = {}
I.version = ""
I.lib = {}
I.live = {}
I.state = {}
I.work = {}
I.counter = 0

class Handle:
    def module(self, name):
        return I.work[name]
    def world_time(self):
        return I.world_time
    def random(self):
        if not I.randoms:
            raise RuntimeError("the ephemeral Instance's random stream is exhausted")
        return I.randoms.pop(0)
    def seed(self):
        return I.seed
    def build_version(self):
        return I.version
    def limits(self):
        return dict(I.limits)
    def history(self, n):
        n = int(n)
        return copy.deepcopy(I.ledger[-n:]) if n > 0 else []
    def propose(self, text):
        I.outbox.append(text)
        return "proposal-%d" % len(I.outbox)

def load(rel, tag):
    I.counter += 1
    path = I.root + "/" + rel
    spec = importlib.util.spec_from_file_location("_cwr_ephemeral_%s_%d" % (tag, I.counter), path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

def state_hint(mod):
    try:
        hints = typing.get_type_hints(mod)
    except Exception:
        hints = {}
    h = hints.get("INITIAL_STATE")
    if h is not None:
        return h
    for name in sorted(vars(mod)):
        v = getattr(mod, name)
        if name.endswith("State") and isinstance(v, type) and typing.is_typeddict(v):
            return v
    return None

def is_union(h):
    o = typing.get_origin(h)
    return o is typing.Union or o is types.UnionType

def by_hint(v, h):
    if h is None or h is typing.Any:
        return v
    if h is bytes:
        return v.encode("utf-8") if isinstance(v, str) else v
    if isinstance(h, type) and typing.is_typeddict(h):
        if not isinstance(v, dict):
            return v
        hints = typing.get_type_hints(h)
        return {k: by_hint(x, hints.get(k)) for k, x in v.items()}
    o, a = typing.get_origin(h), typing.get_args(h)
    if o is dict and len(a) == 2 and isinstance(v, dict):
        return {k: by_hint(x, a[1]) for k, x in v.items()}
    if o is list and len(a) == 1 and isinstance(v, list):
        return [by_hint(x, a[0]) for x in v]
    if is_union(h):
        if v is None:
            return None
        for arg in a:
            if arg is not type(None):
                return by_hint(v, arg)
    return v

def validate(v, h, where, lenient):
    if h is None or h is typing.Any:
        return None
    if h is type(None):
        return None if v is None else "%s: expected None" % where
    if h is bytes:
        return None if isinstance(v, (bytes, bytearray)) or (lenient and isinstance(v, str)) else "%s: expected bytes" % where
    if h is bool:
        return None if isinstance(v, bool) else "%s: expected bool" % where
    if h is int:
        return None if isinstance(v, int) and not isinstance(v, bool) else "%s: expected int" % where
    if h is str:
        return None if isinstance(v, str) else "%s: expected str" % where
    if isinstance(h, type) and typing.is_typeddict(h):
        if not isinstance(v, dict):
            return "%s: expected a %s record" % (where, h.__name__)
        hints = typing.get_type_hints(h)
        missing = sorted(set(h.__required_keys__) - set(v))
        extra = sorted(set(v) - set(hints))
        if missing or extra:
            return "%s: record %s missing keys %s, unknown keys %s" % (where, h.__name__, missing, extra)
        for k, x in v.items():
            e = validate(x, hints[k], "%s.%s" % (where, k), lenient)
            if e:
                return e
        return None
    o, a = typing.get_origin(h), typing.get_args(h)
    if o is dict:
        if not isinstance(v, dict):
            return "%s: expected dict" % where
        for k, x in v.items():
            if not isinstance(k, str):
                return "%s: key %r is not str" % (where, k)
            if len(a) == 2:
                e = validate(x, a[1], "%s[%r]" % (where, k), lenient)
                if e:
                    return e
        return None
    if o is list:
        if not isinstance(v, list):
            return "%s: expected list" % where
        for i, x in enumerate(v):
            if len(a) == 1:
                e = validate(x, a[0], "%s[%d]" % (where, i), lenient)
                if e:
                    return e
        return None
    if is_union(h):
        errs = [validate(v, arg, where, lenient) for arg in a]
        return None if any(e is None for e in errs) else errs[0]
    return None

def by_type(v, t):
    t = (t or "").strip()
    if t == "bytes":
        return v.encode("utf-8") if isinstance(v, str) else v
    if t.startswith("list[") and t.endswith("]") and isinstance(v, list):
        return [by_type(x, t[5:-1]) for x in v]
    if t.startswith("dict[str, ") and t.endswith("]") and isinstance(v, dict):
        return {k: by_type(x, t[10:-1]) for k, x in v.items()}
    if t in I.records and isinstance(v, dict):
        fields = I.records[t]
        return {k: by_type(x, fields.get(k, "")) for k, x in v.items()}
    return v

def guarded(fn, *args):
    signal.alarm(STEP_SECONDS)
    try:
        return fn(*args)
    finally:
        signal.alarm(0)

def describe(e):
    text = "%s: %s" % (type(e).__name__, e)
    return text if len(text) <= 400 else text[:400] + "..."

def raised_code(e):
    code = getattr(e, "code", None)
    return code if isinstance(code, str) else None

def op_init(req):
    I.root = req["root"]
    I.libpaths = req["lib"]
    I.livepaths = req["live"]
    I.records = req["records"]
    I.version = req["version"]
    return {"python": "%d.%d.%d" % sys.version_info[:3]}

def op_reset(req):
    try:
        I.lib = {m: load(p, m) for m, p in sorted(I.libpaths.items())}
        I.live = {m: load(p, "live_" + m) for m, p in sorted(I.livepaths.items())}
    except Exception as e:
        return {"fatal": "world code failed to load: " + describe(e)}
    state = {}
    for m, mod in I.lib.items():
        state[m] = copy.deepcopy(getattr(mod, "INITIAL_STATE", {}))
    raw = expand(req["state"])
    if not isinstance(raw, dict):
        return {"error": "fixture state must be an object keyed by module"}
    for m, v in raw.items():
        if m not in I.lib:
            return {"error": "fixture state names module %s, which has no lib/ implementation" % m}
        h = state_hint(I.lib[m])
        dv = by_hint(v, h)
        e = validate(dv, h, "state." + m, False)
        if e:
            return {"error": "fixture state does not match the typed state record: " + e}
        state[m] = dv
    I.state = state
    I.work = state
    I.seed = req["seed"]
    I.randoms = list(req["randoms"])
    I.outbox = []
    I.ledger = []
    I.limits = {}
    I.world_time = 0
    I.store = {}
    I.counts = {}
    I.cassette = {}
    for c in req["cassette"]:
        h = req_hash(c["adapter"], c["params"])
        I.cassette[(c["adapter"], h, int(c.get("sequence") or 1))] = c["response"]
    return {}

def op_decode(req):
    return {"values": [enc(by_type(expand(v), t)) for v, t in req["items"]]}

def op_call(req):
    name = req["fn"]
    module, member = name.split(".", 1)
    mod = I.lib.get(module)
    fn = getattr(mod, pyname(member), None) if mod is not None else None
    if fn is None:
        return {"missing": True}
    I.world_time = req["world_time"]
    I.limits = req["limits"]
    I.ledger = req["ledger"]
    work = copy.deepcopy(I.state)
    I.work = work
    handle = Handle()
    CURRENT[0] = handle
    try:
        result = guarded(fn, handle, *[dec(a) for a in req["args"]])
    except Failure as e:
        I.work = I.state
        return {"raised": e.code, "detail": e.detail, "changed": False}
    except Exception as e:
        I.work = I.state
        code = raised_code(e)
        if code is not None:
            return {"raised": code, "detail": describe(e), "changed": False}
        return {"exception": describe(e), "changed": False}
    changed = work != I.state
    I.state = work
    I.work = work
    try:
        encoded = enc(result)
    except TypeError as e:
        return {"exception": describe(e), "changed": changed}
    return {"result": encoded, "changed": changed}

def req_hash(adapter, params):
    text = json.dumps({"adapter": adapter, "params": params}, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()

class GwFail(Exception):
    def __init__(self, code, retry_after=None, detail=""):
        super().__init__(code)
        self.code = code
        self.retry_after = retry_after
        self.detail = detail

def op_adapter(req):
    name = req["name"]
    module, member = name.split(".", 1)
    live = I.live.get(module)
    fn = getattr(live, pyname(member), None) if live is not None else None
    if fn is None:
        return {"missing": True}
    params = req["params"]
    h = req_hash(name, params)
    key_param = getattr(live, "IDEMPOTENCY_KEY_PARAM", {}).get(name) if req["idempotency"] == "key" else None
    store_key = None
    if key_param is not None:
        store_key = (name, json.dumps(params.get(key_param), sort_keys=True))
        stored = I.store.get(store_key)
        if stored is not None:
            # The first outcome per key, ok or failure, as the kernel gateway stores it (kernel.md section 7).
            if stored[0] == h:
                return dict(stored[1], replayed=True)
            return {"code": "DUPLICATE_KEY", "retry_after": None, "detail": "idempotency key reused with a different payload"}
    status_codes = getattr(live, "STATUS_CODES", {}).get(name, {})
    retry_field = getattr(live, "RETRY_AFTER_FIELD", None)
    timeout_ms = req["timeout_ms"]
    # The contract's undeclared_status and retry_after_ratio (kernel.md section 7), as the kernel gateway applies them.
    undeclared = req.get("undeclared_status") or "UNAVAILABLE"
    ratio = req.get("retry_after_ratio") or 1

    class Gateway:
        def send(self, payload):
            k = (name, h)
            n = I.counts.get(k, 0) + 1
            I.counts[k] = n
            resp = I.cassette.get((name, h, n))
            if resp is None:
                raise GwFail("UNAVAILABLE", None, "replay miss: no cassette entry")
            if (resp.get("delay_ms") or 0) > timeout_ms:
                raise GwFail("TIMEOUT")
            status = resp.get("status")
            mapped = status_codes.get(status)
            if mapped is None:
                raise GwFail(undeclared, None, "undeclared service status %r" % (status,))
            body = copy.deepcopy(resp.get("body") or {})
            if mapped == "ok":
                return body
            ra = body.get(retry_field) if (mapped == "RATE_LIMITED" and retry_field and isinstance(body, dict)) else None
            if isinstance(ra, int) and not isinstance(ra, bool):
                ra = ra * ratio
            raise GwFail(mapped, ra)

    try:
        result = guarded(fn, Gateway(), *[dec(a) for a in req["args"]])
    except GwFail as e:
        failed = {"code": e.code, "retry_after": e.retry_after, "detail": e.detail}
        # RATE_LIMITED releases the key, as in the kernel gateway: the service did not process it.
        if store_key is not None and e.code != "RATE_LIMITED":
            I.store[store_key] = (h, failed)
        return failed
    except Exception as e:
        return {"exception": describe(e)}
    try:
        encoded = enc(result)
    except TypeError as e:
        return {"exception": describe(e)}
    if store_key is not None:
        I.store[store_key] = (h, {"result": encoded})
    return {"result": encoded}

def short(v):
    text = json.dumps(enc(v), sort_keys=True)
    return text if len(text) <= 300 else text[:300] + "..."

def op_status_codes(req):
    # live.py STATUS_CODES per adapter, for the compiler's check that every target is declared.
    try:
        live = {m: load(p, "live_" + m) for m, p in sorted(I.livepaths.items())}
    except Exception as e:
        return {"fatal": "world code failed to load: " + describe(e)}
    codes = {}
    for m, mod in live.items():
        table = getattr(mod, "STATUS_CODES", None)
        if isinstance(table, dict):
            for adapter, mapping in table.items():
                if isinstance(mapping, dict):
                    codes[str(adapter)] = {str(k): str(v) for k, v in mapping.items()}
    return {"codes": codes}

def op_migrate(req):
    errors = []
    try:
        mod = load(req["path"], "migration")
        libs = {m: load(p, "shape_" + m) for m, p in sorted(I.libpaths.items())}
    except Exception as e:
        return {"errors": ["migration failed to load: " + describe(e)]}
    fn = getattr(mod, "migrate", None)
    if fn is None:
        return {"errors": ["%s defines no migrate(state)" % req["path"]]}
    before, after = expand(req["before"]), expand(req["after"])
    arg = copy.deepcopy(before)
    try:
        r1 = guarded(fn, arg)
        if arg != before:
            errors.append("migrate mutated its argument; a migration is a pure function")
        if r1 != after:
            errors.append("migrate(before) != after: got %s" % short(r1))
        r2 = guarded(fn, copy.deepcopy(r1))
        if r2 != r1:
            errors.append("not idempotent: migrate(migrate(before)) != migrate(before)")
        r3 = guarded(fn, copy.deepcopy(after))
        if r3 != after:
            errors.append("not idempotent: migrate(after) != after")
    except Exception as e:
        return {"errors": ["migrate raised " + describe(e)]}
    if isinstance(r1, dict):
        for m, v in r1.items():
            if m in libs:
                e = validate(v, state_hint(libs[m]), "state." + m, True)
                if e:
                    errors.append("migrated state does not validate against the new state shape: " + e)
    else:
        errors.append("migrate returned a %s, not a state dict" % type(r1).__name__)
    return {"errors": errors}

OPS = {"init": op_init, "reset": op_reset, "decode": op_decode, "call": op_call, "adapter": op_adapter, "status_codes": op_status_codes, "migrate": op_migrate}

for line in sys.stdin:
    try:
        req = json.loads(line)
        out(OPS[req["op"]](req))
    except Exception as e:
        out({"fatal": "contract-test worker: " + describe(e)})
"#####;

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn records_parse() {
        let r = Records::parse("text\n\n```text\nrecord Hit\n  title: str        content\n  tags: dict[str, int]   x\n\nrecord Empty\n```\n");
        let hit = &r.0["Hit"];
        assert_eq!(hit.len(), 2);
        assert_eq!(hit[1].2, "dict[str, int]");
        assert!(hit[1].1.is_some());
        assert!(r.0["Empty"].is_empty());
    }

    #[test]
    fn value_helpers() {
        assert_eq!(b64_len("aGVsbG8="), 5);
        assert_eq!(b64_len(""), 0);
        assert_eq!(b64_len("="), 0);
        assert!(!truthy(&json!({})));
        assert_eq!(
            compare(CmpOp::Lt, &json!(1), &json!("a")),
            Err("cannot order a int and a str".into())
        );
        assert_eq!(len_of(&json!({"$bytes": "aGk="})).unwrap(), 2);
        assert_eq!(len_of(&json!("héllo")).unwrap(), 5);
        assert!(compare(CmpOp::Le, &json!(3), &json!(3)).unwrap());
        assert!(compare(CmpOp::Lt, &json!("a"), &json!(1)).is_err());
        assert!(!truthy(&json!({"$bytes": ""})));
        let recs = Records::default();
        assert!(check_shape(&json!(true), &TypeExpr::Int, &recs).is_err());
        assert!(check_shape(
            &json!([{"$bytes": "aGk="}]),
            &"list[bytes]".parse().unwrap(),
            &recs
        )
        .is_ok());
    }
}
