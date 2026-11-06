// Copyright Sidiora Labs Inc. CWR® 2026-2027
// SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0

//! ContractDiff (compiler.md section 5, clause 4.3) between the compiled surface of this
//! source and that of the previous Build, and the diff pipeline stage.
//!
//! The compared surface: primitive and adapter names, `side_effect`, `params`, `returns`,
//! ordered preconditions with codes, effects, adapter `idempotency`, `timeout_ms`, `endpoint`,
//! `failures`, `undeclared_status` and `retry_after_ratio` (absent compares as its default), every value in `limits` and `clock`, `rng.seed_visible`, and the state shape
//! declared by `lib/`'s typed state records. `doc` strings are compared separately into
//! `doc_entries` so that a documentation-only diff is told apart from a rule diff.
//!
//! Entry kinds and minimum bumps (world-source.md section 11):
//!
//! | Difference | kind | bump |
//! |---|---|---|
//! | name only in this source | `primitive_added` | minor |
//! | name only in the previous Build | `primitive_removed` | major |
//! | old preconditions are an ordered subsequence of the new ones | `precondition_added` | major |
//! | new preconditions are an ordered subsequence of the old ones | `precondition_removed` | minor |
//! | any other precondition difference (expression or code) | `precondition_changed` | major |
//! | `side_effect` or `returns` differs | `precondition_changed` (field names which) | major |
//! | `effects` differ | `effect_changed` | major |
//! | adapter `failures` differ as a set | `failure_changed` | major |
//! | `idempotency`, `timeout_ms`, `endpoint`, `undeclared_status` or `retry_after_ratio` differs | `adapter_changed` | major |
//! | a `limits`, `clock` or `rng` value differs | `limit_changed` | major when stricter (smaller limit, larger tick, seed hidden), minor otherwise |
//! | a module's typed state record differs | `state_shape` | major |
//! | `params` differ | kind `null`: world-source.md section 11 has no change kind for a parameter list; see [`crate::changelog_gate`] for how it is covered | major |
//! | `doc` differs | `doc_only` (in `doc_entries`) | patch |
//! | a `manual/` prose region differs | `doc_only` (in `prose_entries`) | patch |
//!
//! `manual/` prose (world-source.md section 11: `doc_only` is "only `doc` strings or `manual/`
//! prose changed") is compared as the four regions the manual generator merges it into
//! ([`PROSE_REGIONS`]): `manual/intro.md`, `manual/conventions.md`, `manual/examples.md` (with
//! any additional prose file, which the generator renders after it in the same section) and
//! `manual/types.md`. A region is the prose exactly as rendered into `manual.md` (headings
//! demoted). For a source it is rendered from `manual/`; for a Build it is cut out of the
//! hash-verified `manual.md` by rendering the Build's own `contracts.json` with a marker in place
//! of each region and matching the fixed text between the markers, so a source and a Build of
//! the same prose give identical regions. A `manual.md` that does not have the generator's layout
//! (a hand-written fixture or another generator revision) yields no prose surface, and prose is
//! then not compared. Prose entries are reported under their own key, `prose_entries`, so the
//! entry lists of compiler.md section 5 (`entries`, `doc_entries`, `doc_only_entries`) keep
//! their meaning; they count towards the derived bump and are gated like doc entries.
//!
//! Entries are sorted bytewise by target, then field, then kind, and serialised as canonical
//! JSON (sorted keys, two-space indentation, `\n` line ends, one final newline), so two hosts
//! produce identical bytes.
//!
//! The previous surface comes from a verified Build directory (its `contracts.json`, its
//! `manifest.kvx` and the byte-copied `lib/`), or, for authoring and for `cwr diff`, from a
//! second World Source compiled through lint by this compiler.

use std::collections::{BTreeMap, BTreeSet};
use std::fmt::Write as _;
use std::fs;
use std::path::{Path, PathBuf};

use cwr_contract::kvx::{self, Json};
use cwr_contract::{Bump, ChangeKind, Changelog, FileClass, Version};
use serde_json::{json, Map, Value};

use crate::parse::Parsed;
use crate::version::{self, bump_str};
use crate::{Context, Diagnostic, Location, Options, Stage, StageRun};

// ---------------------------------------------------------------------------------------------
// Compiled surface
// ---------------------------------------------------------------------------------------------

/// One primitive or adapter contract as the diff compares it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ContractSurface {
    /// Primitive or adapter name.
    pub name: String,
    /// Whether this is an adapter contract.
    pub adapter: bool,
    /// Declared side-effect class.
    pub side_effect: String,
    /// Declared order.
    pub params: Vec<(String, String)>,
    /// Declared return type.
    pub returns: String,
    /// `(expression source, failure code)` in declared order.
    pub preconditions: Vec<(String, String)>,
    /// Declared effect expressions in declared order.
    pub effects: Vec<String>,
    /// Documentation string, compared separately as `doc_only`.
    pub doc: String,
    /// Adapters: declared idempotency.
    pub idempotency: Option<String>,
    /// Adapters: declared timeout in milliseconds.
    pub timeout_ms: Option<u64>,
    /// Adapters: declared endpoint.
    pub endpoint: Option<String>,
    /// Adapters: declared failure codes.
    pub failures: Option<Vec<String>>,
    /// Adapters: the selected undeclared-status fallback, `UNAVAILABLE` when not declared.
    pub undeclared_status: Option<String>,
    /// Adapters: world ticks per service `retry_after` unit, 1 when not declared.
    pub retry_after_ratio: Option<u64>,
}

impl ContractSurface {
    fn params_value(&self) -> Value {
        let mut m = Map::new();
        for (k, t) in &self.params {
            m.insert(k.clone(), Value::String(t.clone()));
        }
        Value::Object(m)
    }

    fn preconditions_value(&self) -> Value {
        Value::Array(
            self.preconditions
                .iter()
                .map(|(e, c)| json!([e, c]))
                .collect(),
        )
    }

    fn to_value(&self) -> Value {
        let mut v = json!({
            "name": self.name,
            "kind": if self.adapter { "adapter" } else { "primitive" },
            "side_effect": self.side_effect,
            "params": Value::Array(self.params.iter().map(|(k, t)| json!([k, t])).collect()),
            "returns": self.returns,
            "preconditions": self.preconditions_value(),
            "effects": self.effects,
            "doc": self.doc,
        });
        if let (true, Some(o)) = (self.adapter, v.as_object_mut()) {
            o.insert("idempotency".into(), json!(self.idempotency));
            o.insert("timeout_ms".into(), json!(self.timeout_ms));
            o.insert("endpoint".into(), json!(self.endpoint));
            o.insert("failures".into(), json!(self.failures));
            o.insert("undeclared_status".into(), json!(self.undeclared_status));
            o.insert("retry_after_ratio".into(), json!(self.retry_after_ratio));
        }
        v
    }
}

/// The compiled surface of a World Source or a Build.
#[derive(Debug, Clone)]
pub struct Surface {
    /// World name.
    pub name: String,
    /// Declared world version.
    pub version: Version,
    /// Contracts by name.
    pub contracts: BTreeMap<String, ContractSurface>,
    /// `limits.<k>`, `clock.<k>` and `rng.<k>` values.
    pub settings: BTreeMap<String, Value>,
    /// Module name to canonical state shape, for every module with a `lib/` file.
    pub state: BTreeMap<String, String>,
    /// `manual/` prose by region target (`manual/intro.md`, ...), as rendered into `manual.md`;
    /// `None` when a Build's `manual.md` does not have the generator's layout.
    pub prose: Option<BTreeMap<String, String>>,
    /// Whether parameter order is known (true for sources; false when a Build's
    /// `contracts.json` stores parameters as a JSON object, whose key order is not normative).
    pub params_ordered: bool,
    /// `sha256:<hex>` for a Build, `source:sha256:<hex of the canonical surface>` for a source.
    pub identity: String,
    /// Where the surface came from, for messages.
    pub origin: String,
}

impl Surface {
    /// Canonical value of the compared surface (not including identity or origin).
    pub fn to_value(&self) -> Value {
        json!({
            "name": self.name,
            "version": self.version.to_string(),
            "contracts": self.contracts.values().map(ContractSurface::to_value).collect::<Vec<_>>(),
            "settings": self.settings,
            "state": self.state,
        })
    }

