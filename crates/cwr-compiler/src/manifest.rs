// Copyright Sidiora Labs Inc. CWR® 2026-2027
// SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0

//! Compiled contracts (`contracts.json`), the generate stage and the Build manifest
//! (`manifest.kvx`); compiler.md sections 2, 7 and 8, clauses 1.4, 4.5 and 4.6 (task 1.3).
//!
//! `contracts.json` is the complete compiled surface the kernel enforces and the diff compares.
//! It is canonical JSON (keys sorted bytewise, two-space indentation, `\n`, one final newline):
//!
//! | Key | Content |
//! |---|---|
//! | `format`, `schema_version` | `"cwr.contracts"`, 1 |
//! | `world` | `name`, `version`, `language`, `runtime`, `modules` (declared order) |
//! | `limits`, `clock`, `rng` | every value of `world.kvx` |
//! | `envelope` | the eleven codes in envelope order, each with `meaning`, `guidance` and whether it carries `retry_after` |
//! | `primitives` | one object per primitive, modules in `world.modules` order, primitives in declared order: `name`, `module`, `kind`, `side_effect`, `params` (ordered `[name, type]` pairs), `returns`, `preconditions` (`[expr, code]` in evaluation order), `effects`, `doc` |
//! | `adapters` | the same plus `idempotency`, `timeout_ms`, `endpoint`, `failures`, `content_guaranteed` (always false), and `undeclared_status` / `retry_after_ratio` when declared |
//! | `state` | module to canonical state shape of its `lib/` typed state records |
//! | `changelog` | every release newest first with its changes, so the manual and the recompile notice are functions of the Build |
//!
//! The manual and the SDK are rendered from this value (plus `manual/` prose for the manual), never
//! from the source model directly, so documented and enforced rules cannot diverge (clause 2.4).
//!
//! The Build manifest carries no timestamp, host, user, path or environment value. Its `compiler`
//! key is the crate version plus a digest of the generator templates and runtime files, so a template
//! change shows up as a different `compiler` value (compiler.md section 8).

use std::collections::BTreeMap;

use cwr_contract::{Code, FileClass};
use serde_json::{json, Map, Value};

use crate::diff::{canonical_pretty, state_shape};
use crate::generate::{manual, sdk};
use crate::parse::Parsed;
use crate::{Context, Diagnostic, Location, Stage, StageRun};

/// One-line meaning of each envelope code (world-source.md section 6).
pub fn meaning(code: Code) -> &'static str {
    match code {
        Code::NotFound => "The target the parameters name does not exist in the world.",
        Code::Locked => "The target is held by a lock the caller does not hold.",
        Code::Conflict => "A visible condition changed between the caller's check and this call, or the action would create a duplicate world object.",
        Code::Invalid => "Parameters fail the contract's types, ranges or declared validity preconditions.",
        Code::Limit => "A declared limit was reached: a world limit, a size rule or the per-turn budgets. The envelope names the limit.",
        Code::Unavailable => "An external endpoint could not be reached, returned an undeclared status, or a replay cassette has no entry.",
        Code::RateLimited => "An external endpoint throttled the call.",
        Code::Timeout => "An external call exceeded the contract's timeout_ms.",
        Code::Rejected => "An external endpoint refused the content for a reason it reported.",
        Code::DuplicateKey => "An idempotency key was reused with a different payload.",
        Code::InvariantViolation => "The kernel observed a rule outcome the contract does not allow: an undeclared code after preconditions passed, or an effect that did not hold.",
    }
}

/// What the generate stage produces; packaged by [`crate::package`].
#[derive(Debug, Clone)]
pub struct Generated {
    /// The contracts document `contracts.json` serialises.
    pub contracts: Value,
    /// `contracts.json` bytes.
    pub contracts_json: String,
    /// `manual.md` bytes.
    pub manual: String,
    /// Files under `sdk/`, by path relative to `sdk/`.
    pub sdk: BTreeMap<String, String>,
}

fn module_rank(modules: &[String], name: &str) -> usize {
    let m = name.split('.').next().unwrap_or(name);
    modules.iter().position(|x| x == m).unwrap_or(usize::MAX)
}

