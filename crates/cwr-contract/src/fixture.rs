// Copyright Sidiora Labs Inc. CWR® 2026-2027
// SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0

//! Contract-test fixtures `fixtures/<module>/*.kvx` and drift schedules `drift/*.kvx`.
//!
//! Fixture files are parsed strictly so that unknown sections and keys are rejected like
//! every other World Source file; their values (states, parameters, recorded responses)
//! stay generic JSON because their meaning belongs to the contract-test stage. The
//! `expect` value is `"ok"` or one of the eleven envelope codes; nothing else parses.
//!
//! The drift grammar is compiler.md section 10: `[drift.<n>]` with `turn`, `primitive`,
//! `change`, `expr`, `failure` and `doc`.

use std::num::{NonZeroU32, NonZeroU64};

use serde::{Deserialize, Deserializer, Serialize, Serializer};

use crate::changelog::ChangeKind;
use crate::envelope::Code;
use crate::error::ErrorKind;
use crate::kvx::{Document, Json};
use crate::precondition::Expression;
use crate::primitive;
use crate::read::{self, Ctx, Keys};

/// Required `meta.format` value of a primitive or adapter fixture file.
pub const FIXTURE_FORMAT: &str = "cwr.fixture";
/// Required `meta.format` value of a migration fixture file.
pub const MIGRATION_FIXTURE_FORMAT: &str = "cwr.migration-fixture";
/// Required `meta.format` value of a drift schedule `drift/*.kvx`.
pub const DRIFT_FORMAT: &str = "cwr.drift";

/// Expected outcome of a fixture case.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Expect {
    /// The call succeeds; the case names no failing precondition.
    Ok,
    /// The call fails with exactly this envelope code.
    Fail(Code),
}

impl Expect {
    /// The wire spelling: `"ok"` for success, otherwise the envelope code name.
    pub fn as_str(self) -> &'static str {
        match self {
            Expect::Ok => "ok",
            Expect::Fail(c) => c.as_str(),
        }
    }
}

impl Serialize for Expect {
    fn serialize<S: Serializer>(&self, s: S) -> Result<S::Ok, S::Error> {
        s.serialize_str(self.as_str())
    }
}

impl<'de> Deserialize<'de> for Expect {
    fn deserialize<D: Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        let s = String::deserialize(d)?;
        if s == "ok" {
            return Ok(Expect::Ok);
        }
        s.parse()
            .map(Expect::Fail)
            .map_err(serde::de::Error::custom)
    }
}

fn present<'de, D: Deserializer<'de>>(d: D) -> Result<Option<serde_json::Value>, D::Error> {
    serde_json::Value::deserialize(d).map(Some)
}

/// One `[case.<name>]`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct FixtureCase {
    /// Case name from the section suffix.
    pub name: String,
    /// World time, in ticks, at which the call is made.
    pub world_time: u64,
    /// Initial module state the case runs against.
    pub state: serde_json::Value,
    /// Call parameters, an object keyed by parameter name.
    pub params: serde_json::Value,
    /// Expected outcome of the call.
    pub expect: Expect,
    /// One-based index of the precondition expected to fail; `None` for ok cases and for
    /// failures not raised by a declared precondition.
    pub precondition: Option<NonZeroU32>,
    /// Expected return value of an ok call, when the case asserts one.
    #[serde(
        default,
        skip_serializing_if = "Option::is_none",
        deserialize_with = "present"
    )]
    pub returned: Option<serde_json::Value>,
    /// World RNG seed for the case, a non-negative integer (default 0).
    #[serde(
        default,
        skip_serializing_if = "Option::is_none",
        deserialize_with = "present"
    )]
    pub seed: Option<serde_json::Value>,
    /// Calls replayed before the asserted call, as an array of `{params, world_time,
    /// response, sequence}` objects.
    #[serde(
        default,
        skip_serializing_if = "Option::is_none",
        deserialize_with = "present"
    )]
    pub prior: Option<serde_json::Value>,
    /// Canonical request hash the recorded adapter response is keyed by; a string or null.
    #[serde(
        default,
        skip_serializing_if = "Option::is_none",
        deserialize_with = "present"
    )]
    pub request_hash: Option<serde_json::Value>,
    /// One-based position of the recorded response among equal requests; a non-negative
    /// integer or null (default 1).
    #[serde(
        default,
        skip_serializing_if = "Option::is_none",
        deserialize_with = "present"
    )]
    pub sequence: Option<serde_json::Value>,
    /// Recorded adapter response in cassette form; an object or null.
    #[serde(
        default,
        skip_serializing_if = "Option::is_none",
        deserialize_with = "present"
    )]
    pub response: Option<serde_json::Value>,
}