    /// The surface of a parsed World Source (the parse stage must have produced a typed model).
    pub fn from_parsed(parsed: &Parsed, origin: &str) -> Result<Surface, String> {
        let model = parsed.model.as_ref().ok_or_else(|| {
            "the World Source has no typed model (parse did not accept it)".to_string()
        })?;
        let mut contracts = BTreeMap::new();
        for cf in &model.contracts {
            for p in &cf.primitives {
                contracts.insert(
                    p.name.clone(),
                    ContractSurface {
                        name: p.name.clone(),
                        adapter: false,
                        side_effect: p.side_effect.as_str().to_string(),
                        params: p
                            .params
                            .0
                            .iter()
                            .map(|(n, t)| (n.clone(), t.to_string()))
                            .collect(),
                        returns: p.returns.to_string(),
                        preconditions: p
                            .preconditions
                            .iter()
                            .map(|c| (c.expr().source().to_string(), c.code().as_str().to_string()))
                            .collect(),
                        effects: p.effects.iter().map(|e| e.source().to_string()).collect(),
                        doc: p.doc.clone(),
                        idempotency: None,
                        timeout_ms: None,
                        endpoint: None,
                        failures: None,
                        undeclared_status: None,
                        retry_after_ratio: None,
                    },
                );
            }
        }
        for af in &model.adapters {
            for a in &af.adapters {
                contracts.insert(
                    a.name.clone(),
                    ContractSurface {
                        name: a.name.clone(),
                        adapter: true,
                        side_effect: a.side_effect.as_str().to_string(),
                        params: a
                            .params
                            .0
                            .iter()
                            .map(|(n, t)| (n.clone(), t.to_string()))
                            .collect(),
                        returns: a.returns.to_string(),
                        preconditions: a
                            .preconditions
                            .iter()
                            .map(|c| (c.expr().source().to_string(), c.code().as_str().to_string()))
                            .collect(),
                        effects: Vec::new(),
                        doc: a.doc.clone(),
                        idempotency: Some(a.idempotency.as_str().to_string()),
                        timeout_ms: Some(a.timeout_ms.get()),
                        endpoint: Some(a.endpoint.clone()),
                        failures: Some(a.failures.iter().map(|c| c.as_str().to_string()).collect()),
                        undeclared_status: Some(a.undeclared_code().as_str().to_string()),
                        retry_after_ratio: Some(a.retry_ratio()),
                    },
                );
            }
        }
        let w = &model.world;
        let mut settings = BTreeMap::new();
        let l = &w.limits;
        for (k, v) in [
            ("cpu_ms", l.cpu_ms),
            ("wall_ms", l.wall_ms),
            ("memory_mb", l.memory_mb),
            ("primitive_calls", l.primitive_calls),
            ("adapter_calls", l.adapter_calls),
        ] {
            settings.insert(format!("limits.{k}"), json!(v.get()));
        }
        settings.insert(
            "clock.tick_per_primitive".into(),
            json!(w.clock.tick_per_primitive.get()),
        );
        settings.insert(
            "clock.tick_per_turn".into(),
            json!(w.clock.tick_per_turn.get()),
        );
        settings.insert("rng.seed_visible".into(), json!(w.rng.seed_visible));
        let mut state = BTreeMap::new();
        for (path, file) in parsed.of_class(FileClass::Lib) {
            if let Some(m) = &file.module {
                let shape = state_shape(&file.text).map_err(|e| format!("{path}: {e}"))?;
                state.insert(m.clone(), shape);
            }
        }
        let prose = source_prose(&crate::manifest::prose(parsed));
        let mut s = Surface {
            name: w.world.name.clone(),
            version: w.world.version,
            contracts,
            settings,
            state,
            prose,
            params_ordered: true,
            identity: String::new(),
            origin: origin.to_string(),
        };
        s.identity = format!(
            "source:sha256:{}",
            cwr_kernel::sha256_hex(cwr_kernel::canonical_json(&s.to_value()).as_bytes())
        );
        Ok(s)
    }

    /// The surface of a Build directory, after verifying its hash (compiler.md section 7).
    pub fn from_build(dir: &Path) -> Result<Surface, String> {
        let computed = cwr_kernel::Build::compute_hash(dir)
            .map_err(|e| format!("Build {}: {e}", dir.display()))?;
        let manifest_text = fs::read_to_string(dir.join("manifest.kvx"))
            .map_err(|e| format!("Build {}: cannot read manifest.kvx: {e}", dir.display()))?;
        let manifest = kvx::parse("manifest.kvx", &manifest_text).map_err(|e| {
            format!(
                "Build {}: manifest.kvx does not parse: {}",
                dir.display(),
                e.iter()
                    .map(|x| x.to_string())
                    .collect::<Vec<_>>()
                    .join("; ")
            )
        })?;
        let mkey = |k: &str| {
            manifest
                .sections
                .iter()
                .find_map(|s| s.get(k))
                .map(|e| e.value.value.to_value())
        };
        let recorded = cwr_kernel::state::manifest_recorded_hash(manifest_text.as_bytes());
        match recorded {
            Some(r) if r == computed => {}
            Some(r) => return Err(format!("Build {} hashes to sha256:{computed} but its manifest records sha256:{r}; refusing an unverifiable Build", dir.display())),
            None => return Err(format!("Build {}: manifest.kvx records no hash", dir.display())),
        }
        let base = dir.file_name().and_then(|n| n.to_str()).unwrap_or("");
        if is_hex64(base) && base != computed {
            return Err(format!(
                "Build directory {} hashes to sha256:{computed}, not to its name",
                dir.display()
            ));
        }
        let ctext = fs::read_to_string(dir.join("contracts.json"))
            .map_err(|e| format!("Build {}: cannot read contracts.json: {e}", dir.display()))?;
        // The KVX literal reader skips spaces only; raw newlines and tabs cannot occur inside
        // JSON strings, so turning them into spaces keeps the document's meaning and key order.
        let flat: String = ctext
            .trim_end()
            .chars()
            .map(|c| {
                if matches!(c, '\n' | '\r' | '\t') {
                    ' '
                } else {
                    c
                }
            })
            .collect();
        let cj = kvx::parse_json_str(&flat)
            .map_err(|e| {
                format!(
                    "Build {}: contracts.json: {} (column {})",
                    dir.display(),
                    e.message,
                    e.col
                )
            })?
            .value;
        let get = |j: &Json, k: &str| -> Option<Json> {
            match j {
                Json::Object(m) => m.iter().find(|(n, _)| n == k).map(|(_, v)| v.value.clone()),
                _ => None,
            }
        };
        let world = get(&cj, "world");
        let find = |k: &str| get(&cj, k).or_else(|| world.as_ref().and_then(|w| get(w, k)));
        let name = mkey("name")
            .and_then(|v| v.as_str().map(str::to_string))
            .or_else(|| {
                find("name").and_then(|v| if let Json::Str(s) = v { Some(s) } else { None })
            })
            .ok_or_else(|| format!("Build {}: no world name", dir.display()))?;
        let version_s = mkey("version")
            .and_then(|v| v.as_str().map(str::to_string))
            .or_else(|| {
                find("version").and_then(|v| if let Json::Str(s) = v { Some(s) } else { None })
            })
            .ok_or_else(|| format!("Build {}: no version", dir.display()))?;
        let version: Version = version_s
            .parse()
            .map_err(|e| format!("Build {}: {e}", dir.display()))?;
        let mut contracts = BTreeMap::new();
        let mut params_ordered = true;
        for list in ["primitives", "adapters"] {
            let Some(Json::Array(items)) = get(&cj, list) else {
                continue;
            };
            for item in items {
                let c = contract_from_json(&item.value, list == "adapters", &mut params_ordered)
                    .map_err(|m| format!("Build {}: contracts.json {list}: {m}", dir.display()))?;
                contracts.insert(c.name.clone(), c);
            }
        }
        let mut settings = BTreeMap::new();
        for section in ["limits", "clock", "rng"] {
            if let Some(Json::Object(members)) = find(section) {
                for (k, v) in members {
                    settings.insert(format!("{section}.{k}"), v.value.to_value());
                }
            }
        }
        let mut state = BTreeMap::new();
        let lib = dir.join("lib");
        if lib.is_dir() {
            let mut names: Vec<_> = fs::read_dir(&lib)
                .map_err(|e| format!("Build {}: lib/: {e}", dir.display()))?
                .filter_map(|e| e.ok())
                .collect();
            names.sort_by_key(|e| e.file_name());
            for e in names {
                let n = e.file_name().to_string_lossy().into_owned();
                if let Some(m) = n.strip_suffix(".py") {
                    let text = fs::read_to_string(e.path())
                        .map_err(|err| format!("Build {}: lib/{n}: {err}", dir.display()))?;
                    let shape = state_shape(&text)
                        .map_err(|err| format!("Build {}: lib/{n}: {err}", dir.display()))?;
                    state.insert(m.to_string(), shape);
                }
            }
        }
        let flag = match mkey("experimental") {
            Some(Value::Bool(true)) => {
                mkey("experimental_flag").and_then(|v| v.as_str().map(str::to_string))
            }
            _ => None,
        };
        let prose = build_prose(dir, flag.as_deref());
        Ok(Surface {
            name,
            version,
            contracts,
            settings,
            state,
            prose,
            params_ordered,
            identity: format!("sha256:{computed}"),
            origin: dir.display().to_string(),
        })
    }

    /// The `previous` key of a Build manifest, if any (`sha256:<hex>`).
    pub fn manifest_previous(dir: &Path) -> Option<String> {
        let text = fs::read_to_string(dir.join("manifest.kvx")).ok()?;
        let doc = kvx::parse("manifest.kvx", &text).ok()?;
        let v = doc
            .sections
            .iter()
            .find_map(|s| s.get("previous"))
            .map(|e| e.value.value.to_value())?;
        v.as_str().map(str::to_string)
    }
}

