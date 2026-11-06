// Copyright Sidiora Labs Inc. CWR® 2026-2027
// SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0

//! State migrations applied by `cwr instance upgrade` (kernel.md section 10 steps 3 and 5;
//! world-source.md section 12; clauses 3.6 and 5.2).
//!
//! * [`plan`] reads the target Build's compiled changelog (`contracts.json` `changelog`) and lists,
//!   in changelog order, every migration declared by a release newer than the running version and
//!   not newer than the target version. It refuses the upgrade when the state shape of any module
//!   changes (by the target's `contract_diff.json` or by comparing the two Builds' compiled
//!   `state` shapes) without a `state_shape` change for that module naming a migration file that
//!   is present in the target Build.
//! * [`apply`] runs each migration's `migrate(state)` as a pure function in a fresh sandboxed
//!   Python 3.12 process ([`crate::sandbox::spawn_worker`]: no network, a read-only view holding
//!   only the interpreter and the target Build's `migrations/`), one process per migration, with
//!   the forbidden-import list of world-source.md section 12 enforced inside the process, a host
//!   wall budget equal to the target's `wall_ms` and a check that the argument was not mutated.
//!   The result must be an object of module states; modules the target adds start as `{}` (as at
//!   `create`). Each migration's result is validated against the target's compiled state shapes
//!   for every module that no later migration of the plan targets (a module still awaiting a
//!   later migration may hold an intermediate shape), so the result of the last migration, and
//!   therefore the final state, validates in full.
//!
//! Nothing here touches the Instance: [`apply`] works on a copy and returns the migrated state,
//! which the upgrade transaction commits or discards.

use std::collections::{BTreeMap, BTreeSet};
use std::io::{BufRead, BufReader, Write};
use std::path::{Component, Path};
use std::sync::mpsc;
use std::time::Duration;

use serde_json::{json, Map, Value};

use crate::state::{Build, WorldState};
use crate::{canonical_json, io_err, KernelError, Result};

/// One migration the upgrade applies: the changelog change that declares it and its file.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Migration {
    /// Version of the release that declares the change.
    pub release: String,
    /// Changelog change id.
    pub change: String,
    /// Module whose state shape the change alters.
    pub target: String,
    /// Path relative to the Build directory, always under `migrations/`.
    pub path: String,
}

/// A semantic version `MAJOR.MINOR.PATCH` compared numerically, never lexically
/// (world-source.md section 13). Returns None for anything else.
pub fn semver(s: &str) -> Option<(u64, u64, u64)> {
    let mut it = s.split('.');
    let mut part = || -> Option<u64> {
        let p = it.next()?;
        if p.is_empty()
            || !p.bytes().all(|b| b.is_ascii_digit())
            || (p.len() > 1 && p.starts_with('0'))
        {
            return None;
        }
        p.parse().ok()
    };
    let v = (part()?, part()?, part()?);
    it.next().is_none().then_some(v)
}

fn read_json(path: &Path) -> Result<Value> {
    let bytes = std::fs::read(path).map_err(|e| io_err("read", path, e))?;
    serde_json::from_slice(&bytes)
        .map_err(|e| KernelError::Refused(format!("{}: {e}", path.display())))
}

/// The compiled `contracts.json` of a Build.
pub fn contracts_of(build: &Build) -> Result<Value> {
    read_json(&build.dir.join("contracts.json"))
}

/// One compiled changelog release: version, summary and changes in listed order.
#[derive(Debug, Clone, PartialEq)]
pub struct Release {
    /// Release version.
    pub version: String,
    /// The one-sentence human summary.
    pub summary: String,
    /// The release's changes as compiled (`id`, `kind`, `target`, `before`, `after`, `migration`).
    pub changes: Vec<Value>,
}

