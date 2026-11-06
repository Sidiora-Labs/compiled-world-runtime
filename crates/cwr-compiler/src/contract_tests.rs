// Copyright Sidiora Labs Inc. CWR® 2026-2027
// SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0

//! Contract tests (compiler.md section 4, clauses 1.6 and 4.2, world-source.md section 9).
//!
//! Generated from the contracts and run against `fixtures/` in an ephemeral Instance
//! ([`crate::ephemeral`]). For every primitive and adapter, in name order:
//!
//! 1. every fixture case is run and its outcome must equal the case's `expect` and
//!    `precondition` (and `returned`, when given); a refused call leaves state unchanged;
//! 2. for each declared precondition `i`, some case must reach failure code `i` at index `i`;
//! 3. some case must be ok, with the declared return shape and every effect true afterwards;
//! 4. no call may produce a code the contract does not declare, and no Python exception may
//!    escape `lib/` or `live.py`;
//! 5. for an adapter, every declared failure must be reached by replaying a fixture response
//!    through the replay-only gateway (a call that passed parameter validation and every
//!    precondition); a failure that is also one of the adapter's precondition codes may instead
//!    be reached by that precondition.
//!
//! For every migration fixture file: `migrate(before) == after`, `migrate` is idempotent and
//! pure, and its result validates against the new typed state records. Every migration named
//! by the newest release must have migration fixtures.
//!
//! The whole suite runs twice, in two fresh worker processes sharing one scratch directory,
//! with the same seeds; the two outcome streams, compared as (status, shape, side-effect class)
//! per case, the per-case results and the failures found must be equal (the compile-time half
//! of clause 1.6), and a failure found only by the second run is reported with those of the
//! first. A defect in a case's `prior` calls fails the case. A failure names the primitive and
//! is located at the fixture case or the contract section concerned.

use std::collections::{BTreeMap, BTreeSet};
use std::time::Instant;

use cwr_contract::{Code, Expect, FileClass, FixtureCase, FixtureFile, WorldSource};
use serde::Serialize;
use serde_json::{json, Map, Value};

use crate::ephemeral::{CallOutcome, Ephemeral, Records, Target, Worker, WorldSettings};
use crate::parse::Parsed;
use crate::resolve::SymbolTable;
use crate::{Context, Diagnostic, Location, Stage, StageRun};

/// One element of an outcome stream.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct StreamItem {
    /// Primitive or adapter name the case exercised.
    pub target: String,
    /// Fixture case name.
    pub case: String,
    /// `ok` or the envelope code.
    pub status: String,
    /// Whether an ok result had the declared shape (true for failures).
    pub shape: bool,
    /// Declared side-effect class of the target.
    pub side_effect: String,
}

/// One suite run.
#[derive(Debug, Clone, Serialize)]
pub struct RunReport {
    /// Outcome stream in suite order.
    pub stream: Vec<StreamItem>,
    /// Host duration; dropped from the packaged tests.json, which is inside the Build hash.
    pub duration_us: u128,
}

/// Per-case result of the first run.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct CaseResult {
    /// Primitive or adapter name.
    pub target: String,
    /// Fixture file, relative to the source root.
    pub file: String,
    /// Fixture case name.
    pub case: String,
    /// Expected status: `ok` or an envelope code.
    pub expect: String,
    /// Expected failing precondition index (1-based), if any.
    pub expect_precondition: Option<u32>,
    /// Observed status: `ok` or an envelope code.
    pub got: String,
    /// Observed failing precondition index (1-based), if any.
    pub got_precondition: Option<usize>,
    /// Whether the observed outcome matched the fixture and every check held.
    pub passed: bool,
}

/// The contract-test report written as `tests.json` by the package stage.
#[derive(Debug, Clone, Serialize)]
pub struct TestReport {
    /// Number of primitives exercised.
    pub primitives: usize,
    /// Number of adapters exercised.
    pub adapters: usize,
    /// Per-case results of the first run.
    pub cases: Vec<CaseResult>,
    /// Number of migration fixture cases checked.
    pub migration_cases: usize,
    /// The two suite runs with the same seed.
    pub runs: Vec<RunReport>,
    /// Whether the two outcome streams are equal.
    pub deterministic: bool,
    /// Whether the stage passed.
    pub passed: bool,
}

