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
        for (_, file) in parsed.of_class(FileClass::Lib) {
            if let Some(m) = &file.module {
                state.insert(m.clone(), state_shape(&file.text));
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
            .map_err(|(col, m)| {
                format!(
                    "Build {}: contracts.json: {m} (column {col})",
                    dir.display()
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
                    state.insert(m.to_string(), state_shape(&text));
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
}

fn strip_comment(s: &str) -> &str {
    // Type annotations carry no string literals, so the first '#' starts the comment.
    match s.find('#') {
        Some(i) => &s[..i],
        None => s,
    }
}

fn is_ident(s: &str) -> bool {
    let mut cs = s.chars();
    matches!(cs.next(), Some(c) if c.is_ascii_alphabetic() || c == '_')
        && cs.all(|c| c.is_ascii_alphanumeric() || c == '_')
}

fn scan_records(text: &str) -> Records {
    let mut r = Records::default();
    let lines: Vec<&str> = text.lines().collect();
    let mut i = 0;
    while i < lines.len() {
        let line = lines[i];
        let top = !line.starts_with(' ') && !line.starts_with('\t');
        if top {
            if let Some(rest) = line.strip_prefix("class ") {
                if let Some(open) = rest.find('(') {
                    let name = rest[..open].trim().to_string();
                    let bases = &rest[open..];
                    if bases.contains("TypedDict") && is_ident(&name) {
                        let mut fields = Vec::new();
                        let mut j = i + 1;
                        let mut in_doc: Option<&str> = None;
                        while j < lines.len() {
                            let l = lines[j];
                            if !l.trim().is_empty() && !l.starts_with(' ') && !l.starts_with('\t') {
                                break;
                            }
                            let t = l.trim();
                            if let Some(q) = in_doc {
                                if t.contains(q) {
                                    in_doc = None;
                                }
                                j += 1;
                                continue;
                            }
                            if t.starts_with("\"\"\"") || t.starts_with("'''") {
                                let q = &t[..3];
                                if !(t.len() >= 6 && t[3..].contains(q)) {
                                    in_doc = Some(q);
                                }
                                j += 1;
                                continue;
                            }
                            let t = strip_comment(t).trim();
                            if let Some((f, ty)) = t.split_once(':') {
                                let f = f.trim();
                                if is_ident(f) && !ty.contains('=') {
                                    fields.push((f.to_string(), normalize_type(ty)));
                                }
                            }
                            j += 1;
                        }
                        r.classes.insert(name, fields);
                        i = j;
                        continue;
                    }
                }
            } else {
                let t = strip_comment(line).trim();
                if let Some((lhs, rhs)) = t.split_once('=') {
                    let lhs = lhs.trim();
                    if !rhs.starts_with('=') {
                        if let Some((var, ann)) = lhs.split_once(':') {
                            if var.trim() == "INITIAL_STATE" {
                                r.initial = Some(normalize_type(ann));
                            }
                        } else if is_ident(lhs) && lhs.ends_with("State") {
                            r.aliases.insert(lhs.to_string(), normalize_type(rhs));
                        }
                    }
                }
            }
        }
        i += 1;
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

/// The canonical state shape of one `lib/<module>.py`: the record `INITIAL_STATE` is
/// annotated with, or every TypedDict class and alias whose name ends in `State`, expanded
/// through nested records and aliases. Comments, docstrings and record names do not count;
/// field names and field types do. Empty when the module declares no state record.
pub fn state_shape(text: &str) -> String {
    let r = scan_records(text);
    if let Some(root) = &r.initial {
        if r.classes.contains_key(root) || r.aliases.contains_key(root) {
            return expand(root, &r, 0);
        }
    }
    let mut roots: Vec<String> = Vec::new();
    for name in r.classes.keys().chain(r.aliases.keys()) {
        if name.ends_with("State") && !roots.contains(name) {
            roots.push(name.clone());
        }
    }
    roots.sort();
    roots
        .iter()
        .map(|n| expand(n, &r, 0))
        .collect::<Vec<_>>()
        .join(" | ")
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
            StageRun::Pending => {
                return Err(vec![Diagnostic::new(
                    Stage::Diff,
                    None,
                    "previous",
                    loc(&root.display().to_string()),
                    format!("stage {stage} is not implemented in this compiler revision"),
                )])
            }
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

/// The default previous Build: the newest Build of the same world name under the output
/// directory. When that Build has the current version and an identical surface (a rebuild of
/// the same source), its own recorded `previous` is used instead, so the rebuild reproduces
/// the same diff and the same hash.
fn default_previous(
    options: &Options,
    cur: &Surface,
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
    if v == cur.version {
        if let Ok(s) = Surface::from_build(&dir) {
            if s.to_value() == cur.to_value() && s.prose == cur.prose {
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
        }
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
        None => match default_previous(cx.options, &cur) {
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
        let kv = "from typing import TypedDict\n\nclass KvState(TypedDict):\n    namespaces: dict[str,  dict[str, bytes]]  # c\n\n\nINITIAL_STATE: KvState = {\"namespaces\": {}}\n";
        assert_eq!(state_shape(kv), "{namespaces: dict[str, dict[str, bytes]]}");
        let flat = "KvState = dict[str, bytes]\n\nINITIAL_STATE: KvState = {}\n";
        assert_eq!(state_shape(flat), "dict[str, bytes]");
        let jobs = "class JobRecord(TypedDict):\n    \"\"\"doc\n    x: int\n    \"\"\"\n    kind: str\n    fail_at: Optional[int]\n\n\nclass JobsState(TypedDict):\n    next_id: int\n    jobs: dict[str, JobRecord]\n";
        assert_eq!(
            state_shape(jobs),
            "{next_id: int, jobs: dict[str, {kind: str, fail_at: Optional[int]}]}"
        );
        let world = "class WorldState(TypedDict):\n    pass\n\n\nINITIAL_STATE: WorldState = {}\n";
        assert_eq!(state_shape(world), "{}");
        assert_eq!(state_shape("def f(state):\n    return 1\n"), "");
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
}