/// The releases of `to`'s compiled changelog newer than `from_version` and not newer than
/// `to.version`, oldest first. Refuses a changelog entry whose version is not a semantic version.
pub fn releases_between(to: &Build, from_version: &str) -> Result<Vec<Release>> {
    let contracts = contracts_of(to)?;
    let lo = semver(from_version).ok_or_else(|| {
        KernelError::Refused(format!(
            "running version {from_version:?} is not MAJOR.MINOR.PATCH"
        ))
    })?;
    let hi = semver(&to.version).ok_or_else(|| {
        KernelError::Refused(format!(
            "target version {:?} is not MAJOR.MINOR.PATCH",
            to.version
        ))
    })?;
    let mut out: Vec<((u64, u64, u64), Release)> = Vec::new();
    for r in contracts["changelog"].as_array().into_iter().flatten() {
        let version = r["version"].as_str().unwrap_or_default().to_string();
        let v = semver(&version).ok_or_else(|| {
            KernelError::Refused(format!(
                "Build {}: changelog release {version:?} is not MAJOR.MINOR.PATCH",
                to.hash
            ))
        })?;
        if v > lo && v <= hi {
            out.push((
                v,
                Release {
                    version,
                    summary: r["summary"].as_str().unwrap_or_default().to_string(),
                    changes: r["changes"].as_array().cloned().unwrap_or_default(),
                },
            ));
        }
    }
    out.sort_by_key(|a| a.0);
    Ok(out.into_iter().map(|(_, r)| r).collect())
}

fn state_shapes(contracts: &Value) -> BTreeMap<String, String> {
    contracts["state"]
        .as_object()
        .map(|m| {
            m.iter()
                .filter_map(|(k, v)| Some((k.clone(), v.as_str()?.to_string())))
                .collect()
        })
        .unwrap_or_default()
}

/// A migration path is relative, under `migrations/`, without `..`, and names a file of the Build.
fn check_migration_path(to: &Build, path: &str) -> std::result::Result<(), String> {
    let p = Path::new(path);
    let mut comps = p.components();
    let under = comps.next() == Some(Component::Normal("migrations".as_ref()));
    if !under || p.components().any(|c| !matches!(c, Component::Normal(_))) {
        return Err(format!("migration path {path:?} is not under migrations/"));
    }
    if !to.dir.join(p).is_file() {
        return Err(format!(
            "migration {path} is not present in Build {}",
            to.hash
        ));
    }
    Ok(())
}

/// The migrations an upgrade from `from` to `to` applies, in changelog order (kernel.md section
/// 10 step 3). Refuses (with every reason) when a module's state shape changes without a
/// `state_shape` change for it naming a migration present in `to`, or when a declared migration
/// file is missing.
pub fn plan(from: &Build, to: &Build) -> Result<Vec<Migration>> {
    let releases = releases_between(to, &from.version)?;
    let mut problems = Vec::new();
    let mut migrations = Vec::new();
    let mut covered: BTreeSet<String> = BTreeSet::new();
    for r in &releases {
        for c in &r.changes {
            let path = c["migration"].as_str().unwrap_or_default();
            let kind = c["kind"].as_str().unwrap_or_default();
            let target = c["target"].as_str().unwrap_or_default();
            if path.is_empty() {
                continue;
            }
            if let Err(m) = check_migration_path(to, path) {
                problems.push(m);
                continue;
            }
            if kind == "state_shape" {
                covered.insert(target.to_string());
            }
            migrations.push(Migration {
                release: r.version.clone(),
                change: c["id"].as_str().unwrap_or_default().to_string(),
                target: target.to_string(),
                path: path.to_string(),
            });
        }
    }
    let mut needed: BTreeSet<String> = BTreeSet::new();
    let diff = read_json(&to.dir.join("contract_diff.json")).unwrap_or(Value::Null);
    for e in diff["entries"].as_array().into_iter().flatten() {
        if e["kind"] == "state_shape" {
            if let Some(t) = e["target"].as_str() {
                needed.insert(t.to_string());
            }
        }
    }
    let old_shapes = state_shapes(&contracts_of(from)?);
    let new_shapes = state_shapes(&contracts_of(to)?);
    for (m, old) in &old_shapes {
        if new_shapes.get(m) != Some(old) {
            needed.insert(m.clone());
        }
    }
    for m in &needed {
        if !covered.contains(m) {
            problems.push(format!(
                "the state shape of module {m} changes between {} and {} without a state_shape change naming a migration",
                from.version, to.version
            ));
        }
    }
    if problems.is_empty() {
        Ok(migrations)
    } else {
        Err(KernelError::Refused(problems.join("; ")))
    }
}

// ---------------------------------------------------------------------------------------------
// Applying migrations in the sandbox
// ---------------------------------------------------------------------------------------------

/// Python source of the migration runner: reads one request line `{"file", "state"}`, loads the
/// migration from the read-only `migrations/` mount with the forbidden-import guard in place,
/// calls `migrate` on a decoded copy of the state and answers `{"state"}` or `{"error"}`.
pub const MIGRATOR: &str = r#####"
import base64, builtins, copy, importlib.util, json, sys