impl TestReport {
    /// `tests.json` bytes: canonical JSON with sorted keys and two-space indentation, or the
    /// serde error message should the report fail to serialise.
    pub fn to_json(&self) -> Result<String, String> {
        let mut v = serde_json::to_value(self).map_err(|e| format!("tests.json: {e}"))?;
        if let Some(o) = v.as_object_mut() {
            o.insert("format".into(), json!("cwr.tests"));
        }
        Ok(crate::diff::canonical_pretty(&v))
    }
}

fn expect_str(e: &Expect) -> String {
    match e {
        Expect::Ok => "ok".into(),
        Expect::Fail(c) => c.as_str().into(),
    }
}

struct Ctx<'p> {
    parsed: &'p Parsed,
    symbols: &'p SymbolTable,
}

impl<'p> Ctx<'p> {
    fn case_loc(&self, file: &str, case: &str, prefix: &str) -> Location {
        let section = format!("{prefix}.{case}");
        let line = self
            .parsed
            .docs
            .get(file)
            .and_then(|d| d.section(&section))
            .map(|s| s.line)
            .unwrap_or(1);
        Location {
            file: file.to_string(),
            line,
            col: 1,
            section: Some(section),
            key: None,
        }
    }

    fn contract_loc(&self, name: &str, key: Option<&str>) -> Location {
        match self.symbols.symbols.get(name) {
            Some(s) => {
                let mut l = Location {
                    file: s.file.clone(),
                    line: s.line,
                    col: 1,
                    section: Some(s.section.clone()),
                    key: None,
                };
                if let Some(k) = key {
                    if let Some(e) = self
                        .parsed
                        .docs
                        .get(&s.file)
                        .and_then(|d| d.section(&s.section))
                        .and_then(|sec| sec.get(k))
                    {
                        l.line = e.line;
                        l.col = e.value.col;
                        l.key = Some(k.to_string());
                    }
                }
                l
            }
            None => Location {
                file: "world.kvx".into(),
                line: 1,
                col: 1,
                section: None,
                key: None,
            },
        }
    }

    fn diag(&self, kind: &str, loc: Location, primitive: &str, message: String) -> Diagnostic {
        Diagnostic::new(Stage::ContractTests, None, kind, loc, message).with_primitive(primitive)
    }
}

fn world_settings(model: &WorldSource) -> WorldSettings {
    let w = &model.world;
    let l = &w.limits;
    let limits = [
        ("cpu_ms", l.cpu_ms),
        ("wall_ms", l.wall_ms),
        ("memory_mb", l.memory_mb),
        ("primitive_calls", l.primitive_calls),
        ("adapter_calls", l.adapter_calls),
    ]
    .into_iter()
    .map(|(k, v)| (k.to_string(), v.get()))
    .collect();
    WorldSettings {
        limits,
        tick_per_primitive: w.clock.tick_per_primitive.get(),
        tick_per_turn: w.clock.tick_per_turn.get(),
    }
}

/// Runs one fixture case; returns the outcome of the call under test.
fn run_case(
    eph: &mut Ephemeral,
    target: &Target,
    case: &FixtureCase,
) -> Result<CallOutcome, String> {
    let types: BTreeMap<&str, String> = target
        .params()
        .iter()
        .map(|(n, t)| (n.as_str(), t.to_string()))
        .collect();
    let decode_params = |eph: &mut Ephemeral, raw: &Value| -> Result<Map<String, Value>, String> {
        let Value::Object(m) = raw else {
            return Err("params must be an object".into());
        };
        let items: Vec<(Value, String)> = m
            .iter()
            .map(|(k, v)| {
                (
                    v.clone(),
                    types.get(k.as_str()).cloned().unwrap_or_default(),
                )
            })
            .collect();
        let vals = eph.decode(&items)?;
        Ok(m.keys().cloned().zip(vals).collect())
    };
    let params = decode_params(eph, &case.params)?;
    let mut priors: Vec<(u64, Map<String, Value>)> = Vec::new();
    let mut cassette = Vec::new();
    for p in case.prior.iter().flatten() {
        let pp = decode_params(eph, &Value::Object(p.params.clone()))?;
        let wt = p.world_time.unwrap_or(case.world_time);
        if let Some(resp) = p.response.as_ref().and_then(Option::as_ref) {
            let sequence = match p.sequence {
                None => json!(1),
                Some(n) => json!(n.map(|n| n.get())),
            };
            cassette.push(json!({"adapter": target.name(), "params": Value::Object(pp.clone()), "sequence": sequence, "response": resp}));
        }
        priors.push((wt, pp));
    }
    if let Some(resp) = case.response.as_ref().and_then(Option::as_ref) {
        let sequence = case.sequence.flatten().map_or(1, |n| n.get());
        cassette.push(json!({"adapter": target.name(), "params": Value::Object(params.clone()), "sequence": sequence, "response": resp}));
    }
    let seed = case.seed.unwrap_or(0);
    eph.reset(&case.state, case.world_time, seed, &cassette)?;
    let mut prior_defects = Vec::new();
    for (k, (wt, pp)) in priors.iter().enumerate() {
        eph.world_time = *wt;
        let prior = eph.call(target.name(), pp);
        // A prior call sets up the case; a defect in it (an exception, an undeclared code, a
        // replay miss reported as a defect, a broken effect) would leave the asserted call
        // running from an unintended state, so it fails the case.
        prior_defects.extend(
            prior
                .defects
                .iter()
                .map(|d| format!("prior call {} of {}: {d}", k + 1, target.name())),
        );
    }
    eph.world_time = case.world_time;
    let mut out = eph.call(target.name(), &params);
    if !prior_defects.is_empty() {
        prior_defects.append(&mut out.defects);
        out.defects = prior_defects;
        out.code = Some(Code::InvariantViolation);
    }
    if out.code.is_none() {
        if let Some(raw) = &case.returned {
            let want = eph
                .decode(&[(raw.clone(), target.returns().to_string())])?
                .into_iter()
                .next()
                .ok_or_else(|| "decode: empty response".to_string())?;
            if out.returned.as_ref() != Some(&want) {
                out.defects.push(format!(
                    "{} returned {}, the fixture requires {}",
                    target.name(),
                    short(out.returned.as_ref().unwrap_or(&Value::Null)),
                    short(&want)
                ));
            }
        }
    }
    Ok(out)
}