// ---------------------------------------------------------------------------------------------
// manual/ prose regions
// ---------------------------------------------------------------------------------------------

/// The prose regions of the manual, as `(prose file, target)` in manual order. Additional prose
/// files are rendered after `examples.md` in section 2 and belong to its region.
pub const PROSE_REGIONS: [(&str, &str); 4] = [
    ("intro.md", "manual/intro.md"),
    ("conventions.md", "manual/conventions.md"),
    ("examples.md", "manual/examples.md"),
    ("types.md", "manual/types.md"),
];

fn prose_marker(file: &str) -> String {
    format!("\u{1}CWR-PROSE-REGION {file}\u{1}")
}

/// Cuts the prose regions out of `manual`, given `probe`: the same manual rendered with a marker
/// in place of every region. `None` when `manual` does not have the probe's layout.
fn cut_regions(manual: &str, probe: &str) -> Option<BTreeMap<String, String>> {
    let mut fixed: Vec<&str> = Vec::new();
    let mut rest = probe;
    for (file, _) in PROSE_REGIONS {
        let m = prose_marker(file);
        let i = rest.find(&m)?;
        fixed.push(&rest[..i]);
        rest = &rest[i + m.len()..];
    }
    fixed.push(rest);
    let mut cursor = manual.strip_prefix(fixed[0]).map(|_| fixed[0].len())?;
    let mut out = BTreeMap::new();
    for (i, (_, target)) in PROSE_REGIONS.iter().enumerate() {
        // The fixed text after a region starts with the newlines that close the region; prose
        // may be empty, so those newlines are matched as part of the region.
        let after = fixed[i + 1].trim_start_matches('\n');
        let end = if i + 1 == PROSE_REGIONS.len() {
            let e = manual.len().checked_sub(after.len())?;
            if e < cursor || !manual.ends_with(after) {
                return None;
            }
            e
        } else {
            cursor + manual[cursor..].find(after)?
        };
        out.insert(
            target.to_string(),
            manual[cursor..end].trim_end_matches('\n').to_string(),
        );
        cursor = end + after.len();
    }
    Some(out)
}

fn probe_prose() -> BTreeMap<String, String> {
    PROSE_REGIONS
        .iter()
        .map(|(f, _)| (f.to_string(), prose_marker(f)))
        .collect()
}

/// The prose regions of a World Source's `manual/` files. The regions do not depend on the
/// contracts, so the manual is rendered over an empty surface.
pub fn source_prose(prose: &BTreeMap<String, String>) -> Option<BTreeMap<String, String>> {
    let empty = json!({"world": {"name": "", "version": "", "modules": []}});
    let render = crate::generate::manual::render;
    cut_regions(
        &render(&empty, "", prose),
        &render(&empty, "", &probe_prose()),
    )
}

/// The prose regions of a Build's `manual.md`, cut out with the Build's own `contracts.json`.
/// `flag` is the experimental flag of the Build, whose manual transform the probe repeats.
fn build_prose(dir: &Path, flag: Option<&str>) -> Option<BTreeMap<String, String>> {
    let bytes = fs::read(dir.join("contracts.json")).ok()?;
    let contracts: Value = serde_json::from_slice(&bytes).ok()?;
    let manual = fs::read_to_string(dir.join("manual.md")).ok()?;
    let sha = cwr_kernel::sha256_hex(&bytes);
    let mut probe = crate::generate::manual::render(&contracts, &sha, &probe_prose());
    if flag == Some(crate::experimental::one_primitive::FLAG) {
        probe = crate::experimental::one_primitive::with_sentence(&probe).ok()?;
    }
    cut_regions(&manual, &probe)
}

fn is_hex64(s: &str) -> bool {
    s.len() == 64
        && s.bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}

fn json_str(j: &Json) -> Option<String> {
    if let Json::Str(s) = j {
        Some(s.clone())
    } else {
        None
    }
}

fn contract_from_json(
    j: &Json,
    adapter_list: bool,
    params_ordered: &mut bool,
) -> Result<ContractSurface, String> {
    let Json::Object(m) = j else {
        return Err("entry is not an object".into());
    };
    let get = |k: &str| m.iter().find(|(n, _)| n == k).map(|(_, v)| &v.value);
    let name = get("name")
        .and_then(json_str)
        .ok_or("entry without a name")?;
    let side_effect = get("side_effect").and_then(json_str).unwrap_or_default();
    let mut params = Vec::new();
    match get("params") {
        Some(Json::Object(ps)) => {
            *params_ordered = false;
            for (k, v) in ps {
                params.push((k.clone(), json_str(&v.value).unwrap_or_default()));
            }
        }
        Some(Json::Array(ps)) => {
            for p in ps {
                if let Json::Array(pair) = &p.value {
                    if let (Some(k), Some(t)) = (
                        pair.first().and_then(|n| json_str(&n.value)),
                        pair.get(1).and_then(|n| json_str(&n.value)),
                    ) {
                        params.push((k, t));
                    }
                }
            }
        }
        _ => {}
    }
    let mut preconditions = Vec::new();
    if let Some(Json::Array(ps)) = get("preconditions") {
        for p in ps {
            match &p.value {
                Json::Array(pair) if pair.len() == 2 => {
                    let e = json_str(&pair[0].value).unwrap_or_default();
                    let c = json_str(&pair[1].value).unwrap_or_default();
                    preconditions.push((e, c));
                }
                Json::Object(o) => {
                    let f = |k: &str| {
                        o.iter()
                            .find(|(n, _)| n == k)
                            .and_then(|(_, v)| json_str(&v.value))
                            .unwrap_or_default()
                    };
                    preconditions.push((f("expr"), f("failure")));
                }
                _ => return Err(format!("{name}: malformed precondition")),
            }
        }
    }
    let strings = |k: &str| -> Vec<String> {
        match get(k) {
            Some(Json::Array(xs)) => xs.iter().filter_map(|x| json_str(&x.value)).collect(),
            _ => Vec::new(),
        }
    };
    let adapter = adapter_list || side_effect == "external";
    Ok(ContractSurface {
        effects: strings("effects"),
        doc: get("doc").and_then(json_str).unwrap_or_default(),
        returns: get("returns").and_then(json_str).unwrap_or_default(),
        idempotency: if adapter {
            get("idempotency").and_then(json_str)
        } else {
            None
        },
        timeout_ms: if adapter {
            match get("timeout_ms") {
                Some(Json::Int(i)) if *i >= 0 => Some(*i as u64),
                _ => None,
            }
        } else {
            None
        },
        endpoint: if adapter {
            get("endpoint").and_then(json_str)
        } else {
            None
        },
        failures: if adapter {
            Some(strings("failures"))
        } else {
            None
        },
        undeclared_status: if adapter {
            Some(
                get("undeclared_status")
                    .and_then(json_str)
                    .unwrap_or_else(|| "UNAVAILABLE".to_string()),
            )
        } else {
            None
        },
        retry_after_ratio: if adapter {
            match get("retry_after_ratio") {
                Some(Json::Int(i)) if *i > 0 => Some(*i as u64),
                _ => Some(1),
            }
        } else {
            None
        },
        name,
        adapter,
        side_effect,
        params,
        preconditions,
    })
}

// ---------------------------------------------------------------------------------------------
// State shape from lib/ typed state records
// ---------------------------------------------------------------------------------------------

#[derive(Debug, Default)]
struct Records {
    classes: BTreeMap<String, Vec<(String, String)>>,
    aliases: BTreeMap<String, String>,
    initial: Option<String>,
    /// Top-level classes that are neither a TypedDict nor a dataclass, with their line: the
    /// scanner cannot read their fields, so a state type that names one is refused.
    unreadable: BTreeMap<String, usize>,
}

fn is_ident(s: &str) -> bool {
    let mut cs = s.chars();
    matches!(cs.next(), Some(c) if c.is_ascii_alphabetic() || c == '_')
        && cs.all(|c| c.is_ascii_alphanumeric() || c == '_')
}

/// One logical line of a `lib/` file: its first physical line, its indentation and its text,
/// with physical lines joined while a bracket is open or after a trailing `\`, comments
/// removed and string literals kept (a newline inside a triple-quoted string as a space).
struct LogicalLine {
    line: usize,
    indent: usize,
    text: String,
}