/// The compiled contracts of a parsed World Source (the parse stage must have produced a model).
pub fn contracts_value(parsed: &Parsed) -> Result<Value, String> {
    let model = parsed
        .model
        .as_ref()
        .ok_or("the World Source has no typed model")?;
    let w = &model.world;
    let modules = w.world.modules.clone();
    let pre = |ps: &[cwr_contract::Precondition]| -> Value {
        Value::Array(
            ps.iter()
                .map(|c| json!([c.expr().source(), c.code().as_str()]))
                .collect(),
        )
    };
    let params = |ps: &cwr_contract::Params| -> Value {
        Value::Array(
            ps.0.iter()
                .map(|(n, t)| json!([n, t.to_string()]))
                .collect(),
        )
    };

    let mut files: Vec<&cwr_contract::ContractFile> = model.contracts.iter().collect();
    files.sort_by_key(|f| (module_rank(&modules, &f.module), f.path.clone()));
    let mut primitives = Vec::new();
    for f in files {
        for p in &f.primitives {
            primitives.push(json!({
                "name": p.name,
                "module": f.module,
                "kind": "primitive",
                "side_effect": p.side_effect.as_str(),
                "params": params(&p.params),
                "returns": p.returns.to_string(),
                "preconditions": pre(&p.preconditions),
                "effects": p.effects.iter().map(|e| e.source().to_string()).collect::<Vec<_>>(),
                "doc": p.doc,
            }));
        }
    }
    let mut afiles: Vec<&cwr_contract::AdapterFile> = model.adapters.iter().collect();
    afiles.sort_by_key(|f| (module_rank(&modules, &f.module), f.path.clone()));
    let mut adapters = Vec::new();
    for f in afiles {
        for a in &f.adapters {
            let mut entry = json!({
                "name": a.name,
                "module": f.module,
                "kind": "adapter",
                "side_effect": a.side_effect.as_str(),
                "params": params(&a.params),
                "returns": a.returns.to_string(),
                "preconditions": pre(&a.preconditions),
                "effects": [],
                "doc": a.doc,
                "idempotency": a.idempotency.as_str(),
                "timeout_ms": a.timeout_ms.get(),
                "endpoint": a.endpoint,
                "failures": a.failures.iter().map(|c| c.as_str()).collect::<Vec<_>>(),
                "content_guaranteed": a.content_guaranteed,
            });
            // Optional normalisation keys (kernel.md section 7), emitted only when declared.
            if let Some(c) = a.undeclared_status {
                entry["undeclared_status"] = json!(c.as_str());
            }
            if let Some(r) = a.retry_after_ratio {
                entry["retry_after_ratio"] = json!(r.get());
            }
            adapters.push(entry);
        }
    }
    let envelope: Vec<Value> = Code::ALL
        .iter()
        .map(|c| json!({"code": c.as_str(), "meaning": meaning(*c), "guidance": c.guidance(), "retry_after": *c == Code::RateLimited}))
        .collect();
    let mut state = Map::new();
    for (_, file) in parsed.of_class(FileClass::Lib) {
        if let Some(m) = &file.module {
            state.insert(m.clone(), Value::String(state_shape(&file.text)));
        }
    }
    let mut releases: Vec<&cwr_contract::Release> = model.changelog.releases.iter().collect();
    releases.sort_by_key(|r| std::cmp::Reverse(r.version));
    let changelog: Vec<Value> = releases
        .iter()
        .map(|r| {
            let changes: Vec<Value> = r
                .changes
                .iter()
                .filter_map(|id| model.changelog.change(id))
                .map(|c| json!({"id": c.id, "kind": c.kind.as_str(), "target": c.target, "before": c.before, "after": c.after, "migration": c.migration}))
                .collect();
            json!({"version": r.version.to_string(), "date": r.date, "summary": r.summary, "changes": changes})
        })
        .collect();
    let l = &w.limits;
    Ok(json!({
        "format": "cwr.contracts",
        "schema_version": 1,
        "world": {
            "name": w.world.name,
            "version": w.world.version.to_string(),
            "language": "python",
            "runtime": "3.12",
            "modules": modules,
        },
        "limits": {
            "cpu_ms": l.cpu_ms.get(),
            "wall_ms": l.wall_ms.get(),
            "memory_mb": l.memory_mb.get(),
            "primitive_calls": l.primitive_calls.get(),
            "adapter_calls": l.adapter_calls.get(),
        },
        "clock": {"tick_per_primitive": w.clock.tick_per_primitive.get(), "tick_per_turn": w.clock.tick_per_turn.get()},
        "rng": {"seed_visible": w.rng.seed_visible},
        "envelope": envelope,
        "primitives": primitives,
        "adapters": adapters,
        "state": Value::Object(state),
        "changelog": changelog,
    }))
}

/// `manual/<name>` prose files of the source, by file name (`intro.md`, ...).
pub fn prose(parsed: &Parsed) -> BTreeMap<String, String> {
    parsed
        .of_class(FileClass::Manual)
        .filter_map(|(path, f)| {
            path.strip_prefix("manual/")
                .map(|n| (n.to_string(), f.text.clone()))
        })
        .collect()
}

/// Renders every generated file from the compiled contracts and the manual prose. Pure.
pub fn generate(contracts: Value, prose: &BTreeMap<String, String>) -> Generated {
    let contracts_json = canonical_pretty(&contracts);
    let contracts_sha = cwr_kernel::sha256_hex(contracts_json.as_bytes());
    let manual = manual::render(&contracts, &contracts_sha, prose);
    let sdk = sdk::render(&contracts, &contracts_sha);
    Generated {
        contracts,
        contracts_json,
        manual,
        sdk,
    }
}