fn short(v: &Value) -> String {
    let s = v.to_string();
    if s.len() > 200 {
        format!(
            "{}...",
            &s[..s
                .char_indices()
                .take_while(|(i, _)| *i < 200)
                .last()
                .map(|(i, c)| i + c.len_utf8())
                .unwrap_or(0)]
        )
    } else {
        s
    }
}

/// The declared adapter failures no case reached as compiler.md section 4 requires: through
/// the gateway (`gateway`: codes of calls that passed parameter validation and every
/// precondition), or, for a failure that is also one of the adapter's precondition codes, by
/// that precondition (`reached` with an index).
fn unreached_failures(
    failures: &[Code],
    precondition_codes: &BTreeSet<Code>,
    reached: &BTreeSet<(Code, Option<usize>)>,
    gateway: &BTreeSet<Code>,
) -> BTreeSet<Code> {
    failures
        .iter()
        .copied()
        .filter(|code| {
            let by_precondition = precondition_codes.contains(code)
                && reached.iter().any(|(c, idx)| c == code && idx.is_some());
            !gateway.contains(code) && !by_precondition
        })
        .collect()
}

/// One suite run: its report, per-case results, migration case count and failures found.
type SuiteRun = (RunReport, Vec<CaseResult>, usize, Vec<Diagnostic>);