/// One `[migration.<name>]`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct MigrationCase {
    /// Case name from the section suffix.
    pub name: String,
    /// Full state handed to `migrate`.
    pub before: serde_json::Value,
    /// Full state `migrate` must return.
    pub after: serde_json::Value,
}

/// Typed fixture file. Exactly one of `primitive` (format `cwr.fixture`) and `migration`
/// (format `cwr.migration-fixture`) is set.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct FixtureFile {
    /// Source-relative path, `fixtures/<module>/<name>.kvx`.
    pub path: String,
    /// Module named by `meta.module`; equals the directory name.
    pub module: String,
    /// `meta.example`: the file is illustrative.
    pub example: bool,
    /// Primitive or adapter under test, for format `cwr.fixture`.
    pub primitive: Option<String>,
    /// Migration path under test, for format `cwr.migration-fixture`.
    pub migration: Option<String>,
    /// Call cases in declared order; empty in a migration fixture.
    pub cases: Vec<FixtureCase>,
    /// Migration cases in declared order; empty in a primitive fixture.
    pub migrations: Vec<MigrationCase>,
}

/// One `[drift.<n>]` entry.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DriftEntry {
    /// Entry number from the section suffix.
    pub n: u64,
    /// First turn at which the injected rule applies; injected before the turn's script starts.
    pub turn: NonZeroU64,
    /// Existing primitive or adapter whose rule is swapped.
    pub primitive: String,
    /// Change kind; always one that applies to a running enforcement table.
    pub change: ChangeKind,
    /// Precondition expression to add or substitute; references read_only primitives only.
    pub expr: Expression,
    /// Envelope code the injected precondition raises.
    pub failure: Code,
    /// Author's note; never rendered to the model.
    pub doc: String,
}

/// Typed drift schedule `drift/*.kvx` (compiler.md section 10).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DriftFile {
    /// Source-relative path, `drift/<name>.kvx`.
    pub path: String,
    /// `meta.example`: the file is illustrative.
    pub example: bool,
    /// Drift entries in declared order.
    pub entries: Vec<DriftEntry>,
}

fn section_suffix<'a>(ctx: &mut Ctx, name: &'a str, line: usize, prefix: &str) -> Option<&'a str> {
    let rest = name.strip_prefix(prefix)?.strip_prefix('.')?;
    if rest.is_empty() || rest.contains('.') {
        ctx.push(
            line,
            1,
            ErrorKind::UnknownSection,
            format!("section [{name}] must be [{prefix}.<name>] with a single-segment name"),
        );
        return None;
    }
    Some(rest)
}

fn value_of(entry: &crate::kvx::Entry) -> serde_json::Value {
    entry.value.value.to_value()
}