fn logical_lines(text: &str) -> Vec<LogicalLine> {
    let chars: Vec<char> = text.chars().collect();
    let mut out = Vec::new();
    let mut cur = String::new();
    let (mut line, mut start, mut indent) = (1usize, 1usize, 0usize);
    let mut at_start = true;
    let mut depth = 0usize;
    let mut i = 0usize;
    let flush = |cur: &mut String, out: &mut Vec<LogicalLine>, start: usize, indent: usize| {
        if !cur.trim().is_empty() {
            out.push(LogicalLine {
                line: start,
                indent,
                text: std::mem::take(cur),
            });
        }
        cur.clear();
    };
    while let Some(&c) = chars.get(i) {
        if at_start {
            match c {
                ' ' | '\t' => {
                    indent += 1;
                    i += 1;
                    continue;
                }
                '\n' => {
                    line += 1;
                    indent = 0;
                    i += 1;
                    continue;
                }
                '#' => {
                    while chars.get(i).is_some_and(|&c| c != '\n') {
                        i += 1;
                    }
                    continue;
                }
                _ => {
                    at_start = false;
                    start = line;
                }
            }
        }
        match c {
            '#' => {
                while chars.get(i).is_some_and(|&c| c != '\n') {
                    i += 1;
                }
                continue;
            }
            '\\' if chars.get(i + 1) == Some(&'\n') => {
                cur.push(' ');
                line += 1;
                i += 2;
                continue;
            }
            '\n' => {
                line += 1;
                i += 1;
                if depth > 0 {
                    cur.push(' ');
                } else {
                    flush(&mut cur, &mut out, start, indent);
                    at_start = true;
                    indent = 0;
                }
                continue;
            }
            '"' | '\'' => {
                let triple = chars.get(i + 1) == Some(&c) && chars.get(i + 2) == Some(&c);
                let n = if triple { 3 } else { 1 };
                cur.extend(std::iter::repeat_n(c, n));
                i += n;
                while let Some(&d) = chars.get(i) {
                    if d == '\\' && i + 1 < chars.len() {
                        cur.push(d);
                        if chars.get(i + 1) == Some(&'\n') {
                            cur.push(' ');
                            line += 1;
                        } else if let Some(&e) = chars.get(i + 1) {
                            cur.push(e);
                        }
                        i += 2;
                        continue;
                    }
                    if d == c
                        && (!triple
                            || (chars.get(i + 1) == Some(&c) && chars.get(i + 2) == Some(&c)))
                    {
                        cur.extend(std::iter::repeat_n(c, n));
                        i += n;
                        break;
                    }
                    if d == '\n' {
                        if !triple {
                            break;
                        }
                        cur.push(' ');
                        line += 1;
                        i += 1;
                        continue;
                    }
                    cur.push(d);
                    i += 1;
                }
                continue;
            }
            '(' | '[' | '{' => depth += 1,
            ')' | ']' | '}' => depth = depth.saturating_sub(1),
            _ => {}
        }
        cur.push(c);
        i += 1;
    }
    flush(&mut cur, &mut out, start, indent);
    out
}

/// Splits `text` at every top-level (bracket depth 0) occurrence of `sep`, outside strings.
fn split_top(text: &str, sep: char) -> Vec<&str> {
    let mut parts = Vec::new();
    let (mut depth, mut quote, mut from) = (0usize, None::<char>, 0usize);
    let mut prev = '\0';
    for (k, c) in text.char_indices() {
        match quote {
            Some(q) => {
                if c == q && prev != '\\' {
                    quote = None;
                }
            }
            None => match c {
                '"' | '\'' => quote = Some(c),
                '(' | '[' | '{' => depth += 1,
                ')' | ']' | '}' => depth = depth.saturating_sub(1),
                _ if c == sep && depth == 0 => {
                    parts.push(&text[from..k]);
                    from = k + c.len_utf8();
                }
                _ => {}
            },
        }
        prev = c;
    }
    parts.push(&text[from..]);
    parts
}

/// Whether a statement is a bare string literal (a docstring).
fn is_docstring(stmt: &str) -> bool {
    let word: String = stmt
        .chars()
        .take_while(|c| c.is_ascii_alphabetic())
        .collect();
    let rest = &stmt[word.len()..];
    word.len() <= 2 && (rest.starts_with('"') || rest.starts_with('\''))
}

/// A record field `name: type` (or `name: type = default` in a dataclass).
fn field(stmt: &str) -> Option<(String, String)> {
    let stmt = stmt.trim();
    if is_docstring(stmt) {
        return None;
    }
    let parts = split_top(stmt, ':');
    let (f, rest) = match parts.as_slice() {
        [f, rest @ ..] if !rest.is_empty() => (f.trim(), rest.join(":")),
        _ => return None,
    };
    if !is_ident(f) {
        return None;
    }
    let ty = split_top(&rest, '=').first().map(|t| t.to_string())?;
    Some((f.to_string(), normalize_type(&ty)))
}

fn scan_records(text: &str) -> Records {
    let mut r = Records::default();
    let lines = logical_lines(text);
    let mut i = 0;
    let mut dataclass = false;
    while let Some(l) = lines.get(i) {
        i += 1;
        if l.indent != 0 {
            continue;
        }
        let t = l.text.trim();
        if let Some(dec) = t.strip_prefix('@') {
            let name = dec.split('(').next().unwrap_or("").trim();
            if matches!(name, "dataclass" | "dataclasses.dataclass") {
                dataclass = true;
            }
            continue;
        }
        let is_dataclass = std::mem::take(&mut dataclass);
        if let Some(rest) = t.strip_prefix("class ") {
            let header = split_top(rest, ':');
            let head = header.first().copied().unwrap_or("").trim();
            let inline = header.get(1..).map(|p| p.join(":")).unwrap_or_default();
            let (name, bases) = match head.split_once('(') {
                Some((n, b)) => (n.trim(), b),
                None => (head, ""),
            };
            // A generic class `class X[T](...)` keeps only its name.
            let name = name.split('[').next().unwrap_or(name).trim().to_string();
            if !is_ident(&name) {
                continue;
            }
            let mut body: Vec<&str> = split_top(&inline, ';');
            let body_indent = lines.get(i).map(|b| b.indent).filter(|&n| n > 0);
            while let Some(b) = lines.get(i) {
                if b.indent == 0 {
                    break;
                }
                if Some(b.indent) == body_indent {
                    body.extend(split_top(&b.text, ';'));
                }
                i += 1;
            }
            if bases.contains("TypedDict") || is_dataclass {
                let fields = body.iter().filter_map(|s| field(s)).collect();
                r.classes.insert(name, fields);
            } else {
                r.unreadable.insert(name, l.line);
            }
            continue;
        }
        let parts = split_top(t, '=');
        let (lhs, rhs) = match parts.as_slice() {
            [lhs, rhs] => (lhs.trim(), rhs.trim()),
            _ => continue,
        };
        // `type X = ...` / `type X[T] = ...` (Python 3.12) and `X: TypeAlias = ...` are
        // aliases like `X = ...`.
        let lhs = match lhs.strip_prefix("type ") {
            Some(t) => t.split('[').next().unwrap_or(t).trim(),
            None => lhs,
        };
        match lhs.split_once(':') {
            Some((var, ann)) if var.trim() == "INITIAL_STATE" => {
                r.initial = Some(normalize_type(ann));
            }
            Some((var, ann)) if ann.trim().ends_with("TypeAlias") && is_ident(var.trim()) => {
                r.aliases
                    .insert(var.trim().to_string(), normalize_type(rhs));
            }
            None if is_ident(lhs) => {
                r.aliases.insert(lhs.to_string(), normalize_type(rhs));
            }
            _ => {}
        }
    }
    r
}

/// Whitespace-normalised type text: no spaces except one after each comma and colon.
fn normalize_type(s: &str) -> String {
    let compact: String = s.chars().filter(|c| !c.is_whitespace()).collect();
    compact.replace(',', ", ").replace(':', ": ")
}

fn expand(ty: &str, r: &Records, depth: usize) -> String {
    if depth > 16 {
        return ty.to_string();
    }
    let mut out = String::new();
    let chars: Vec<char> = ty.chars().collect();
    let mut i = 0;
    while i < chars.len() {
        let c = chars[i];
        if c.is_ascii_alphabetic() || c == '_' {
            let start = i;
            while i < chars.len()
                && (chars[i].is_ascii_alphanumeric() || chars[i] == '_' || chars[i] == '.')
            {
                i += 1;
            }
            let word: String = chars[start..i].iter().collect();
            if let Some(fields) = r.classes.get(&word) {
                out.push_str(&record_shape(fields, r, depth + 1));
            } else if let Some(alias) = r.aliases.get(&word) {
                out.push_str(&expand(alias, r, depth + 1));
            } else {
                out.push_str(&word);
            }
            continue;
        }
        out.push(c);
        i += 1;
    }
    out
}

fn record_shape(fields: &[(String, String)], r: &Records, depth: usize) -> String {
    let parts: Vec<String> = fields
        .iter()
        .map(|(f, t)| format!("{f}: {}", expand(t, r, depth)))
        .collect();
    format!("{{{}}}", parts.join(", "))
}