/// One complete suite run in a fresh worker. Returns the report, per-case results and the
/// failures found.
fn run_suite(
    parsed: &Parsed,
    symbols: &SymbolTable,
    root: &std::path::Path,
    scratch: &std::path::Path,
) -> Result<SuiteRun, Vec<Diagnostic>> {
    let start = Instant::now();
    let cx = Ctx { parsed, symbols };
    let model = parsed.require_model(Stage::ContractTests)?;
    let records = parsed
        .files
        .get("manual/types.md")
        .map(|f| Records::parse(&f.text))
        .unwrap_or_default();
    let mut lib = BTreeMap::new();
    for (path, f) in parsed.of_class(FileClass::Lib) {
        if let Some(m) = &f.module {
            lib.insert(m.clone(), path.clone());
        }
    }
    let mut live = BTreeMap::new();
    for (path, f) in parsed.of_class(FileClass::AdapterLive) {
        if let Some(m) = &f.module {
            live.insert(m.clone(), path.clone());
        }
    }
    let mut targets: BTreeMap<String, Target> = BTreeMap::new();
    for cf in &model.contracts {
        for p in &cf.primitives {
            targets.insert(p.name.clone(), Target::Primitive(p));
        }
    }
    for af in &model.adapters {
        for a in &af.adapters {
            targets.insert(a.name.clone(), Target::Adapter(a));
        }
    }
    let settings = world_settings(model);
    let mut worker = Worker::spawn(
        root,
        scratch,
        &lib,
        &live,
        &records,
        &model.world.world.version.to_string(),
        &settings,
    )
    .map_err(|m| {
        vec![Diagnostic::new(
            Stage::ContractTests,
            None,
            "worker",
            Location {
                file: "world.kvx".into(),
                line: 1,
                col: 1,
                section: None,
                key: None,
            },
            m,
        )]
    })?;
    let mut diags = Vec::new();
    // kernel.md section 7: every code the gateway maps a service status to is a declared failure;
    // a live.py STATUS_CODES target outside `failures` is refused here, never patched at runtime.
    if !live.is_empty() {
        let codes = worker
            .request(&json!({"op": "status_codes"}))
            .map_err(|m| {
                vec![Diagnostic::new(
                    Stage::ContractTests,
                    None,
                    "worker",
                    Location {
                        file: "world.kvx".into(),
                        line: 1,
                        col: 1,
                        section: None,
                        key: None,
                    },
                    m,
                )]
            })?;
        for (name, target) in &targets {
            let Target::Adapter(a) = target else { continue };
            let Some(map) = codes
                .get("codes")
                .and_then(|c| c.get(name))
                .and_then(Value::as_object)
            else {
                continue;
            };
            for (status, code) in map {
                let code = code.as_str().unwrap_or("");
                if code != "ok"
                    && !code
                        .parse::<Code>()
                        .map(|c| a.failures.contains(&c))
                        .unwrap_or(false)
                {
                    diags.push(cx.diag(
                        "adapter_status",
                        cx.contract_loc(name, Some("failures")),
                        name,
                        format!("{name}: live.py STATUS_CODES maps status {status} to {code:?}, which is not among its declared failures; the gateway would need it"),
                    ));
                }
            }
        }
    }
    let mut eph = Ephemeral::new(worker, &targets, &records, &settings);
    let mut stream = Vec::new();
    let mut cases = Vec::new();

    let mut by_target: BTreeMap<&str, Vec<&FixtureFile>> = BTreeMap::new();
    for f in &model.fixtures {
        if let Some(p) = &f.primitive {
            if targets.contains_key(p) {
                by_target.entry(p.as_str()).or_default().push(f);
            } else {
                diags.push(cx.diag(
                    "fixture",
                    Location { file: f.path.clone(), line: 1, col: 1, section: Some("meta".into()), key: Some("primitive".into()) },
                    p,
                    format!("fixture file names {p}, which is not a primitive or adapter of this source"),
                ));
            }
        }
    }

    for (name, target) in &targets {
        let mut reached: BTreeSet<(Code, Option<usize>)> = BTreeSet::new();
        // Codes an adapter call produced at the gateway (step 3), not at parameter validation
        // or a precondition: what replaying a fixture response through the gateway reached.
        let mut gateway: BTreeSet<Code> = BTreeSet::new();
        let mut has_ok = false;
        for file in by_target.get(name.as_str()).cloned().unwrap_or_default() {
            for case in &file.cases {
                let loc = cx.case_loc(&file.path, &case.name, "case");
                let out = match run_case(&mut eph, target, case) {
                    Ok(o) => o,
                    Err(m) => {
                        diags.push(cx.diag(
                            "fixture",
                            loc,
                            name,
                            format!("{name} case {}: {m}", case.name),
                        ));
                        continue;
                    }
                };
                let want_idx = case.precondition.map(|n| n.get() as usize);
                let got = out.status();
                let matches = got == expect_str(&case.expect)
                    && out.precondition == want_idx
                    && out.defects.is_empty();
                for d in &out.defects {
                    diags.push(cx.diag(
                        "contract_test",
                        loc.clone(),
                        name,
                        format!("{name} case {}: {d}", case.name),
                    ));
                }
                if out.defects.is_empty() && !matches {
                    let at = |i: Option<usize>| {
                        i.map(|i| format!(" at precondition {i}"))
                            .unwrap_or_default()
                    };
                    let message = format!(
                        "{name} case {}: got {got}{}, the fixture expects {}{}",
                        case.name,
                        at(out.precondition),
                        expect_str(&case.expect),
                        at(want_idx)
                    );
                    diags.push(cx.diag("contract_test", loc.clone(), name, message));
                }
                match out.code {
                    None if out.defects.is_empty() => has_ok = true,
                    Some(c) if out.defects.is_empty() => {
                        reached.insert((c, out.precondition));
                        if out.executed && out.precondition.is_none() {
                            gateway.insert(c);
                        }
                    }
                    _ => {}
                }
                stream.push(StreamItem {
                    target: name.clone(),
                    case: format!("{}#{}", file.path, case.name),
                    status: got.clone(),
                    shape: out.shape_ok,
                    side_effect: target.side_effect().into(),
                });
                cases.push(CaseResult {
                    target: name.clone(),
                    file: file.path.clone(),
                    case: case.name.clone(),
                    expect: expect_str(&case.expect),
                    expect_precondition: case.precondition.map(|n| n.get()),
                    got,
                    got_precondition: out.precondition,
                    passed: matches,
                });
            }
        }
        for (i, (expr, code)) in target.preconditions().into_iter().enumerate() {
            if !reached.contains(&(code, Some(i + 1))) {
                diags.push(
                    cx.diag(
                        "unreachable_failure",
                        cx.contract_loc(name, Some("preconditions")),
                        name,
                        format!(
                            "{name}: declared failure {} of precondition {} (`{}`) is not reached by any fixture in declared order",
                            code.as_str(),
                            i + 1,
                            expr.source()
                        ),
                    )
                    .with_index(i + 1),
                );
            }
        }
        if let Target::Adapter(a) = target {
            let precondition_codes: BTreeSet<Code> =
                target.preconditions().into_iter().map(|(_, c)| c).collect();
            let unreached =
                unreached_failures(&a.failures, &precondition_codes, &reached, &gateway);
            for (i, code) in a.failures.iter().enumerate() {
                let reached_here = !unreached.contains(code);
                if !reached_here {
                    diags.push(
                        cx.diag(
                            "unreachable_failure",
                            cx.contract_loc(name, Some("failures")),
                            name,
                            format!("{name}: declared failure {} is not reached by replaying any fixture response through the gateway", code.as_str()),
                        )
                        .with_index(i + 1),
                    );
                }
            }
        }
        if !has_ok {
            diags.push(cx.diag(
                "no_ok_fixture",
                cx.contract_loc(name, None),
                name,
                format!("{name}: no fixture in which every precondition holds produced an ok outcome with every effect true"),
            ));
        }
    }

    // Migrations.
    let mut migration_cases = 0;
    let mut fixtured: BTreeSet<&str> = BTreeSet::new();
    for f in &model.fixtures {
        let Some(path) = &f.migration else { continue };
        fixtured.insert(path.as_str());
        for m in &f.migrations {
            migration_cases += 1;
            let loc = cx.case_loc(&f.path, &m.name, "migration");
            match eph.migrate(path, &m.before, &m.after) {
                Ok(errors) => {
                    let ok = errors.is_empty();
                    for e in errors {
                        diags.push(cx.diag(
                            "migration",
                            loc.clone(),
                            path,
                            format!("{path} case {}: {e}", m.name),
                        ));
                    }
                    stream.push(StreamItem {
                        target: path.clone(),
                        case: format!("{}#{}", f.path, m.name),
                        status: if ok { "ok".into() } else { "INVALID".into() },
                        shape: ok,
                        side_effect: "migration".into(),
                    });
                }
                Err(e) => diags.push(cx.diag(
                    "migration",
                    loc,
                    path,
                    format!("{path} case {}: {e}", m.name),
                )),
            }
        }
    }
    if let Some(rel) = model.changelog.newest() {
        for id in &rel.changes {
            let Some(c) = model.changelog.change(id) else {
                continue;
            };
            if !c.migration.is_empty() && !fixtured.contains(c.migration.as_str()) {
                let section = format!("change.{id}");
                let line = parsed
                    .docs
                    .get("CHANGELOG.kvx")
                    .and_then(|d| d.section(&section))
                    .and_then(|s| s.get("migration"))
                    .map(|e| e.line)
                    .unwrap_or(1);
                diags.push(cx.diag(
                    "migration",
                    Location { file: "CHANGELOG.kvx".into(), line, col: 1, section: Some(section), key: Some("migration".into()) },
                    &c.migration,
                    format!("migration {} named by change {id} has no migration fixtures (fixtures/<module>/*.kvx with format cwr.migration-fixture)", c.migration),
                ));
            }
        }
    }
    Ok((
        RunReport {
            stream,
            duration_us: start.elapsed().as_micros(),
        },
        cases,
        migration_cases,
        diags,
    ))
}