/// Reads a fixture file in `fixtures/<module_dir>/`.
pub(crate) fn read_fixture(ctx: &mut Ctx, doc: &Document, module_dir: &str) -> Option<FixtureFile> {
    let format = doc
        .section("meta")
        .and_then(|s| s.get("format"))
        .and_then(|e| {
            if let Json::Str(s) = &e.value.value {
                Some(s.as_str())
            } else {
                None
            }
        });
    let migration_format = format == Some(MIGRATION_FIXTURE_FORMAT);
    let (example, meta) = if migration_format {
        read::meta(ctx, doc, MIGRATION_FIXTURE_FORMAT, &["module", "migration"])?
    } else {
        read::meta(ctx, doc, FIXTURE_FORMAT, &["module", "primitive"])?
    };
    let mut meta = meta.into_iter();
    let module = primitive::check_module(ctx, meta.next().flatten(), module_dir)?;
    let target = meta.next().flatten().map(|(s, _)| s)?;
    let mut ok = true;
    let mut cases = Vec::new();
    let mut migrations = Vec::new();
    let case_prefix = if migration_format {
        "migration"
    } else {
        "case"
    };
    for section in &doc.sections {
        if section.name == "meta" {
            continue;
        }
        let Some(name) = section_suffix(ctx, &section.name, section.line, case_prefix) else {
            if !section.name.starts_with(&format!("{case_prefix}.")) {
                ctx.push(section.line, 1, ErrorKind::UnknownSection, format!("unknown section [{}]; this fixture file defines [meta] and [{case_prefix}.<name>]", section.name));
            }
            ok = false;
            continue;
        };
        let mut keys = Keys::new(section);
        if migration_format {
            let before = keys
                .req(ctx, "before")
                .and_then(|e| read::object(ctx, e).map(|_| value_of(e)));
            let after = keys
                .req(ctx, "after")
                .and_then(|e| read::object(ctx, e).map(|_| value_of(e)));
            keys.finish(ctx, "a migration fixture case");
            match (before, after) {
                (Some(before), Some(after)) => migrations.push(MigrationCase {
                    name: name.to_string(),
                    before,
                    after,
                }),
                _ => ok = false,
            }
            continue;
        }
        let world_time = keys
            .req(ctx, "world_time")
            .and_then(|e| read::non_negative(ctx, e));
        let state = keys
            .req(ctx, "state")
            .and_then(|e| read::object(ctx, e).map(|_| value_of(e)));
        let params = keys
            .req(ctx, "params")
            .and_then(|e| read::object(ctx, e).map(|_| value_of(e)));
        let expect = keys.req(ctx, "expect").and_then(|e| {
            let s = read::string(ctx, e)?;
            if s == "ok" {
                return Some(Expect::Ok);
            }
            match s.parse::<Code>() {
                Ok(c) => Some(Expect::Fail(c)),
                Err(err) => {
                    ctx.at(
                        e,
                        &e.value,
                        ErrorKind::Envelope,
                        format!("expect must be \"ok\" or an envelope code: {err}"),
                    );
                    None
                }
            }
        });
        let precondition = keys
            .req(ctx, "precondition")
            .and_then(|e| match e.value.value {
                Json::Null => Some(None),
                Json::Int(i) if i >= 1 && i <= u32::MAX as i64 => Some(NonZeroU32::new(i as u32)),
                _ => {
                    ctx.bad(
                        e,
                        &e.value,
                        "precondition must be null or a one-based precondition index",
                    );
                    None
                }
            });
        if let (Some(Expect::Ok), Some(Some(_)), Some(e)) =
            (expect, precondition, section.get("precondition"))
        {
            ctx.at(
                e,
                &e.value,
                ErrorKind::Fixture,
                "an ok case names no failing precondition; precondition must be null",
            );
            ok = false;
        }
        let returned = keys.opt("returned").map(value_of);
        let seed = keys
            .opt("seed")
            .and_then(|e| read::non_negative(ctx, e).map(|_| value_of(e)));
        let prior = keys
            .opt("prior")
            .and_then(|e| read::array(ctx, e).map(|_| value_of(e)));
        let request_hash = keys.opt("request_hash").and_then(|e| match &e.value.value {
            Json::Null | Json::Str(_) => Some(value_of(e)),
            _ => {
                ctx.bad(e, &e.value, "request_hash must be a string or null");
                None
            }
        });
        let sequence = keys.opt("sequence").and_then(|e| match e.value.value {
            Json::Null => Some(value_of(e)),
            Json::Int(i) if i >= 0 => Some(value_of(e)),
            _ => {
                ctx.bad(
                    e,
                    &e.value,
                    "sequence must be a non-negative integer or null",
                );
                None
            }
        });
        let response = keys.opt("response").and_then(|e| match &e.value.value {
            Json::Null | Json::Object(_) => Some(value_of(e)),
            _ => {
                ctx.bad(e, &e.value, "response must be an object or null");
                None
            }
        });
        for (key, parsed) in [
            ("seed", seed.is_some()),
            ("prior", prior.is_some()),
            ("request_hash", request_hash.is_some()),
            ("sequence", sequence.is_some()),
            ("response", response.is_some()),
        ] {
            if section.get(key).is_some() && !parsed {
                ok = false;
            }
        }
        keys.finish(ctx, "a fixture case");
        match (world_time, state, params, expect, precondition) {
            (Some(world_time), Some(state), Some(params), Some(expect), Some(precondition)) => {
                cases.push(FixtureCase {
                    name: name.to_string(),
                    world_time,
                    state,
                    params,
                    expect,
                    precondition,
                    returned,
                    seed,
                    prior,
                    request_hash,
                    sequence,
                    response,
                })
            }
            _ => ok = false,
        }
    }
    let (primitive, migration) = if migration_format {
        (None, Some(target))
    } else {
        (Some(target), None)
    };
    ok.then(|| FixtureFile {
        path: ctx.file.clone(),
        module,
        example,
        primitive,
        migration,
        cases,
        migrations,
    })
}