/// The canonical state shape of one `lib/<module>.py`: the type `INITIAL_STATE` is annotated
/// with (a record, an alias or a plain type such as `dict[str, bytes]`), or, when there is no
/// annotation, every TypedDict or dataclass record and alias whose name ends in `State`,
/// expanded through nested records and aliases. Comments, docstrings and record names do not
/// count; field names and field types do. A record or alias may span lines. Empty when the
/// module declares no state record.
///
/// Refuses, with the one-based line, a state type that names a class the scanner cannot read
/// as a TypedDict or a dataclass (a plain class, a `NamedTuple`, an `Enum`, ...), rather than
/// comparing a partial shape.
pub fn state_shape(text: &str) -> Result<String, String> {
    let r = scan_records(text);
    let roots: Vec<String> = match &r.initial {
        Some(t) => vec![t.clone()],
        None => {
            let mut roots: Vec<String> = r
                .classes
                .keys()
                .chain(r.aliases.keys())
                .chain(r.unreadable.keys())
                .filter(|n| n.ends_with("State"))
                .cloned()
                .collect();
            roots.sort();
            roots.dedup();
            roots
        }
    };
    let shapes: Vec<String> = roots.iter().map(|n| expand(n, &r, 0)).collect();
    for shape in &shapes {
        let words = shape.split(|c: char| !(c.is_ascii_alphanumeric() || c == '_'));
        if let Some((name, line)) = words.filter_map(|w| r.unreadable.get_key_value(w)).next() {
            return Err(format!(
                "line {line}: the state type names the class {name}, which is neither a TypedDict nor a dataclass, so its shape cannot be read or compared"
            ));
        }
    }
    Ok(shapes.join(" | "))
}

// ---------------------------------------------------------------------------------------------
// ContractDiff
// ---------------------------------------------------------------------------------------------

/// One diff entry.
#[derive(Debug, Clone, PartialEq)]
pub struct Entry {
    /// `None` for a `params` entry, which has no change kind.
    pub kind: Option<ChangeKind>,
    /// Primitive, adapter, module or settings key the entry concerns.
    pub target: String,
    /// Compared field.
    pub field: String,
    /// Value in the previous surface; absent for additions.
    pub before: Option<Value>,
    /// Value in this surface; absent for removals.
    pub after: Option<Value>,
    /// Minimum bump this entry requires.
    pub bump: Bump,
    /// For `state_shape` entries: the migration the newest release names for the module, if any.
    pub migration: Option<Option<String>>,
}

impl Entry {
    fn new(
        kind: Option<ChangeKind>,
        target: &str,
        field: &str,
        before: Option<Value>,
        after: Option<Value>,
        bump: Bump,
    ) -> Self {
        Entry {
            kind,
            target: target.to_string(),
            field: field.to_string(),
            before,
            after,
            bump,
            migration: None,
        }
    }

    /// The change kind as written in CHANGELOG.kvx, or `params`.
    pub fn kind_str(&self) -> &'static str {
        self.kind.map(|k| k.as_str()).unwrap_or("params")
    }

    /// The module (or settings section) the target belongs to.
    pub fn module(&self) -> &str {
        if self.target.starts_with("manual/") {
            return "manual";
        }
        self.target.split('.').next().unwrap_or(&self.target)
    }

    /// The entry as a contract_diff.json object.
    pub fn to_value(&self) -> Value {
        let mut m = Map::new();
        m.insert(
            "kind".into(),
            self.kind
                .map(|k| Value::String(k.as_str().into()))
                .unwrap_or(Value::Null),
        );
        m.insert("target".into(), Value::String(self.target.clone()));
        m.insert("field".into(), Value::String(self.field.clone()));
        if let Some(b) = &self.before {
            m.insert("before".into(), b.clone());
        }
        if let Some(a) = &self.after {
            m.insert("after".into(), a.clone());
        }
        m.insert("bump".into(), Value::String(bump_str(self.bump).into()));
        if let Some(mig) = &self.migration {
            m.insert(
                "migration".into(),
                mig.clone().map(Value::String).unwrap_or(Value::Null),
            );
        }
        Value::Object(m)
    }

    /// Short label for diagnostics: `kind target field`.
    pub fn label(&self) -> String {
        format!("{} {} ({})", self.kind_str(), self.target, self.field)
    }
}

/// The ContractDiff of compiler.md section 5 plus the version verdict of section 6.
#[derive(Debug, Clone)]
pub struct ContractDiff {
    /// Identity of the previous Build or source; `None` on the first-Build path.
    pub previous: Option<String>,
    /// Version of the previous Build or source; `None` on the first-Build path.
    pub previous_version: Option<Version>,
    /// Declared version of this source.
    pub version: Version,
    /// Rule entries, sorted by target then field.
    pub entries: Vec<Entry>,
    /// Documentation-only entries, sorted by target then field.
    pub doc_entries: Vec<Entry>,
    /// `manual/` prose entries (kind `doc_only`, field `prose`), sorted by target.
    pub prose_entries: Vec<Entry>,
    /// Maximum bump over all entries.
    pub derived_bump: Bump,
    /// `None` on the first-Build path.
    pub verdict: Option<version::Verdict>,
}

impl ContractDiff {
    /// The first-Build diff: no previous Build, empty entries, derived bump none.
    pub fn first(version: Version) -> Self {
        ContractDiff {
            previous: None,
            previous_version: None,
            version,
            entries: Vec::new(),
            doc_entries: Vec::new(),
            prose_entries: Vec::new(),
            derived_bump: Bump::None,
            verdict: None,
        }
    }

    /// The diff as the contract_diff.json value.
    pub fn to_value(&self) -> Value {
        let (declared, pre, rule) = match &self.verdict {
            Some(v) => (Value::String(bump_str(v.declared).into()), v.pre_1_0_rule_applied, v.statement()),
            None => (Value::Null, false, "first Build: no previous Build, empty diff, derived bump none, any version is accepted".to_string()),
        };
        json!({
            "previous": self.previous,
            "previous_version": self.previous_version.map(|v| v.to_string()),
            "version": self.version.to_string(),
            "entries": self.entries.iter().map(Entry::to_value).collect::<Vec<_>>(),
            "doc_entries": self.doc_entries.iter().map(Entry::to_value).collect::<Vec<_>>(),
            "doc_only_entries": self.doc_entries.len(),
            "prose_entries": self.prose_entries.iter().map(Entry::to_value).collect::<Vec<_>>(),
            "derived_bump": bump_str(self.derived_bump),
            "declared_bump": declared,
            "pre_1_0_rule_applied": pre,
            "version_rule": rule,
        })
    }

    /// `contract_diff.json` bytes: canonical JSON with sorted keys and two-space indentation.
    pub fn to_json(&self) -> String {
        canonical_pretty(&self.to_value())
    }
}

/// Canonical pretty JSON: keys sorted bytewise at every level, two-space indentation, `\n`
/// line ends and one final newline. Independent of serde_json's map-ordering features.
pub fn canonical_pretty(v: &Value) -> String {
    let mut out = String::new();
    write_pretty(v, 0, &mut out);
    out.push('\n');
    out
}

fn write_pretty(v: &Value, indent: usize, out: &mut String) {
    let pad = |n: usize| "  ".repeat(n);
    match v {
        Value::Object(m) if !m.is_empty() => {
            let mut keys: Vec<&String> = m.keys().collect();
            keys.sort();
            out.push_str("{\n");
            for (i, k) in keys.iter().enumerate() {
                let _ = write!(out, "{}{}: ", pad(indent + 1), Value::from(k.as_str()));
                write_pretty(&m[*k], indent + 1, out);
                out.push_str(if i + 1 < keys.len() { ",\n" } else { "\n" });
            }
            let _ = write!(out, "{}}}", pad(indent));
        }
        Value::Array(a) if !a.is_empty() => {
            out.push_str("[\n");
            for (i, x) in a.iter().enumerate() {
                out.push_str(&pad(indent + 1));
                write_pretty(x, indent + 1, out);
                out.push_str(if i + 1 < a.len() { ",\n" } else { "\n" });
            }
            let _ = write!(out, "{}]", pad(indent));
        }
        Value::Object(_) => out.push_str("{}"),
        Value::Array(_) => out.push_str("[]"),
        other => out.push_str(&other.to_string()),
    }
}

fn is_subsequence<T: PartialEq>(short: &[T], long: &[T]) -> bool {
    let mut it = long.iter();
    short.iter().all(|x| it.any(|y| y == x))
}

fn setting_bump(key: &str, before: &Value, after: &Value) -> Bump {
    match (before, after) {
        (Value::Number(b), Value::Number(a)) => {
            let (b, a) = (b.as_f64().unwrap_or(0.0), a.as_f64().unwrap_or(0.0));
            let stricter = if key.starts_with("clock.") {
                a > b
            } else {
                a < b
            };
            if stricter {
                Bump::Major
            } else {
                Bump::Minor
            }
        }
        (Value::Bool(b), Value::Bool(a)) => {
            // rng.seed_visible: hiding the seed removes a visible value.
            if *b && !*a {
                Bump::Major
            } else {
                Bump::Minor
            }
        }
        _ => Bump::Major,
    }
}