/// The generate stage.
pub fn run_stage(cx: &mut Context) -> StageRun {
    let parsed = match crate::prerequisite(&cx.parsed, Stage::Generate, Stage::Parse) {
        Ok(p) => p,
        Err(d) => return StageRun::Failed(d),
    };
    match contracts_value(parsed) {
        Ok(v) => {
            cx.generated = Some(generate(v, &prose(parsed)));
            StageRun::Passed
        }
        Err(m) => StageRun::Failed(vec![Diagnostic::new(
            Stage::Generate,
            None,
            "generator",
            Location {
                file: "world.kvx".into(),
                line: 1,
                col: 1,
                section: None,
                key: None,
            },
            format!("compiler defect: {m}"),
        )]),
    }
}

/// The compiler revision written into `manifest.kvx`: crate version plus a digest of the
/// templates and runtime files the generators embed.
pub fn compiler_revision() -> String {
    let mut h = String::new();
    for (name, body) in sdk::embedded_files() {
        h.push_str(name);
        h.push('\0');
        h.push_str(body);
        h.push('\0');
    }
    h.push_str(manual::GENERATOR_REVISION);
    format!(
        "cwr-compiler {}+gen.{}",
        env!("CARGO_PKG_VERSION"),
        &cwr_kernel::sha256_hex(h.as_bytes())[..16]
    )
}

/// Fields of `manifest.kvx`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Manifest {
    /// World name from `world.kvx`.
    pub name: String,
    /// World version from `world.kvx`.
    pub version: String,
    /// `sha256:<hex>` of the previous Build, if any.
    pub previous: Option<String>,
    /// The [`compiler_revision`] that produced the Build.
    pub compiler: String,
    /// Runtime name from `world.kvx`.
    pub runtime: String,
    /// Experimental flag name for an `xbuild/` Build; None for a control Build.
    pub experimental_flag: Option<String>,
}

fn lit(v: &Value) -> String {
    v.to_string()
}

impl Manifest {
    /// The manifest of a control Build whose world fields come from `contracts`; a field
    /// missing from `contracts` is the empty string.
    pub fn from_contracts(contracts: &Value, previous: Option<String>) -> Manifest {
        let s = |k: &str| contracts["world"][k].as_str().unwrap_or("").to_string();
        Manifest {
            name: s("name"),
            version: s("version"),
            previous,
            compiler: compiler_revision(),
            runtime: s("runtime"),
            experimental_flag: None,
        }
    }

    /// `manifest.kvx` text; `hash` is `None` for the bytes that enter the Build hash, and the
    /// `sha256:<hex>` value for the file written last.
    pub fn render(&self, hash: Option<&str>) -> String {
        let mut out = String::new();
        out.push_str("# CWR Build manifest, generated by the CWR compiler (spec/cwr/compiler.md section 7).\n");
        out.push_str("# Every key except the build hash enters the Build hash; the hash key is written last.\n\n");
        out.push_str("[meta]\nformat = \"cwr.build-manifest\"\nschema_version = 1\n\n[build]\n");
        let opt = |o: &Option<String>| {
            o.as_ref()
                .map(|s| lit(&json!(s)))
                .unwrap_or_else(|| "null".into())
        };
        out.push_str(&format!("name = {}\n", lit(&json!(self.name))));
        out.push_str(&format!("version = {}\n", lit(&json!(self.version))));
        out.push_str(&format!("previous = {}\n", opt(&self.previous)));
        out.push_str(&format!("compiler = {}\n", lit(&json!(self.compiler))));
        out.push_str(&format!("runtime = {}\n", lit(&json!(self.runtime))));
        out.push_str(&format!(
            "experimental = {}\n",
            self.experimental_flag.is_some()
        ));
        out.push_str(&format!(
            "experimental_flag = {}\n",
            opt(&self.experimental_flag)
        ));
        if let Some(h) = hash {
            out.push_str(&format!("hash = {}\n", lit(&json!(h))));
        }
        out
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn manifest_hash_line_is_last_and_only_difference() {
        let m = Manifest {
            name: "w".into(),
            version: "0.1.0".into(),
            previous: None,
            compiler: "c".into(),
            runtime: "3.12".into(),
            experimental_flag: None,
        };
        let a = m.render(None);
        let b = m.render(Some("sha256:00"));
        assert!(b.starts_with(&a));
        assert_eq!(&b[a.len()..], "hash = \"sha256:00\"\n");
        assert!(!a.lines().any(|l| l.trim_start().starts_with("hash")));
    }

    #[test]
    fn compiler_revision_is_stable() {
        assert_eq!(compiler_revision(), compiler_revision());
        assert!(compiler_revision().starts_with("cwr-compiler "));
    }
}