/// A private scratch directory for the contract-test workers, removed when dropped.
struct Scratch(std::path::PathBuf);

impl Scratch {
    fn new() -> Result<Scratch, String> {
        let nanos = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_nanos())
            .unwrap_or(0);
        let dir =
            std::env::temp_dir().join(format!("cwr-contract-tests-{}-{nanos}", std::process::id()));
        std::fs::create_dir(&dir).map_err(|e| {
            format!(
                "cannot create the contract-test scratch directory {}: {e}",
                dir.display()
            )
        })?;
        Ok(Scratch(dir))
    }
}

impl Drop for Scratch {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

/// The first difference between the two suite runs.
struct Divergence<'r> {
    /// The differing stream item (or the item of the differing case) of each run.
    items: (Option<&'r StreamItem>, Option<&'r StreamItem>),
    /// What differs beyond the stream item, for the message.
    detail: String,
    /// A failure found by one run only, when the streams and cases agree.
    failure: Option<&'r Diagnostic>,
}

/// Compares the outcome streams, the per-case results and the failures found of two suite
/// runs; `None` when they agree on all three.
fn first_divergence<'r>(run1: &'r SuiteRun, run2: &'r SuiteRun) -> Option<Divergence<'r>> {
    let (s1, s2) = (&run1.0.stream, &run2.0.stream);
    let n = s1.len().max(s2.len());
    if let Some(i) = (0..n).find(|&i| s1.get(i) != s2.get(i)) {
        return Some(Divergence {
            items: (s1.get(i), s2.get(i)),
            detail: String::new(),
            failure: None,
        });
    }
    let (c1, c2) = (&run1.1, &run2.1);
    let n = c1.len().max(c2.len());
    if let Some(i) = (0..n).find(|&i| c1.get(i) != c2.get(i)) {
        let fmt = |c: Option<&CaseResult>| {
            c.map(|c| {
                format!(
                    "got {} at precondition {:?}, passed {}",
                    c.got, c.got_precondition, c.passed
                )
            })
            .unwrap_or_else(|| "(absent)".into())
        };
        let item = |c: Option<&CaseResult>| {
            c.and_then(|c| {
                s1.iter()
                    .find(|s| s.case == format!("{}#{}", c.file, c.case))
            })
        };
        return Some(Divergence {
            items: (item(c1.get(i)), item(c2.get(i))),
            detail: format!(
                "; case results: first run {}, second run {}",
                fmt(c1.get(i)),
                fmt(c2.get(i))
            ),
            failure: None,
        });
    }
    let (d1, d2) = (&run1.3, &run2.3);
    let only = |a: &'r [Diagnostic], b: &[Diagnostic]| a.iter().find(|d| !b.contains(d));
    only(d2, d1).or_else(|| only(d1, d2)).map(|d| Divergence {
        items: (None, None),
        detail: format!("; a failure found by one run only: {}", d.message),
        failure: Some(d),
    })
}