/// Computes the ContractDiff from `prev` to `cur`. `changelog` (the new source's) supplies the
/// migration named for each `state_shape` entry.
pub fn compute(prev: &Surface, cur: &Surface, changelog: Option<&Changelog>) -> ContractDiff {
    let mut entries = Vec::new();
    let mut docs = Vec::new();
    let names: BTreeSet<&String> = prev.contracts.keys().chain(cur.contracts.keys()).collect();
    let ordered = prev.params_ordered && cur.params_ordered;
    for name in names {
        let (old, new) = match (prev.contracts.get(name), cur.contracts.get(name)) {
            (None, Some(_)) => {
                entries.push(Entry::new(
                    Some(ChangeKind::PrimitiveAdded),
                    name,
                    "primitive",
                    None,
                    None,
                    Bump::Minor,
                ));
                continue;
            }
            (Some(_), None) => {
                entries.push(Entry::new(
                    Some(ChangeKind::PrimitiveRemoved),
                    name,
                    "primitive",
                    None,
                    None,
                    Bump::Major,
                ));
                continue;
            }
            (Some(o), Some(n)) => (o, n),
            (None, None) => continue,
        };
        if old.side_effect != new.side_effect {
            entries.push(Entry::new(
                Some(ChangeKind::PreconditionChanged),
                name,
                "side_effect",
                Some(json!(old.side_effect)),
                Some(json!(new.side_effect)),
                Bump::Major,
            ));
        }
        if old.returns != new.returns {
            entries.push(Entry::new(
                Some(ChangeKind::PreconditionChanged),
                name,
                "returns",
                Some(json!(old.returns)),
                Some(json!(new.returns)),
                Bump::Major,
            ));
        }
        let params_differ = if ordered {
            old.params != new.params
        } else {
            let a: BTreeMap<_, _> = old.params.iter().cloned().collect();
            let b: BTreeMap<_, _> = new.params.iter().cloned().collect();
            a != b
        };
        if params_differ {
            entries.push(Entry::new(
                None,
                name,
                "params",
                Some(old.params_value()),
                Some(new.params_value()),
                Bump::Major,
            ));
        }
        if old.preconditions != new.preconditions {
            let kind = if is_subsequence(&old.preconditions, &new.preconditions) {
                ChangeKind::PreconditionAdded
            } else if is_subsequence(&new.preconditions, &old.preconditions) {
                ChangeKind::PreconditionRemoved
            } else {
                ChangeKind::PreconditionChanged
            };
            let bump = if kind == ChangeKind::PreconditionRemoved {
                Bump::Minor
            } else {
                Bump::Major
            };
            entries.push(Entry::new(
                Some(kind),
                name,
                "preconditions",
                Some(old.preconditions_value()),
                Some(new.preconditions_value()),
                bump,
            ));
        }
        if old.effects != new.effects {
            entries.push(Entry::new(
                Some(ChangeKind::EffectChanged),
                name,
                "effects",
                Some(json!(old.effects)),
                Some(json!(new.effects)),
                Bump::Major,
            ));
        }
        if old.idempotency != new.idempotency {
            entries.push(Entry::new(
                Some(ChangeKind::AdapterChanged),
                name,
                "idempotency",
                Some(json!(old.idempotency)),
                Some(json!(new.idempotency)),
                Bump::Major,
            ));
        }
        if old.timeout_ms != new.timeout_ms {
            entries.push(Entry::new(
                Some(ChangeKind::AdapterChanged),
                name,
                "timeout_ms",
                Some(json!(old.timeout_ms)),
                Some(json!(new.timeout_ms)),
                Bump::Major,
            ));
        }
        if old.endpoint != new.endpoint {
            entries.push(Entry::new(
                Some(ChangeKind::AdapterChanged),
                name,
                "endpoint",
                Some(json!(old.endpoint)),
                Some(json!(new.endpoint)),
                Bump::Major,
            ));
        }
        if old.undeclared_status != new.undeclared_status {
            entries.push(Entry::new(
                Some(ChangeKind::AdapterChanged),
                name,
                "undeclared_status",
                Some(json!(old.undeclared_status)),
                Some(json!(new.undeclared_status)),
                Bump::Major,
            ));
        }
        if old.retry_after_ratio != new.retry_after_ratio {
            entries.push(Entry::new(
                Some(ChangeKind::AdapterChanged),
                name,
                "retry_after_ratio",
                Some(json!(old.retry_after_ratio)),
                Some(json!(new.retry_after_ratio)),
                Bump::Major,
            ));
        }
        let fset = |f: &Option<Vec<String>>| {
            f.as_ref()
                .map(|v| v.iter().cloned().collect::<BTreeSet<_>>())
        };
        if fset(&old.failures) != fset(&new.failures) {
            entries.push(Entry::new(
                Some(ChangeKind::FailureChanged),
                name,
                "failures",
                Some(json!(old.failures)),
                Some(json!(new.failures)),
                Bump::Major,
            ));
        }
        if old.doc != new.doc {
            docs.push(Entry::new(
                Some(ChangeKind::DocOnly),
                name,
                "doc",
                Some(json!(old.doc)),
                Some(json!(new.doc)),
                Bump::Patch,
            ));
        }
    }
    let keys: BTreeSet<&String> = prev.settings.keys().chain(cur.settings.keys()).collect();
    for key in keys {
        let (b, a) = (prev.settings.get(key), cur.settings.get(key));
        if b != a {
            let bump = match (b, a) {
                (Some(b), Some(a)) => setting_bump(key, b, a),
                _ => Bump::Major,
            };
            entries.push(Entry::new(
                Some(ChangeKind::LimitChanged),
                key,
                "value",
                Some(b.cloned().unwrap_or(Value::Null)),
                Some(a.cloned().unwrap_or(Value::Null)),
                bump,
            ));
        }
    }
    let modules: BTreeSet<&String> = prev.state.keys().chain(cur.state.keys()).collect();
    let newest = changelog.and_then(|c| c.newest().map(|r| (c, r)));
    for m in modules {
        let (b, a) = (prev.state.get(m), cur.state.get(m));
        if b != a {
            let mut e = Entry::new(
                Some(ChangeKind::StateShape),
                m,
                "state",
                Some(json!(b)),
                Some(json!(a)),
                Bump::Major,
            );
            let migration = newest.and_then(|(log, rel)| {
                rel.changes
                    .iter()
                    .filter_map(|id| log.change(id))
                    .find(|c| c.kind == ChangeKind::StateShape && c.target == *m)
                    .map(|c| c.migration.clone())
                    .filter(|s| !s.is_empty())
            });
            e.migration = Some(migration);
            entries.push(e);
        }
    }
    let mut prose = Vec::new();
    if let (Some(b), Some(a)) = (&prev.prose, &cur.prose) {
        let targets: BTreeSet<&String> = b.keys().chain(a.keys()).collect();
        for t in targets {
            let (x, y) = (b.get(t), a.get(t));
            if x != y {
                prose.push(Entry::new(
                    Some(ChangeKind::DocOnly),
                    t,
                    "prose",
                    Some(json!(x)),
                    Some(json!(y)),
                    Bump::Patch,
                ));
            }
        }
    }
    let key = |e: &Entry| (e.target.clone(), e.field.clone(), e.kind_str());
    entries.sort_by_key(key);
    docs.sort_by_key(key);
    prose.sort_by_key(key);
    let derived = entries
        .iter()
        .chain(docs.iter())
        .chain(prose.iter())
        .map(|e| e.bump)
        .max()
        .unwrap_or(Bump::None);
    let verdict = version::check(prev.version, cur.version, derived);
    ContractDiff {
        previous: Some(prev.identity.clone()),
        previous_version: Some(prev.version),
        version: cur.version,
        entries,
        doc_entries: docs,
        prose_entries: prose,
        derived_bump: derived,
        verdict: Some(verdict),
    }
}

// ---------------------------------------------------------------------------------------------
// Resolving the previous Build or source, and the diff stage
// ---------------------------------------------------------------------------------------------

/// What a Build or source reference resolved to.
#[derive(Debug, Clone)]
pub enum Reference {
    /// A hash-verifiable Build directory.
    Build(PathBuf),
    /// A World Source directory.
    Source(PathBuf),
}

/// Resolves a reference: a Build directory (has `manifest.kvx`), a World Source directory (has
/// `world.kvx`), or a Build hash (`sha256:<hex>` or `<hex>`) looked up in each of `roots`.
pub fn resolve_reference(reference: &str, roots: &[PathBuf]) -> Result<Reference, String> {
    let p = Path::new(reference);
    if p.is_dir() {
        if p.join("manifest.kvx").is_file() {
            return Ok(Reference::Build(p.to_path_buf()));
        }
        if p.join("world.kvx").is_file() {
            return Ok(Reference::Source(p.to_path_buf()));
        }
        return Err(format!("{reference} is neither a Build directory (manifest.kvx) nor a World Source directory (world.kvx)"));
    }
    let hex = reference.trim_start_matches("sha256:");
    if is_hex64(hex) {
        for r in roots {
            let c = r.join(hex);
            if c.is_dir() {
                return Ok(Reference::Build(c));
            }
        }
        return Err(format!(
            "no Build directory for {reference} under {}",
            roots
                .iter()
                .map(|r| r.display().to_string())
                .collect::<Vec<_>>()
                .join(", ")
        ));
    }
    Err(format!("no Build or World Source directory {reference}"))
}