FORBIDDEN = {"os", "sys", "socket", "http", "urllib", "time", "datetime", "random", "secrets",
             "subprocess", "pathlib", "shutil", "tempfile", "threading", "multiprocessing",
             "ctypes", "importlib", "cwr_state", "io", "builtins", "signal", "select", "posix"}
OUT = sys.stdout

def out(obj):
    OUT.write(json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n")
    OUT.flush()

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

def describe(e):
    text = "%s: %s" % (type(e).__name__, e)
    return text if len(text) <= 400 else text[:400] + "..."

def main():
    req = json.loads(sys.stdin.readline())
    real_import = builtins.__import__
    def guarded(name, globals=None, locals=None, fromlist=(), level=0):
        if level == 0 and name.partition(".")[0] in FORBIDDEN:
            raise ImportError("a migration may not import %s (world-source.md section 12)" % name)
        return real_import(name, globals, locals, fromlist, level)
    try:
        spec = importlib.util.spec_from_file_location("cwr_migration", "/build/lib/" + req["file"])
        mod = importlib.util.module_from_spec(spec)
        builtins.__import__ = guarded
        spec.loader.exec_module(mod)
    except BaseException as e:
        return out({"error": "migration failed to load: " + describe(e)})
    fn = getattr(mod, "migrate", None)
    if not callable(fn):
        return out({"error": "%s defines no migrate(state)" % req["file"]})
    before = dec(req["state"])
    arg = copy.deepcopy(before)
    try:
        result = fn(arg)
    except BaseException as e:
        return out({"error": "migrate raised " + describe(e)})
    if arg != before:
        return out({"error": "migrate mutated its argument; a migration is a pure function"})
    if not isinstance(result, dict):
        return out({"error": "migrate returned a %s, not a state dict" % type(result).__name__})
    try:
        return out({"state": enc(result)})
    except BaseException as e:
        return out({"error": "migrate returned a value outside the codec: " + describe(e)})

main()
"#####;

/// Runs one migration file of `build` over `state` (codec JSON) in a fresh sandboxed process.
fn run_one(
    build: &Build,
    migration: &Migration,
    state: &Value,
) -> std::result::Result<Value, String> {
    let rel = migration
        .path
        .strip_prefix("migrations/")
        .ok_or_else(|| format!("migration path {} is not under migrations/", migration.path))?;
    let mut child = crate::sandbox::spawn_worker(Some(&build.dir.join("migrations")), MIGRATOR)
        .map_err(|e| e.to_string())?;
    let request = format!(
        "{}\n",
        canonical_json(&json!({"file": rel, "state": state}))
    );
    let sent = child
        .stdin
        .take()
        .ok_or_else(|| "migration process has no stdin".to_string())
        .and_then(|mut stdin| {
            stdin
                .write_all(request.as_bytes())
                .map_err(|e| e.to_string())
        });
    let stdout = child.stdout.take();
    let (tx, rx) = mpsc::channel();
    let reader = std::thread::spawn(move || {
        let mut line = String::new();
        if let Some(out) = stdout {
            let _ = BufReader::new(out).read_line(&mut line);
        }
        let _ = tx.send(line);
    });
    let budget = Duration::from_millis(build.limits.get("wall_ms").copied().unwrap_or(30_000));
    let answer = rx.recv_timeout(budget);
    let _ = child.kill();
    let _ = child.wait();
    let _ = reader.join();
    sent.map_err(|e| format!("cannot send the state to the migration process: {e}"))?;
    let line = answer.map_err(|_| {
        format!(
            "migration {} exceeded the wall budget of {} ms",
            migration.path,
            budget.as_millis()
        )
    })?;
    let v: Value = serde_json::from_str(line.trim_end())
        .map_err(|_| format!("migration {} ended without an answer", migration.path))?;
    if let Some(e) = v.get("error").and_then(Value::as_str) {
        return Err(format!("{}: {e}", migration.path));
    }
    v.get("state")
        .cloned()
        .ok_or_else(|| format!("migration {} answered without a state", migration.path))
}

/// The module a changelog change target names: `kv` for `kv` and for `kv.set`.
fn module_of(target: &str) -> &str {
    target.split('.').next().unwrap_or(target)
}

/// Checks the module states of `state` (codec JSON) named in `shapes` against their target
/// shapes, skipping the modules in `pending`. `after` names the step for the failure text.
fn check_modules(
    state: &Value,
    shapes: &[(String, Shape)],
    pending: &BTreeSet<&str>,
    after: &str,
) -> std::result::Result<(), String> {
    let obj = state.as_object().ok_or_else(|| {
        format!("the state after {after} is not an object of module states: {state}")
    })?;
    for (module, ty) in shapes {
        if pending.contains(module.as_str()) {
            continue;
        }
        if let Some(v) = obj.get(module) {
            check_shape(v, ty, &format!("state.{module}"))
                .map_err(|e| format!("the state after {after} does not validate: {e}"))?;
        }
    }
    Ok(())
}

/// Applies `plan` in order to a copy of `state` and returns the migrated state for `to`, or the
/// first failure as text. After each migration, every module that no later migration of the plan
/// targets must validate against `to`'s compiled state shapes (kernel.md section 10 step 5). The
/// result has exactly `to`'s modules (added modules start as `{}`) and validates in full.
pub fn apply(
    to: &Build,
    plan: &[Migration],
    state: &WorldState,
) -> std::result::Result<WorldState, String> {
    let contracts = contracts_of(to).map_err(|e| e.to_string())?;
    let mut shapes = Vec::new();
    for (module, shape) in state_shapes(&contracts) {
        let ty = parse_shape(&shape)
            .map_err(|e| format!("state shape of {module} in Build {}: {e}", to.version))?;
        shapes.push((module, ty));
    }
    let mut current = state.to_json();
    for (i, m) in plan.iter().enumerate() {
        current = run_one(to, m, &current)?;
        let pending: BTreeSet<&str> = plan
            .iter()
            .skip(i + 1)
            .map(|later| module_of(&later.target))
            .collect();
        check_modules(&current, &shapes, &pending, &m.path)?;
    }
    let mut obj: Map<String, Value> = match current {
        Value::Object(o) => o,
        other => {
            return Err(format!(
                "migrated state is not an object of module states: {other}"
            ))
        }
    };
    let extra: Vec<&String> = obj.keys().filter(|k| !to.modules.contains(*k)).collect();
    if !extra.is_empty() {
        return Err(format!(
            "migrated state holds modules {extra:?} that Build {} does not have",
            to.version
        ));
    }
    for m in &to.modules {
        obj.entry(m.clone()).or_insert_with(|| json!({}));
    }
    let migrated = WorldState::from_json(&Value::Object(obj)).map_err(|e| e.to_string())?;
    for (module, ty) in &shapes {
        if let Some(v) = migrated.get(module) {
            check_shape(v, ty, &format!("state.{module}"))?;
        }
    }
    Ok(migrated)
}

// ---------------------------------------------------------------------------------------------
// State shapes
// ---------------------------------------------------------------------------------------------

/// A compiled state shape (`contracts.json` `state`), e.g.
/// `{files: dict[str, bytes], locks: list[str]}`.
#[derive(Debug, Clone, PartialEq)]
pub enum Shape {
    /// `str` or `Path`: a JSON string.
    Str,
    /// `int`: a JSON integer.
    Int,
    /// `bool`.
    Bool,
    /// `bytes`: the codec form `{"$bytes": "<base64>"}`.
    Bytes,
    /// `None`: null.
    Null,
    /// `Any`: any value.
    Any,
    /// `Optional[T]`.
    Optional(Box<Shape>),
    /// `list[T]`.
    List(Box<Shape>),
    /// `dict[str, T]`.
    Dict(Box<Shape>),
    /// `A|B|...`: any of the alternatives.
    Union(Vec<Shape>),
    /// `{name: T, ...}`: a record. Stored module state is merged over the module's
    /// `INITIAL_STATE` when loaded, so a field may be absent; an unknown field is refused.
    Record(Vec<(String, Shape)>),
}

struct ShapeParser<'a> {
    s: &'a [u8],
    i: usize,
}