/// Runs the suite twice and returns the report and every failure.
///
/// The two runs share one scratch directory (the only place world code may write), so host
/// state carried from the first run into the second shows up as a difference. Failures found
/// only by the second run are reported with those of the first.
pub fn run(
    parsed: &Parsed,
    symbols: &SymbolTable,
    root: &std::path::Path,
) -> (Option<TestReport>, Vec<Diagnostic>) {
    let model = match parsed.require_model(Stage::ContractTests) {
        Ok(m) => m,
        Err(d) => return (None, d),
    };
    let scratch = match Scratch::new() {
        Ok(s) => s,
        Err(m) => {
            let loc = Location {
                file: "world.kvx".into(),
                line: 1,
                col: 1,
                section: None,
                key: None,
            };
            return (
                None,
                vec![Diagnostic::new(
                    Stage::ContractTests,
                    None,
                    "worker",
                    loc,
                    m,
                )],
            );
        }
    };
    let first = match run_suite(parsed, symbols, root, &scratch.0) {
        Ok(r) => r,
        Err(d) => return (None, d),
    };
    let second = match run_suite(parsed, symbols, root, &scratch.0) {
        Ok(r) => r,
        Err(d) => return (None, d),
    };
    drop(scratch);
    let divergence = first_divergence(&first, &second);
    let deterministic = divergence.is_none();
    let mut diags = first.3.clone();
    if let (true, Some(dv)) = (diags.is_empty(), &divergence) {
        let cx = Ctx { parsed, symbols };
        let (a, b) = dv.items;
        let detail = &dv.detail;
        let (file, case) = a
            .or(b)
            .map(|s| {
                s.case
                    .split_once('#')
                    .map(|(f, c)| (f.to_string(), c.to_string()))
                    .unwrap_or_default()
            })
            .unwrap_or_default();
        let target = a.or(b).map(|s| s.target.clone()).unwrap_or_default();
        let prefix = if a
            .or(b)
            .map(|s| s.side_effect == "migration")
            .unwrap_or(false)
        {
            "migration"
        } else {
            "case"
        };
        let fmt = |s: Option<&StreamItem>| {
            s.map(|s| {
                format!(
                    "({}, shape {}, {})",
                    s.status,
                    if s.shape { "ok" } else { "wrong" },
                    s.side_effect
                )
            })
            .unwrap_or_else(|| "(absent)".into())
        };
        diags.push(match dv.failure {
            Some(f) => {
                let mut d = f.clone();
                d.kind = "nondeterministic".into();
                d.message = format!("contract tests differ on the second run with the same seed{detail}");
                d
            }
            None => cx.diag(
                "nondeterministic",
                cx.case_loc(&file, &case, prefix),
                &target,
                format!("{target} case {case}: contract tests passed once and differ on the second run with the same seed: first run {}, second run {}{detail}", fmt(a), fmt(b)),
            ),
        });
    }
    for d in &second.3 {
        if !diags.contains(d) {
            diags.push(d.clone());
        }
    }
    let (run1, cases, migration_cases, _) = first;
    let run2 = second.0;
    let primitives = model.contracts.iter().map(|c| c.primitives.len()).sum();
    let adapters = model.adapters.iter().map(|a| a.adapters.len()).sum();
    let passed = diags.is_empty();
    (
        Some(TestReport {
            primitives,
            adapters,
            cases,
            migration_cases,
            runs: vec![run1, run2],
            deterministic,
            passed,
        }),
        diags,
    )
}