/// Compiles a second World Source through lint and returns its surface and changelog.
/// Diagnostics of a failing run are returned as the error.
pub fn surface_of_source(
    root: &Path,
    allow_example: bool,
) -> Result<(Surface, Changelog), Vec<Diagnostic>> {
    let options = Options {
        allow_example,
        through: Stage::Lint,
        ..Options::default()
    };
    let mut cx = Context::new(root, &options);
    for stage in [Stage::Parse, Stage::Resolve, Stage::Lint] {
        match crate::run_stage(stage, &mut cx) {
            StageRun::Passed => {}
            StageRun::Failed(d) => return Err(d),
        }
    }
    let parsed = crate::prerequisite(&cx.parsed, Stage::Diff, Stage::Parse)?;
    let surface = Surface::from_parsed(parsed, &root.display().to_string()).map_err(|m| {
        vec![Diagnostic::new(
            Stage::Diff,
            None,
            "previous",
            loc(&root.display().to_string()),
            m,
        )]
    })?;
    let changelog = parsed.require_model(Stage::Diff)?.changelog.clone();
    Ok((surface, changelog))
}

/// Resolves a reference to a surface, with the changelog when the reference is a source.
pub fn load_reference(
    reference: &Reference,
    allow_example: bool,
) -> Result<(Surface, Option<Changelog>), Vec<Diagnostic>> {
    match reference {
        Reference::Build(dir) => Surface::from_build(dir).map(|s| (s, None)).map_err(|m| {
            vec![Diagnostic::new(
                Stage::Diff,
                None,
                "previous_build",
                loc(&dir.display().to_string()),
                m,
            )]
        }),
        Reference::Source(dir) => surface_of_source(dir, allow_example).map(|(s, c)| (s, Some(c))),
    }
}

/// Resolves a reference to a surface.
pub fn surface_of(reference: &Reference, allow_example: bool) -> Result<Surface, Vec<Diagnostic>> {
    load_reference(reference, allow_example).map(|(s, _)| s)
}

/// The ContractDiff from reference `a` to reference `b` (`cwr diff <a> <b>`): each is a Build
/// directory, a Build hash under `roots`, or a World Source directory.
pub fn diff_references(
    a: &str,
    b: &str,
    roots: &[PathBuf],
    allow_example: bool,
) -> Result<ContractDiff, Vec<Diagnostic>> {
    let resolve = |r: &str| {
        resolve_reference(r, roots)
            .map_err(|m| vec![Diagnostic::new(Stage::Diff, None, "reference", loc(r), m)])
    };
    let (ra, rb) = (resolve(a)?, resolve(b)?);
    let (sa, _) = load_reference(&ra, allow_example)?;
    let (sb, log) = load_reference(&rb, allow_example)?;
    Ok(compute(&sa, &sb, log.as_ref()))
}

fn loc(file: &str) -> Location {
    Location {
        file: file.to_string(),
        line: 1,
        col: 1,
        section: None,
        key: None,
    }
}

/// The Build roots searched for a hash reference: the output directory (default `build/`) and
/// the experimental root `xbuild/` beside it.
pub fn build_roots(options: &Options) -> Vec<PathBuf> {
    let out = options
        .out
        .clone()
        .unwrap_or_else(|| PathBuf::from("build"));
    let x = out
        .parent()
        .map(|p| p.join("xbuild"))
        .unwrap_or_else(|| PathBuf::from("xbuild"));
    vec![out, x]
}

/// Whether the Build in `dir` is exactly what packaging this source again would produce: the
/// same compared surface and manual prose, the same `contracts.json` content (which carries
/// every adapter setting, the state shapes and the changelog) and copied directories (`lib/`,
/// `adapters/`, `fixtures/`, `migrations/`) byte-identical to the parse-stage inventory.
fn rebuild_of(dir: &Path, parsed: &Parsed, cur: &Surface) -> bool {
    let Ok(s) = Surface::from_build(dir) else {
        return false;
    };
    if s.to_value() != cur.to_value() || s.prose != cur.prose {
        return false;
    }
    let Ok(want) = crate::manifest::contracts_value(parsed) else {
        return false;
    };
    let got: Option<Value> = fs::read_to_string(dir.join("contracts.json"))
        .ok()
        .and_then(|t| serde_json::from_str(&t).ok());
    got.as_ref() == Some(&want) && crate::package::verify_unchanged(parsed, dir).is_ok()
}

/// The default previous Build: the newest Build of the same world name under the output
/// directory. When that Build has the current version and is byte for byte what this source
/// packages to (a rebuild of the same source, [`rebuild_of`]), its own recorded `previous` is
/// used instead, so the rebuild reproduces the same diff and the same hash. Any other Build of
/// the same version is the previous Build, so the gate refuses the equal version.
fn default_previous(
    options: &Options,
    cur: &Surface,
    parsed: &Parsed,
) -> Result<Option<Reference>, Vec<Diagnostic>> {
    let out = options
        .out
        .clone()
        .unwrap_or_else(|| PathBuf::from("build"));
    let Ok(rd) = fs::read_dir(&out) else {
        return Ok(None);
    };
    let mut best: Option<(Version, PathBuf)> = None;
    let mut dirs: Vec<PathBuf> = rd
        .filter_map(|e| e.ok())
        .map(|e| e.path())
        .filter(|p| p.join("manifest.kvx").is_file())
        .collect();
    dirs.sort();
    for d in dirs {
        let Ok(text) = fs::read_to_string(d.join("manifest.kvx")) else {
            continue;
        };
        let Ok(doc) = kvx::parse("manifest.kvx", &text) else {
            continue;
        };
        let key = |k: &str| {
            doc.sections
                .iter()
                .find_map(|s| s.get(k))
                .and_then(|e| json_str(&e.value.value))
        };
        if key("name").as_deref() != Some(cur.name.as_str()) {
            continue;
        }
        if doc
            .sections
            .iter()
            .find_map(|s| s.get("experimental"))
            .map(|e| e.value.value == Json::Bool(true))
            .unwrap_or(false)
        {
            continue;
        }
        let Some(v) = key("version").and_then(|s| s.parse::<Version>().ok()) else {
            continue;
        };
        if best.as_ref().map(|(bv, _)| v > *bv).unwrap_or(true) {
            best = Some((v, d));
        }
    }
    let Some((v, dir)) = best else {
        return Ok(None);
    };
    if v == cur.version && rebuild_of(&dir, parsed, cur) {
        return match Surface::manifest_previous(&dir) {
            None => Ok(None),
            Some(h) => resolve_reference(&h, &build_roots(options))
                .map(Some)
                .map_err(|m| {
                    vec![Diagnostic::new(
                        Stage::Diff,
                        None,
                        "previous_build",
                        loc(&dir.display().to_string()),
                        m,
                    )]
                }),
        };
    }
    Ok(Some(Reference::Build(dir)))
}