impl ShapeParser<'_> {
    fn ws(&mut self) {
        while self.i < self.s.len() && self.s[self.i] == b' ' {
            self.i += 1;
        }
    }
    fn eat(&mut self, c: u8) -> std::result::Result<(), String> {
        self.ws();
        if self.s.get(self.i) == Some(&c) {
            self.i += 1;
            Ok(())
        } else {
            Err(format!("expected {:?} at offset {}", c as char, self.i))
        }
    }
    fn ident(&mut self) -> std::result::Result<String, String> {
        self.ws();
        let start = self.i;
        while self
            .s
            .get(self.i)
            .is_some_and(|b| b.is_ascii_alphanumeric() || *b == b'_')
        {
            self.i += 1;
        }
        if start == self.i {
            return Err(format!("expected a name at offset {start}"));
        }
        Ok(String::from_utf8_lossy(&self.s[start..self.i]).into_owned())
    }
    fn ty(&mut self) -> std::result::Result<Shape, String> {
        let mut alts = vec![self.atom()?];
        loop {
            self.ws();
            if self.s.get(self.i) != Some(&b'|') {
                break;
            }
            self.i += 1;
            alts.push(self.atom()?);
        }
        Ok(if alts.len() == 1 {
            alts.remove(0)
        } else {
            Shape::Union(alts)
        })
    }
    fn atom(&mut self) -> std::result::Result<Shape, String> {
        self.ws();
        if self.s.get(self.i) == Some(&b'{') {
            self.i += 1;
            let mut fields = Vec::new();
            self.ws();
            if self.s.get(self.i) == Some(&b'}') {
                self.i += 1;
                return Ok(Shape::Record(fields));
            }
            loop {
                let name = self.ident()?;
                self.eat(b':')?;
                fields.push((name, self.ty()?));
                self.ws();
                match self.s.get(self.i) {
                    Some(b',') => self.i += 1,
                    Some(b'}') => {
                        self.i += 1;
                        return Ok(Shape::Record(fields));
                    }
                    _ => return Err(format!("expected ',' or '}}' at offset {}", self.i)),
                }
            }
        }
        let name = self.ident()?;
        let one = |p: &mut Self| -> std::result::Result<Shape, String> {
            p.eat(b'[')?;
            let t = p.ty()?;
            p.eat(b']')?;
            Ok(t)
        };
        Ok(match name.as_str() {
            "str" | "Path" => Shape::Str,
            "int" => Shape::Int,
            "bool" => Shape::Bool,
            "bytes" => Shape::Bytes,
            "None" => Shape::Null,
            "Any" => Shape::Any,
            "Optional" => Shape::Optional(Box::new(one(self)?)),
            "list" => Shape::List(Box::new(one(self)?)),
            "dict" => {
                self.eat(b'[')?;
                let k = self.ident()?;
                if k != "str" {
                    return Err(format!("dict keys must be str, not {k}"));
                }
                self.eat(b',')?;
                let t = self.ty()?;
                self.eat(b']')?;
                Shape::Dict(Box::new(t))
            }
            other => return Err(format!("unknown type {other:?} in a state shape")),
        })
    }
}

