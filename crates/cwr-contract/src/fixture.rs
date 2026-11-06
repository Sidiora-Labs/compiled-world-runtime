// Copyright Sidiora Labs Inc. CWR® 2026-2027
// SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0

//! Contract-test fixtures `fixtures/<module>/*.kvx` and drift schedules `drift/*.kvx`.
//!
//! Fixture files are parsed strictly so that unknown sections and keys are rejected like
//! every other World Source file. States, parameters and recorded response bodies stay
//! generic JSON because their meaning belongs to the contract-test stage; every other value
//! is typed, so the JSON model refuses what the KVX reader refuses. The `expect` value is
//! `"ok"` or one of the eleven envelope codes; `seed` is a non-negative integer; `sequence`
//! is a one-based position or null; `request_hash` is `sha256:` and 64 lowercase hex digits,
//! or null; `response` is an object or null; `prior` is a list of [`PriorCall`].
//!
//! The drift grammar is compiler.md section 10: `[drift.<n>]` with `turn`, `primitive`,
//! `change`, `expr`, `failure` and `doc`.

use std::num::{NonZeroU32, NonZeroU64};

use serde::{Deserialize, Deserializer, Serialize, Serializer};

use crate::changelog::ChangeKind;
use crate::envelope::Code;
use crate::error::{ErrorKind, Errors};
use crate::kvx::{Document, Json};
use crate::precondition::Expression;
use crate::primitive;
use crate::read::{self, Ctx, Keys};
use crate::source::FileClass;

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

/// Deserialises a key that is present as `Some`, so `null` reaches `T` (an inner `Option` or a
/// type that refuses it) instead of meaning "absent".
fn present<'de, D: Deserializer<'de>, T: Deserialize<'de>>(d: D) -> Result<Option<T>, D::Error> {
    T::deserialize(d).map(Some)
}

/// A canonical request hash, `sha256:` followed by 64 lowercase hex digits, as the adapter
/// gateway keys cassettes (kernel.md section 7). No other text can be constructed or parsed.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct RequestHash(String);

impl RequestHash {
    /// The hash text, `sha256:<64 lowercase hex>`.
    pub fn as_str(&self) -> &str {
        &self.0
    }
}

/// A string that is not a canonical request hash; carries the refused text.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RequestHashError(pub String);

impl std::fmt::Display for RequestHashError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(
            f,
            "request_hash {:?} must be sha256: followed by 64 lowercase hex digits",
            self.0
        )
    }
}

impl std::error::Error for RequestHashError {}

impl std::str::FromStr for RequestHash {
    type Err = RequestHashError;

    fn from_str(s: &str) -> Result<Self, Self::Err> {
        let hex = s.strip_prefix("sha256:").unwrap_or_default();
        if hex.len() == 64
            && hex
                .chars()
                .all(|c| c.is_ascii_digit() || ('a'..='f').contains(&c))
        {
            Ok(RequestHash(s.to_string()))
        } else {
            Err(RequestHashError(s.to_string()))
        }
    }
}

impl Serialize for RequestHash {
    fn serialize<S: Serializer>(&self, s: S) -> Result<S::Ok, S::Error> {
        s.serialize_str(&self.0)
    }
}

impl<'de> Deserialize<'de> for RequestHash {
    fn deserialize<D: Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        String::deserialize(d)?
            .parse()
            .map_err(serde::de::Error::custom)
    }
}

/// One call replayed before a fixture case's asserted call (`prior` list element).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PriorCall {
    /// Call parameters, an object keyed by parameter name.
    pub params: serde_json::Map<String, serde_json::Value>,
    /// World time of the call (default: the case's `world_time`).
    #[serde(
        default,
        skip_serializing_if = "Option::is_none",
        deserialize_with = "present"
    )]
    pub world_time: Option<u64>,
    /// Canonical request hash of the recorded response, or null.
    #[serde(
        default,
        skip_serializing_if = "Option::is_none",
        deserialize_with = "present"
    )]
    pub request_hash: Option<Option<RequestHash>>,
    /// Recorded adapter response in cassette form, or null.
    #[serde(
        default,
        skip_serializing_if = "Option::is_none",
        deserialize_with = "present"
    )]
    pub response: Option<Option<serde_json::Map<String, serde_json::Value>>>,
    /// One-based position of the recorded response among equal requests, or null (default 1).
    #[serde(
        default,
        skip_serializing_if = "Option::is_none",
        deserialize_with = "present"
    )]
    pub sequence: Option<Option<NonZeroU32>>,
}

impl PriorCall {
    /// The call as a JSON object with its keys in sorted order, as canonical KVX writes it.
    pub fn to_value(&self) -> serde_json::Value {
        let mut map = serde_json::Map::new();
        map.insert(
            "params".to_string(),
            serde_json::Value::Object(self.params.clone()),
        );
        if let Some(h) = &self.request_hash {
            map.insert(
                "request_hash".to_string(),
                opt_value(h.as_ref().map(|h| h.as_str().into())),
            );
        }
        if let Some(r) = &self.response {
            map.insert(
                "response".to_string(),
                opt_value(r.clone().map(serde_json::Value::Object)),
            );
        }
        if let Some(n) = self.sequence {
            map.insert("sequence".to_string(), opt_value(n.map(|n| n.get().into())));
        }
        if let Some(t) = self.world_time {
            map.insert("world_time".to_string(), t.into());
        }
        serde_json::Value::Object(map)
    }
}