/// Reads a drift schedule. `exists` answers whether a primitive or adapter name exists.
pub(crate) fn read_drift(
    ctx: &mut Ctx,
    doc: &Document,
    exists: &dyn Fn(&str) -> bool,
) -> Option<DriftFile> {
    let (example, _) = read::meta(ctx, doc, DRIFT_FORMAT, &[])?;
    let mut ok = true;
    let mut entries = Vec::new();
    for section in &doc.sections {
        if section.name == "meta" {
            continue;
        }
        let n = section
            .name
            .strip_prefix("drift.")
            .filter(|s| {
                !s.is_empty() && s.chars().all(|c| c.is_ascii_digit()) && !s.starts_with('0')
            })
            .and_then(|s| s.parse::<u64>().ok());
        let Some(n) = n else {
            ctx.push(section.line, 1, ErrorKind::UnknownSection, format!("unknown section [{}]; a drift file defines [meta] and [drift.<n>] with n a positive integer", section.name));
            ok = false;
            continue;
        };
        let mut keys = Keys::new(section);
        let turn = keys.req(ctx, "turn").and_then(|e| read::positive(ctx, e));
        let prim = keys.req(ctx, "primitive").and_then(|e| {
            let s = read::nonempty_string(ctx, e)?;
            if !exists(&s) {
                ctx.bad(
                    e,
                    &e.value,
                    format!(
                        "drift names {s:?}, which is not a primitive or adapter of this source"
                    ),
                );
                return None;
            }
            Some(s)
        });
        let change = keys.req(ctx, "change").and_then(|e| {
            let s = read::string(ctx, e)?;
            match ChangeKind::parse(&s) {
                Some(k) if k.drift_applicable() => Some(k),
                Some(k) => {
                    ctx.bad(
                        e,
                        &e.value,
                        format!(
                            "change kind {} cannot be applied to a running enforcement table",
                            k.as_str()
                        ),
                    );
                    None
                }
                None => {
                    ctx.bad(e, &e.value, format!("unknown change kind {s:?}"));
                    None
                }
            }
        });
        let expr = keys
            .req(ctx, "expr")
            .and_then(|e| primitive::read_expression(ctx, e, &e.value, "drift expr"));
        let failure = keys
            .req(ctx, "failure")
            .and_then(|e| primitive::read_code(ctx, e, &e.value, "drift failure"));
        let doc_text = keys.req(ctx, "doc").and_then(|e| read::string(ctx, e));
        keys.finish(ctx, "a drift entry");
        match (turn, prim, change, expr, failure, doc_text) {
            (Some(turn), Some(primitive), Some(change), Some(expr), Some(failure), Some(doc)) => {
                entries.push(DriftEntry {
                    n,
                    turn,
                    primitive,
                    change,
                    expr,
                    failure,
                    doc,
                })
            }
            _ => ok = false,
        }
    }
    ok.then(|| DriftFile {
        path: ctx.file.clone(),
        example,
        entries,
    })
}