/// Parses a compiled state shape.
pub fn parse_shape(s: &str) -> std::result::Result<Shape, String> {
    let mut p = ShapeParser {
        s: s.as_bytes(),
        i: 0,
    };
    let t = p.ty()?;
    p.ws();
    if p.i != s.len() {
        return Err(format!("trailing text at offset {} of {s:?}", p.i));
    }
    Ok(t)
}

fn is_codec_tag(m: &Map<String, Value>) -> bool {
    m.len() == 1 && m.keys().all(|k| k.starts_with('$') && !k.starts_with("$$"))
}

/// Checks a stored (codec JSON) value against a shape; the error names the first offending path.
pub fn check_shape(v: &Value, shape: &Shape, at: &str) -> std::result::Result<(), String> {
    let bad = |what: &str| Err(format!("{at}: expected {what}, found {v}"));
    match shape {
        Shape::Any => Ok(()),
        Shape::Str => v.is_string().then_some(()).map_or_else(|| bad("str"), Ok),
        Shape::Int => (v.is_i64() || v.is_u64())
            .then_some(())
            .map_or_else(|| bad("int"), Ok),
        Shape::Bool => v.is_boolean().then_some(()).map_or_else(|| bad("bool"), Ok),
        Shape::Null => v.is_null().then_some(()).map_or_else(|| bad("None"), Ok),
        Shape::Bytes => match v.as_object() {
            Some(m) if m.len() == 1 && m.get("$bytes").is_some_and(Value::is_string) => Ok(()),
            _ => bad("bytes"),
        },
        Shape::Optional(t) => {
            if v.is_null() {
                Ok(())
            } else {
                check_shape(v, t, at)
            }
        }
        Shape::Union(alts) => {
            if alts.iter().any(|t| check_shape(v, t, at).is_ok()) {
                Ok(())
            } else {
                bad("one of the union's alternatives")
            }
        }
        Shape::List(t) => match v.as_array() {
            Some(items) => items
                .iter()
                .enumerate()
                .try_for_each(|(i, x)| check_shape(x, t, &format!("{at}[{i}]"))),
            None => bad("list"),
        },
        Shape::Dict(t) => match v.as_object() {
            Some(m) if !is_codec_tag(m) => m
                .iter()
                .try_for_each(|(k, x)| check_shape(x, t, &format!("{at}[{k:?}]"))),
            _ => bad("dict"),
        },
        Shape::Record(fields) => match v.as_object() {
            Some(m) if !is_codec_tag(m) || fields.is_empty() => {
                for (k, x) in m {
                    let Some((_, t)) = fields.iter().find(|(n, _)| n == k) else {
                        return Err(format!("{at}: unknown field {k:?}"));
                    };
                    check_shape(x, t, &format!("{at}.{k}"))?;
                }
                Ok(())
            }
            _ => bad("record"),
        },
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn intermediate_results_validate_settled_modules_only() {
        let shapes = vec![
            (
                "kv".to_string(),
                parse_shape("{namespaces: dict[str, dict[str, bytes]]}").unwrap(),
            ),
            ("jobs".to_string(), parse_shape("{next_id: int}").unwrap()),
        ];
        let mid = json!({"kv": {"a": {"$bytes": ""}}, "jobs": {"next_id": 1}});
        let kv_pending: BTreeSet<&str> = ["kv"].into_iter().collect();
        assert_eq!(check_modules(&mid, &shapes, &kv_pending, "m1"), Ok(()));
        let err = check_modules(&mid, &shapes, &BTreeSet::new(), "m1").unwrap_err();
        assert!(
            err.contains("after m1") && err.contains("state.kv"),
            "{err}"
        );
        let bad_jobs = json!({"kv": {}, "jobs": {"next_id": 1, "bogus": 1}});
        let err = check_modules(&bad_jobs, &shapes, &kv_pending, "m1").unwrap_err();
        assert!(err.contains("state.jobs"), "{err}");
        assert!(check_modules(&json!([]), &shapes, &kv_pending, "m1").is_err());
        assert_eq!(module_of("kv.set"), "kv");
        assert_eq!(module_of("kv"), "kv");
    }

    #[test]
    fn semver_is_numeric_and_strict() {
        assert_eq!(semver("0.10.2"), Some((0, 10, 2)));
        assert!(semver("0.10.0") > semver("0.9.9"));
        for bad in ["1.2", "1.2.3.4", "01.2.3", "1.2.x", "", "1..2"] {
            assert_eq!(semver(bad), None, "{bad}");
        }
    }

    #[test]
    fn shapes_parse_and_check() {
        let jobs = parse_shape(
            "{next_id: int, jobs: dict[str, {kind: str, n: int, fail_at: Optional[int]}]}",
        )
        .unwrap();
        let ok = json!({"next_id": 2, "jobs": {"j1": {"kind": "sum", "n": 3, "fail_at": null}}});
        assert_eq!(check_shape(&ok, &jobs, "state.jobs"), Ok(()));
        assert_eq!(check_shape(&json!({}), &jobs, "state.jobs"), Ok(()));
        let unknown = json!({"next": 1});
        assert!(check_shape(&unknown, &jobs, "s")
            .unwrap_err()
            .contains("unknown field"));
        let kv = parse_shape("{namespaces: dict[str, dict[str, bytes]]}").unwrap();
        let good = json!({"namespaces": {"default": {"a": {"$bytes": "aGk="}}}});
        assert_eq!(check_shape(&good, &kv, "state.kv"), Ok(()));
        let flat = json!({"a": {"$bytes": "aGk="}});
        assert!(check_shape(&flat, &kv, "state.kv").is_err());
        let flat_kv = parse_shape("dict[str, bytes]").unwrap();
        assert_eq!(check_shape(&flat, &flat_kv, "state.kv"), Ok(()));
        assert!(check_shape(&good, &flat_kv, "state.kv").is_err());
        assert_eq!(parse_shape("{}").unwrap(), Shape::Record(vec![]));
        let u = parse_shape("{fail_at: int|None, tag: str | bytes}").unwrap();
        assert_eq!(
            check_shape(&json!({"fail_at": null, "tag": "x"}), &u, "s"),
            Ok(())
        );
        assert_eq!(
            check_shape(&json!({"fail_at": 3, "tag": {"$bytes": ""}}), &u, "s"),
            Ok(())
        );
        assert!(check_shape(&json!({"fail_at": "3"}), &u, "s").is_err());
        assert!(parse_shape("dict[int, str]").is_err());
        assert!(parse_shape("float").is_err());
        assert!(parse_shape("list[str] x").is_err());
    }
}