/// The diff stage: resolve the previous Build, verify it, compute the ContractDiff.
pub fn run_stage(cx: &mut Context) -> StageRun {
    let parsed = match crate::prerequisite(&cx.parsed, Stage::Diff, Stage::Parse) {
        Ok(p) => p,
        Err(d) => return StageRun::Failed(d),
    };
    let cur = match Surface::from_parsed(parsed, &cx.root.display().to_string()) {
        Ok(s) => s,
        Err(m) => {
            return StageRun::Failed(vec![Diagnostic::new(
                Stage::Diff,
                None,
                "surface",
                loc("world.kvx"),
                m,
            )])
        }
    };
    let reference = match &cx.options.previous {
        Some(r) => match resolve_reference(r, &build_roots(cx.options)) {
            Ok(r) => Some(r),
            Err(m) => {
                return StageRun::Failed(vec![Diagnostic::new(
                    Stage::Diff,
                    None,
                    "previous_build",
                    loc(r),
                    m,
                )])
            }
        },
        None => match default_previous(cx.options, &cur, parsed) {
            Ok(r) => r,
            Err(d) => return StageRun::Failed(d),
        },
    };
    let changelog = parsed.model.as_ref().map(|m| &m.changelog);
    let diff = match reference {
        None => ContractDiff::first(cur.version),
        Some(r) => match surface_of(&r, cx.options.allow_example) {
            Ok(prev) => compute(&prev, &cur, changelog),
            Err(mut d) => {
                for x in &mut d {
                    x.message = format!("previous: {}", x.message);
                }
                return StageRun::Failed(d);
            }
        },
    };
    cx.diff = Some(diff);
    StageRun::Passed
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn state_shapes() {
        let shape = |s: &str| state_shape(s).expect("readable state");
        let kv = "from typing import TypedDict\n\nclass KvState(TypedDict):\n    namespaces: dict[str,  dict[str, bytes]]  # c\n\n\nINITIAL_STATE: KvState = {\"namespaces\": {}}\n";
        assert_eq!(shape(kv), "{namespaces: dict[str, dict[str, bytes]]}");
        let flat = "KvState = dict[str, bytes]\n\nINITIAL_STATE: KvState = {}\n";
        assert_eq!(shape(flat), "dict[str, bytes]");
        let jobs = "class JobRecord(TypedDict):\n    \"\"\"doc\n    x: int\n    \"\"\"\n    kind: str\n    fail_at: Optional[int]\n\n\nclass JobsState(TypedDict):\n    next_id: int\n    jobs: dict[str, JobRecord]\n";
        assert_eq!(
            shape(jobs),
            "{next_id: int, jobs: dict[str, {kind: str, fail_at: Optional[int]}]}"
        );
        let world = "class WorldState(TypedDict):\n    pass\n\n\nINITIAL_STATE: WorldState = {}\n";
        assert_eq!(shape(world), "{}");
        assert_eq!(shape("def f(state):\n    return 1\n"), "");
    }

    #[test]
    fn plain_initial_state_annotations_have_a_shape() {
        // compiler.md section 5: a plain annotation is the state shape, so changing it in
        // either direction yields a state_shape entry and needs a migration.
        let a = state_shape("INITIAL_STATE: dict[str, bytes] = {}\n").expect("a");
        let b = state_shape("INITIAL_STATE: dict[str, dict[str, bytes]] = {}\n").expect("b");
        assert_eq!(a, "dict[str, bytes]");
        assert_eq!(b, "dict[str, dict[str, bytes]]");
        assert_ne!(a, b);
        // An annotation wins over the *State roots.
        let both =
            "class OtherState(TypedDict):\n    n: int\n\nINITIAL_STATE: dict[str, int] = {}\n";
        assert_eq!(state_shape(both).expect("both"), "dict[str, int]");
    }

    #[test]
    fn multi_line_records_and_dataclasses_are_read_whole() {
        let alias =
            |t: &str| format!("KvState = dict[\n    str, {t}]\n\nINITIAL_STATE: KvState = {{}}\n");
        let (a, b) = (state_shape(&alias("bytes")), state_shape(&alias("int")));
        assert_eq!(a.as_deref(), Ok("dict[str, bytes]"));
        assert_eq!(b.as_deref(), Ok("dict[str, int]"));
        let field = "class S(TypedDict):\n    files: dict[\n        str,  # name\n        bytes,\n    ]\n    n: int; m: str\n\nINITIAL_STATE: S = {}\n";
        assert_eq!(
            state_shape(field).as_deref(),
            Ok("{files: dict[str, bytes, ], n: int, m: str}")
        );
        let dc = "@dataclass(frozen=True)\nclass CounterState:\n    \"\"\"Doc: x.\"\"\"\n    count: int = 0\n    names: list[str] = field(default_factory=list)\n\n    def bump(self, by: int = 1) -> None:\n        local: int = by\n";
        assert_eq!(
            state_shape(dc).as_deref(),
            Ok("{count: int, names: list[str]}")
        );
    }

    #[test]
    fn unreadable_state_records_are_refused() {
        let nt = "class PointState(NamedTuple):\n    x: int\n";
        let e = state_shape(nt).expect_err("refused");
        assert!(e.starts_with("line 1:") && e.contains("PointState"), "{e}");
        let nested = "class Kind(Enum):\n    A = 1\n\nclass S(TypedDict):\n    kind: Kind\n\nINITIAL_STATE: S = {}\n";
        let e = state_shape(nested).expect_err("refused");
        assert!(e.starts_with("line 1:") && e.contains("Kind"), "{e}");
        // A class the state type does not name is not part of the shape.
        let helper = "class Helper:\n    pass\n\nINITIAL_STATE: dict[str, int] = {}\n";
        assert_eq!(state_shape(helper).as_deref(), Ok("dict[str, int]"));
    }

    #[test]
    fn subsequence() {
        assert!(is_subsequence(&[1, 3], &[1, 2, 3]));
        assert!(!is_subsequence(&[3, 1], &[1, 2, 3]));
        assert!(is_subsequence::<i32>(&[], &[1]));
    }

    #[test]
    fn canonical_output_is_sorted() {
        let v = json!({"b": [1, {"z": 1, "a": []}], "a": {}});
        assert_eq!(canonical_pretty(&v), "{\n  \"a\": {},\n  \"b\": [\n    1,\n    {\n      \"a\": [],\n      \"z\": 1\n    }\n  ]\n}\n");
    }

    #[test]
    fn prose_regions_are_the_same_from_a_source_and_from_a_rendered_manual() {
        let mut prose = BTreeMap::new();
        prose.insert("intro.md".to_string(), "# Intro\n\nHello.\n".to_string());
        prose.insert("conventions.md".to_string(), String::new());
        prose.insert("examples.md".to_string(), "Example.\n\n".to_string());
        prose.insert("types.md".to_string(), "## T\n\nx".to_string());
        prose.insert("zz.md".to_string(), "Extra.".to_string());
        let s = source_prose(&prose).expect("regions");
        assert_eq!(s["manual/intro.md"], "### Intro\n\nHello.");
        assert_eq!(s["manual/conventions.md"], "");
        assert_eq!(s["manual/examples.md"], "Example.\n\n### zz\n\nExtra.");
        assert_eq!(s["manual/types.md"], "#### T\n\nx");
        let c = json!({"world": {"name": "w", "version": "1.2.3", "modules": ["fs"]},
            "limits": {"cpu_ms": 1, "primitive_calls": 3}, "clock": {"tick_per_turn": 1}});
        let render = crate::generate::manual::render;
        let (m, p) = (render(&c, "ab", &prose), render(&c, "ab", &probe_prose()));
        assert_eq!(cut_regions(&m, &p).as_ref(), Some(&s));
        let w = crate::experimental::one_primitive::with_sentence;
        assert_eq!(
            cut_regions(&w(&m).unwrap(), &w(&p).unwrap()).as_ref(),
            Some(&s)
        );
        prose.insert("conventions.md".to_string(), "More.".to_string());
        let changed = source_prose(&prose).expect("regions");
        assert_eq!(changed["manual/conventions.md"], "More.");
        assert_eq!(cut_regions("hand-written manual\n", &p), None);
    }

    #[test]
    fn limit_bumps() {
        assert_eq!(
            setting_bump("limits.cpu_ms", &json!(5000), &json!(4000)),
            Bump::Major
        );
        assert_eq!(
            setting_bump("limits.cpu_ms", &json!(5000), &json!(6000)),
            Bump::Minor
        );
        assert_eq!(
            setting_bump("clock.tick_per_turn", &json!(10), &json!(20)),
            Bump::Major
        );
        assert_eq!(
            setting_bump("clock.tick_per_turn", &json!(10), &json!(5)),
            Bump::Minor
        );
        assert_eq!(
            setting_bump("rng.seed_visible", &json!(true), &json!(false)),
            Bump::Major
        );
    }

    fn copy_tree(from: &Path, to: &Path) {
        fs::create_dir_all(to).expect("create");
        for e in fs::read_dir(from).expect("read").flatten() {
            let (p, name) = (e.path(), e.file_name());
            if name == "README.md" {
                continue;
            }
            if p.is_dir() {
                copy_tree(&p, &to.join(&name));
            } else {
                fs::copy(&p, to.join(&name)).expect("copy");
            }
        }
    }

    fn remove_sealed(dir: &Path) {
        use std::os::unix::fs::PermissionsExt;
        fn unseal(d: &Path) {
            let _ = fs::set_permissions(d, fs::Permissions::from_mode(0o755));
            for e in fs::read_dir(d).into_iter().flatten().flatten() {
                if e.file_type().map(|t| t.is_dir()).unwrap_or(false) {
                    unseal(&e.path());
                }
            }
        }
        unseal(dir);
        let _ = fs::remove_dir_all(dir);
    }

    /// world-source.md section 14: a source at the version of the newest Build is a rebuild
    /// only when it packages to that very Build. Changing only `lib/` bytes keeps the compared
    /// surface and `contracts.json` equal, yet the gate must refuse the equal version instead of
    /// packaging a second, different Build under the same name and version.
    #[test]
    fn a_same_version_source_that_changes_lib_only_is_not_a_rebuild() {
        let repo = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..");
        let refworld = repo.join("examples/refworld");
        if !refworld.is_dir()
            || std::process::Command::new(crate::ephemeral::python())
                .arg("-V")
                .output()
                .is_err()
        {
            eprintln!("skipped: no refworld or no python3");
            return;
        }
        let base = std::env::temp_dir().join(format!("cwr-diff-rebuild-{}", std::process::id()));
        remove_sealed(&base);
        let (src, out) = (base.join("src"), base.join("build"));
        copy_tree(&refworld, &src);
        let options = Options {
            out: Some(out),
            ..Options::default()
        };
        let first = crate::compile(&src, &options);
        let again = crate::compile(&src, &options);
        let lib = src.join("lib/fs.py");
        let text = fs::read_to_string(&lib).expect("lib/fs.py");
        fs::write(
            &lib,
            format!("{text}\n# A comment: lib/ bytes change, the surface does not.\n"),
        )
        .expect("write lib");
        let changed = crate::compile(&src, &options);
        remove_sealed(&base);
        assert_eq!(first.exit_code, 0, "{}", first.to_text());
        assert_eq!(again.exit_code, 0, "{}", again.to_text());
        assert!(again.build.as_ref().is_some_and(|b| b.already_built));
        assert_eq!(
            changed.stopped_at,
            Some(Stage::ChangelogGate),
            "{}",
            changed.to_text()
        );
        assert!(
            changed
                .diagnostics()
                .any(|d| d.kind == "version_not_greater"),
            "{}",
            changed.to_text()
        );
    }
}