fn json_inline(v: &serde_json::Value) -> String {
    // Single-line JSON with ", " and ": " separators, matching the hand-written sources.
    match v {
        serde_json::Value::Array(items) => format!(
            "[{}]",
            items.iter().map(json_inline).collect::<Vec<_>>().join(", ")
        ),
        serde_json::Value::Object(map) => format!(
            "{{{}}}",
            map.iter()
                .map(|(k, v)| format!("{}: {}", crate::kvx::quote(k), json_inline(v)))
                .collect::<Vec<_>>()
                .join(", ")
        ),
        serde_json::Value::Null => "null".to_string(),
        serde_json::Value::Bool(b) => b.to_string(),
        serde_json::Value::Number(n) => n.to_string(),
        serde_json::Value::String(s) => crate::kvx::quote(s),
    }
}

pub(crate) fn emit_fixture(f: &FixtureFile) -> String {
    let q = crate::kvx::quote;
    let format = if f.migration.is_some() {
        MIGRATION_FIXTURE_FORMAT
    } else {
        FIXTURE_FORMAT
    };
    let mut out = format!("[meta]\nformat = {}\nschema_version = 1\n", q(format));
    if f.example {
        out.push_str("example = true\n");
    }
    out.push_str(&format!("module = {}\n", q(&f.module)));
    if let Some(p) = &f.primitive {
        out.push_str(&format!("primitive = {}\n", q(p)));
    }
    if let Some(m) = &f.migration {
        out.push_str(&format!("migration = {}\n", q(m)));
    }
    for c in &f.cases {
        out.push_str(&format!("\n[case.{}]\n", c.name));
        out.push_str(&format!("world_time = {}\n", c.world_time));
        out.push_str(&format!("state = {}\n", json_inline(&c.state)));
        out.push_str(&format!("params = {}\n", json_inline(&c.params)));
        out.push_str(&format!("expect = {}\n", q(c.expect.as_str())));
        out.push_str(&format!(
            "precondition = {}\n",
            c.precondition
                .map_or_else(|| "null".to_string(), |n| n.to_string())
        ));
        for (key, value) in [
            ("returned", &c.returned),
            ("seed", &c.seed),
            ("prior", &c.prior),
            ("request_hash", &c.request_hash),
            ("sequence", &c.sequence),
            ("response", &c.response),
        ] {
            if let Some(v) = value {
                out.push_str(&format!("{key} = {}\n", json_inline(v)));
            }
        }
    }
    for m in &f.migrations {
        out.push_str(&format!("\n[migration.{}]\n", m.name));
        out.push_str(&format!("before = {}\n", json_inline(&m.before)));
        out.push_str(&format!("after = {}\n", json_inline(&m.after)));
    }
    out
}

pub(crate) fn emit_drift(f: &DriftFile) -> String {
    let q = crate::kvx::quote;
    let mut out = format!("[meta]\nformat = {}\nschema_version = 1\n", q(DRIFT_FORMAT));
    if f.example {
        out.push_str("example = true\n");
    }
    for d in &f.entries {
        out.push_str(&format!("\n[drift.{}]\n", d.n));
        out.push_str(&format!("turn = {}\n", d.turn));
        out.push_str(&format!("primitive = {}\n", q(&d.primitive)));
        out.push_str(&format!("change = {}\n", q(d.change.as_str())));
        out.push_str(&format!("expr = {}\n", q(d.expr.source())));
        out.push_str(&format!("failure = {}\n", q(d.failure.as_str())));
        out.push_str(&format!("doc = {}\n", q(&d.doc)));
    }
    out
}