fn opt_value(v: Option<serde_json::Value>) -> serde_json::Value {
    v.unwrap_or(serde_json::Value::Null)
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
    /// World RNG seed for the case (default 0).
    #[serde(
        default,
        skip_serializing_if = "Option::is_none",
        deserialize_with = "present"
    )]
    pub seed: Option<u64>,
    /// Calls replayed before the asserted call, in order.
    #[serde(
        default,
        skip_serializing_if = "Option::is_none",
        deserialize_with = "present"
    )]
    pub prior: Option<Vec<PriorCall>>,
    /// Canonical request hash the recorded adapter response is keyed by, or null.
    #[serde(
        default,
        skip_serializing_if = "Option::is_none",
        deserialize_with = "present"
    )]
    pub request_hash: Option<Option<RequestHash>>,
    /// One-based position of the recorded response among equal requests, or null (default 1).
    #[serde(
        default,
        skip_serializing_if = "Option::is_none",
        deserialize_with = "present"
    )]
    pub sequence: Option<Option<NonZeroU32>>,
    /// Recorded adapter response in cassette form, or null.
    #[serde(
        default,
        skip_serializing_if = "Option::is_none",
        deserialize_with = "present"
    )]
    pub response: Option<Option<serde_json::Map<String, serde_json::Value>>>,
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

impl DriftFile {
    /// Parses one `drift/<name>.kvx` schedule on its own with exactly the rules
    /// [`WorldSource::load_tree`](crate::WorldSource::load_tree) applies to it. `known`
    /// answers whether a primitive or adapter name exists in the Build or source the schedule
    /// targets; every `primitive` it refuses is a located error. `path` is the source-relative
    /// path; every error is located in it.
    pub fn parse(
        path: &str,
        text: &str,
        known: &dyn Fn(&str) -> bool,
    ) -> Result<DriftFile, Errors> {
        crate::source::parse_file(path, text, FileClass::Drift, |ctx, doc, _| {
            read_drift(ctx, doc, known)
        })
    }
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
        let seed = keys.opt("seed").and_then(|e| read::non_negative(ctx, e));
        let prior = keys.opt("prior").and_then(|e| {
            let items = read::array(ctx, e)?;
            let mut calls = Vec::new();
            let mut good = true;
            for (i, node) in items.iter().enumerate() {
                match serde_json::from_value::<PriorCall>(node.value.to_value()) {
                    Ok(call) => calls.push(call),
                    Err(err) => {
                        ctx.bad(e, node, format!("prior[{i}] is not a prior call: {err}"));
                        good = false;
                    }
                }
            }
            good.then_some(calls)
        });
        let request_hash = keys.opt("request_hash").and_then(|e| match &e.value.value {
            Json::Null => Some(None),
            Json::Str(s) => match s.parse::<RequestHash>() {
                Ok(h) => Some(Some(h)),
                Err(m) => {
                    ctx.bad(e, &e.value, m.to_string());
                    None
                }
            },
            _ => {
                ctx.bad(e, &e.value, "request_hash must be a string or null");
                None
            }
        });
        let sequence = keys.opt("sequence").and_then(|e| match e.value.value {
            Json::Null => Some(None),
            Json::Int(i) if i >= 1 && i <= i64::from(u32::MAX) => {
                Some(u32::try_from(i).ok().and_then(NonZeroU32::new))
            }
            _ => {
                ctx.bad(
                    e,
                    &e.value,
                    "sequence must be a one-based position (a positive integer) or null",
                );
                None
            }
        });
        let response = keys.opt("response").and_then(|e| match &e.value.value {
            Json::Null => Some(None),
            Json::Object(_) => match e.value.value.to_value() {
                serde_json::Value::Object(map) => Some(Some(map)),
                _ => None,
            },
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
        let optional = [
            ("returned", c.returned.clone()),
            ("seed", c.seed.map(serde_json::Value::from)),
            (
                "prior",
                c.prior.as_ref().map(|calls| {
                    serde_json::Value::Array(calls.iter().map(PriorCall::to_value).collect())
                }),
            ),
            (
                "request_hash",
                c.request_hash
                    .as_ref()
                    .map(|h| opt_value(h.as_ref().map(|h| h.as_str().into()))),
            ),
            (
                "sequence",
                c.sequence.map(|n| opt_value(n.map(|n| n.get().into()))),
            ),
            (
                "response",
                c.response
                    .as_ref()
                    .map(|r| opt_value(r.clone().map(serde_json::Value::Object))),
            ),
        ];
        for (key, value) in optional {
            if let Some(v) = value {
                out.push_str(&format!("{key} = {}\n", json_inline(&v)));
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