/// The contract-tests stage.
pub fn run_stage(cx: &mut Context) -> StageRun {
    let (parsed, symbols) = match (
        crate::prerequisite(&cx.parsed, Stage::ContractTests, Stage::Parse),
        crate::prerequisite(&cx.symbols, Stage::ContractTests, Stage::Resolve),
    ) {
        (Ok(p), Ok(s)) => (p, s),
        (Err(d), _) | (_, Err(d)) => return StageRun::Failed(d),
    };
    let (report, diags) = run(parsed, symbols, cx.root);
    cx.tests = report;
    if diags.is_empty() {
        StageRun::Passed
    } else {
        StageRun::Failed(diags)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::{Path, PathBuf};

    /// compiler.md section 4: a declared adapter failure counts as reached only through the
    /// gateway, or by its own precondition; a code produced at parameter validation (no
    /// precondition, never executed) or by an unrelated precondition does not count.
    #[test]
    fn adapter_failures_must_come_out_of_the_gateway() {
        let failures = [Code::Invalid, Code::Limit, Code::Timeout, Code::Unavailable];
        let pre: BTreeSet<Code> = [Code::Limit].into_iter().collect();
        // INVALID only from parameter validation, TIMEOUT only from a precondition index of
        // another code, LIMIT by its precondition, UNAVAILABLE through the gateway.
        let reached: BTreeSet<(Code, Option<usize>)> = [
            (Code::Invalid, None),
            (Code::Limit, Some(1)),
            (Code::Unavailable, None),
        ]
        .into_iter()
        .collect();
        let gateway: BTreeSet<Code> = [Code::Unavailable].into_iter().collect();
        let got = unreached_failures(&failures, &pre, &reached, &gateway);
        assert_eq!(
            got,
            [Code::Invalid, Code::Timeout]
                .into_iter()
                .collect::<BTreeSet<_>>()
        );
        let gateway: BTreeSet<Code> = [Code::Invalid, Code::Timeout, Code::Unavailable]
            .into_iter()
            .collect();
        assert!(unreached_failures(&failures, &pre, &reached, &gateway).is_empty());
    }

    fn copy_tree(from: &Path, to: &Path) {
        std::fs::create_dir_all(to).unwrap();
        for e in std::fs::read_dir(from).unwrap() {
            let e = e.unwrap();
            let name = e.file_name();
            if name == "README.md" {
                continue;
            }
            let p = e.path();
            if p.is_dir() {
                copy_tree(&p, &to.join(&name));
            } else {
                std::fs::copy(&p, to.join(&name)).unwrap();
            }
        }
    }

    /// The refworld suite passes, twice, with equal outcome streams. Prints the counts the task
    /// driver reports as evidence.
    #[test]
    fn refworld_contract_suite() {
        let repo = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..");
        let src = repo.join("examples/refworld");
        if !src.is_dir()
            || std::process::Command::new(crate::ephemeral::python())
                .arg("-V")
                .output()
                .is_err()
        {
            eprintln!("skipped: no refworld or no python3");
            return;
        }
        let dir = std::env::temp_dir().join(format!("cwr-1_2-unit-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        copy_tree(&src, &dir);
        let options = crate::Options {
            through: Stage::Lint,
            ..Default::default()
        };
        let mut cx = Context::new(&dir, &options);
        for st in [Stage::Parse, Stage::Resolve, Stage::Lint] {
            assert!(
                matches!(crate::run_stage(st, &mut cx), StageRun::Passed),
                "{st} failed"
            );
        }
        let (report, diags) = run(
            cx.parsed.as_ref().unwrap(),
            cx.symbols.as_ref().unwrap(),
            &dir,
        );
        let _ = std::fs::remove_dir_all(&dir);
        assert!(diags.is_empty(), "{diags:?}");
        let r = report.unwrap();
        assert!(r.passed && r.deterministic);
        assert_eq!(r.runs.len(), 2);
        assert_eq!(r.runs[0].stream, r.runs[1].stream);
        let failures = r.cases.iter().filter(|c| c.expect != "ok").count();
        println!(
            "REFWORLD_SUITE primitives={} adapters={} cases={} failure_cases={} ok_cases={} migration_cases={} stream={} deterministic={}",
            r.primitives,
            r.adapters,
            r.cases.len(),
            failures,
            r.cases.len() - failures,
            r.migration_cases,
            r.runs[0].stream.len(),
            r.deterministic
        );
        let json = r.to_json().unwrap();
        assert!(json.contains("\"format\": \"cwr.tests\""));
    }

    /// The second-run comparison (compiler.md section 4, compile-time half of clause 1.6) on a
    /// lib/ whose outcome depends on host state shared by the two runs: a counter file in the
    /// workers' scratch directory (their working directory, the only place world code may
    /// write). Lint refuses `open` in lib/ (CWR-L001), so the stage is reached here directly
    /// after parse and resolve. Prints the diagnostic the task driver reports as evidence.
    #[test]
    fn nondeterministic_lib_is_caught_by_the_second_run() {
        let repo = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..");
        let src = repo.join("examples/refworld");
        if !src.is_dir()
            || std::process::Command::new(crate::ephemeral::python())
                .arg("-V")
                .output()
                .is_err()
        {
            eprintln!("skipped: no refworld or no python3");
            return;
        }
        let dir = std::env::temp_dir().join(format!("cwr-1_2-nondet-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        copy_tree(&src, &dir);
        let lib = dir.join("lib/fs.py");
        let head = "def write(state: Any, path: str, content: bytes) -> None:\n";
        let text = std::fs::read_to_string(&lib).unwrap();
        assert!(text.contains(head), "lib/fs.py has no write()");
        let probe = format!(
            "{head}    with open(\"counter.txt\", \"a+\") as handle:\n        handle.seek(0)\n        seen = len(handle.read())\n        handle.write(\"x\")\n    if seen >= 3:\n        raise KeyError(\"host-dependent outcome\")\n"
        );
        std::fs::write(&lib, text.replacen(head, &probe, 1)).unwrap();
        let options = crate::Options {
            through: Stage::Resolve,
            ..Default::default()
        };
        let mut cx = Context::new(&dir, &options);
        for st in [Stage::Parse, Stage::Resolve] {
            assert!(
                matches!(crate::run_stage(st, &mut cx), StageRun::Passed),
                "{st} failed"
            );
        }
        let (report, diags) = run(
            cx.parsed.as_ref().unwrap(),
            cx.symbols.as_ref().unwrap(),
            &dir,
        );
        let _ = std::fs::remove_dir_all(&dir);
        let r = report.unwrap();
        assert!(!r.passed && !r.deterministic);
        let hit: Vec<&Diagnostic> = diags
            .iter()
            .filter(|d| d.kind == "nondeterministic")
            .collect();
        assert!(!hit.is_empty(), "{diags:?}");
        let d = hit[0];
        assert!(d
            .message
            .contains("passed once and differ on the second run"));
        println!(
            "NONDETERMINISM_PROBE kind={} file={} line={} section={} primitive={} diagnostics={} message={}",
            d.kind,
            d.file,
            d.line,
            d.section.as_deref().unwrap_or(""),
            d.primitive.as_deref().unwrap_or(""),
            diags.len(),
            d.message
        );
    }
}
