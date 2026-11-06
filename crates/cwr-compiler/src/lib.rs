// Copyright Sidiora Labs Inc. CWR® 2026-2027
// SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0

//! CWR World Compiler (spec/cwr/compiler.md; task 1.1 front end, case CWR-002).
//!
//! The pipeline has eight stages run in a fixed order (clause 4.1):
//! parse, resolve, lint, contract_tests, diff, changelog_gate, generate, package. The first
//! failing stage stops the run; its report lists every diagnostic of that stage and nothing
//! from later stages. All eight stages are implemented: the front end (task 1.1), contract
//! tests, diff and changelog gate (task 1.2), generate ([`manifest`], [`generate`]) and package
//! ([`package`]) (task 1.3).
//!
//! Stage of each diagnostic class (compiler.md sections 2 and 3, world-source.md section 14):
//!
//! | Stage | Diagnostics |
//! |---|---|
//! | parse | KVX syntax, duplicate section or key, missing key, bad value, changelog and fixture consistency, `meta.example` without `--allow-example`, CWR-C001 (unknown file, section or key; shape rule) |
//! | resolve | CWR-C002 duplicate primitive, CWR-C003 undeclared module, CWR-L002 contract and `lib/` mismatch, CWR-V002 undefined reference or expression outside the language |
//! | lint | CWR-V001 non-read_only call in a precondition, CWR-F001, CWR-F002, CWR-D001, CWR-L001, CWR-M001 (source-level half: empty, missing or nonexistent migration) |
//!
//! Diagnostics are sorted by file, line, column, code and message, so two runs over one
//! source print identical reports.
//!
//! Exit codes (compiler.md section 9): 0 success; 2 parse or resolve; 3 lint; 4 contract
//! tests; 5 diff; 6 changelog gate; 7 package; 1 any other error.

#![warn(missing_docs)]

pub mod changelog_gate;
pub mod contract_tests;
pub mod diff;
pub mod ephemeral;
pub mod experimental;
pub mod expr;
pub mod lint;
pub mod manifest;
pub mod package;
pub mod parse;
pub mod pyimports;
pub mod resolve;
pub mod version;
/// Manual and SDK generators (task 1.3).
pub mod generate {
    pub mod manual;
    pub mod sdk;
}

use std::fmt;
use std::path::{Path, PathBuf};

use serde::Serialize;

/// Pipeline stages, in execution order.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Stage {
    /// World Source directory to typed sections and a file inventory.
    Parse,
    /// Symbol table: primitives, adapters, modules and references.
    Resolve,
    /// Source-level lint rules (compiler.md section 3).
    Lint,
    /// Contract tests run twice in an Ephemeral Instance.
    ContractTests,
    /// ContractDiff against the previous Build.
    Diff,
    /// Every diff entry is matched by a changelog change.
    ChangelogGate,
    /// Manual, SDK and contracts generation.
    Generate,
    /// Content-addressed Build directory.
    Package,
}

impl Stage {
    /// Every stage, in execution order; `ALL[s as usize] == s` for each stage `s`.
    pub const ALL: [Stage; 8] = [
        Stage::Parse,
        Stage::Resolve,
        Stage::Lint,
        Stage::ContractTests,
        Stage::Diff,
        Stage::ChangelogGate,
        Stage::Generate,
        Stage::Package,
    ];

    /// The stable snake_case name used in reports and on the command line.
    pub fn as_str(self) -> &'static str {
        match self {
            Stage::Parse => "parse",
            Stage::Resolve => "resolve",
            Stage::Lint => "lint",
            Stage::ContractTests => "contract_tests",
            Stage::Diff => "diff",
            Stage::ChangelogGate => "changelog_gate",
            Stage::Generate => "generate",
            Stage::Package => "package",
        }
    }

    /// The stage whose [`Stage::as_str`] name is `s`, or `None` for any other text.
    pub fn parse_name(s: &str) -> Option<Stage> {
        Stage::ALL.into_iter().find(|st| st.as_str() == s)
    }

    /// Exit code of `cwr build` when this stage fails (compiler.md section 9).
    pub fn exit_code(self) -> i32 {
        match self {
            Stage::Parse | Stage::Resolve => 2,
            Stage::Lint => 3,
            Stage::ContractTests => 4,
            Stage::Diff => 5,
            Stage::ChangelogGate => 6,
            Stage::Generate => 1,
            Stage::Package => 7,
        }
    }
}

impl fmt::Display for Stage {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

/// A source location: file relative to the World Source root, one-based line and column,
/// and where applicable the KVX section and key.
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Serialize)]
pub struct Location {
    /// Path relative to the World Source root, with `/` separators.
    pub file: String,
    /// One-based line.
    pub line: usize,
    /// One-based column.
    pub col: usize,
    /// Enclosing KVX section, when the file is KVX and the line is inside one.
    pub section: Option<String>,
    /// KVX key on the line, when the line holds an entry.
    pub key: Option<String>,
}

/// One located diagnostic.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct Diagnostic {
    /// The stage that reported it; [`compile`] sets it to the stage that stopped the run.
    pub stage: Stage,
    /// Lint code from compiler.md section 3, when the diagnostic has one.
    pub code: Option<String>,
    /// Stable lowercase class label.
    pub kind: String,
    /// Path relative to the World Source root.
    pub file: String,
    /// One-based line.
    pub line: usize,
    /// One-based column.
    pub col: usize,
    /// Enclosing KVX section, if any.
    pub section: Option<String>,
    /// KVX key on the line, if any.
    pub key: Option<String>,
    /// One-based index into a list value (precondition index, failures index), if any.
    pub index: Option<usize>,
    /// The primitive or adapter concerned, if any.
    pub primitive: Option<String>,
    /// Human explanation; part of the deterministic sort key.
    pub message: String,
    /// Other locations the diagnostic names (the first declaration for CWR-C002, the
    /// implementation for CWR-L002).
    pub related: Vec<Location>,
}

impl Diagnostic {
    /// A diagnostic at `loc` with no index, primitive or related locations.
    pub fn new(
        stage: Stage,
        code: Option<&str>,
        kind: &str,
        loc: Location,
        message: impl Into<String>,
    ) -> Self {
        Diagnostic {
            stage,
            code: code.map(str::to_string),
            kind: kind.to_string(),
            file: loc.file,
            line: loc.line,
            col: loc.col,
            section: loc.section,
            key: loc.key,
            index: None,
            primitive: None,
            message: message.into(),
            related: Vec::new(),
        }
    }

    /// The same diagnostic carrying a one-based list index.
    pub fn with_index(mut self, index: usize) -> Self {
        self.index = Some(index);
        self
    }

    /// The same diagnostic naming the primitive or adapter concerned.
    pub fn with_primitive(mut self, name: &str) -> Self {
        self.primitive = Some(name.to_string());
        self
    }

    /// The same diagnostic with one more related location appended.
    pub fn with_related(mut self, loc: Location) -> Self {
        self.related.push(loc);
        self
    }

    fn sort_key(&self) -> (&str, usize, usize, &str, &str) {
        (
            &self.file,
            self.line,
            self.col,
            self.code.as_deref().unwrap_or(""),
            &self.message,
        )
    }
}

impl fmt::Display for Diagnostic {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            f,
            "{}:{}:{}: {}: {}",
            self.file,
            self.line,
            self.col,
            self.stage,
            self.code.as_deref().unwrap_or(&self.kind)
        )?;
        let mut at = Vec::new();
        if let Some(s) = &self.section {
            at.push(format!("[{s}]"));
        }
        if let Some(k) = &self.key {
            at.push(k.clone());
        }
        if let Some(i) = self.index {
            at.push(format!("#{i}"));
        }
        if !at.is_empty() {
            write!(f, " {}", at.join(" "))?;
        }
        write!(f, ": {}", self.message)?;
        for r in &self.related {
            write!(f, " (see {}:{}:{})", r.file, r.line, r.col)?;
        }
        Ok(())
    }
}

/// Sorts diagnostics into the deterministic report order and removes exact duplicates.
pub fn sort_diagnostics(diags: &mut Vec<Diagnostic>) {
    diags.sort_by(|a, b| a.sort_key().cmp(&b.sort_key()));
    diags.dedup();
}

/// Status of one stage in a report.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum StageStatus {
    /// Ran with no diagnostics.
    Passed,
    /// Ran and reported at least one diagnostic; the run stopped here.
    Failed,
    /// Not run because an earlier stage failed, or the run ended earlier.
    NotRun,
}

impl StageStatus {
    /// The snake_case name the JSON report uses for this status.
    pub fn as_str(self) -> &'static str {
        match self {
            StageStatus::Passed => "passed",
            StageStatus::Failed => "failed",
            StageStatus::NotRun => "not_run",
        }
    }
}

/// The result of one stage within a [`Report`].
#[derive(Debug, Clone, Serialize)]
pub struct StageReport {
    /// The stage this entry describes.
    pub stage: Stage,
    /// Whether it passed, failed or did not run.
    pub status: StageStatus,
    /// Its sorted diagnostics; empty unless `status` is `Failed`.
    pub diagnostics: Vec<Diagnostic>,
}

/// The outcome of one compiler run.
#[derive(Debug, Clone, Serialize)]
pub struct Report {
    /// The source directory as given.
    pub source: String,
    /// The last stage the run was asked to execute.
    pub through: Stage,
    /// One entry per stage, in execution order.
    pub stages: Vec<StageReport>,
    /// The stage that failed and stopped the run, if any.
    pub stopped_at: Option<Stage>,
    /// Process exit code of `cwr build` (compiler.md section 9).
    pub exit_code: i32,
    /// Human summary line.
    pub summary: String,
    /// The packaged Build (`cwr build` past the package stage), if any.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub build: Option<package::Built>,
}

impl Report {
    /// The report of a run over `root` through `through` before any stage has run: every
    /// stage `not_run`, exit code 0 and the success summary.
    pub fn start(root: &Path, through: Stage) -> Report {
        Report {
            source: root.display().to_string(),
            through,
            stages: Stage::ALL
                .iter()
                .map(|&stage| StageReport {
                    stage,
                    status: StageStatus::NotRun,
                    diagnostics: Vec::new(),
                })
                .collect(),
            stopped_at: None,
            exit_code: 0,
            summary: format!("ok: stages parse through {through} passed"),
            build: None,
        }
    }

    /// Records that `stage` passed.
    pub fn pass(&mut self, stage: Stage) {
        if let Some(r) = self.stages.iter_mut().find(|r| r.stage == stage) {
            r.status = StageStatus::Passed;
        }
    }

    /// Stops the run at `stage`: its diagnostics sorted and stamped with `stage`, the stage's
    /// exit code (compiler.md section 9) and the failure summary.
    pub fn fail(&mut self, stage: Stage, mut diags: Vec<Diagnostic>) {
        sort_diagnostics(&mut diags);
        for d in &mut diags {
            d.stage = stage;
        }
        let n = diags.len();
        if let Some(r) = self.stages.iter_mut().find(|r| r.stage == stage) {
            r.status = StageStatus::Failed;
            r.diagnostics = diags;
        }
        self.stopped_at = Some(stage);
        self.exit_code = stage.exit_code();
        self.summary = format!(
            "failed at stage {stage}: {n} diagnostic{}; nothing written",
            if n == 1 { "" } else { "s" }
        );
    }

    /// The entry for `stage`, or `None` when the report has none.
    pub fn stage(&self, stage: Stage) -> Option<&StageReport> {
        self.stages.iter().find(|s| s.stage == stage)
    }

    /// Every diagnostic of the run (only the stopping stage ever has any).
    pub fn diagnostics(&self) -> impl Iterator<Item = &Diagnostic> {
        self.stages.iter().flat_map(|s| s.diagnostics.iter())
    }

    /// Pretty JSON rendering with a final newline. Serialising a report cannot fail (every
    /// field is a string, integer, enum or list of them); should serde ever report an error,
    /// the result is a JSON object whose single `error` member carries its message.
    pub fn to_json(&self) -> String {
        let mut s = serde_json::to_string_pretty(self).unwrap_or_else(|e| {
            serde_json::json!({ "error": format!("report serialisation: {e}") }).to_string()
        });
        s.push('\n');
        s
    }

    /// Text rendering: one line per diagnostic, then the stage line and the summary.
    pub fn to_text(&self) -> String {
        let mut out = String::new();
        for d in self.diagnostics() {
            out.push_str(&d.to_string());
            out.push('\n');
        }
        let stages: Vec<String> = self
            .stages
            .iter()
            .map(|s| format!("{}={}", s.stage, s.status.as_str()))
            .collect();
        out.push_str(&format!("stages: {}\n", stages.join(" ")));
        out.push_str(&self.summary);
        out.push('\n');
        out
    }
}

/// Compiler options.
#[derive(Debug, Clone)]
pub struct Options {
    /// Accept a source whose world.kvx has `meta.example = true` (compiler.md section 9).
    pub allow_example: bool,
    /// The last stage to run: `Lint` for `cwr lint`, `Package` for `cwr build`.
    pub through: Stage,
    /// `--previous <hash|dir>`: the Build (or, for authoring, World Source) the diff stage
    /// compares against; `None` means the newest Build of the same name under `out`.
    pub previous: Option<String>,
    /// `--out <dir>`: the Build root; `None` means `build/`.
    pub out: Option<PathBuf>,
}

impl Default for Options {
    fn default() -> Self {
        Options {
            allow_example: false,
            through: Stage::Package,
            previous: None,
            out: None,
        }
    }
}

/// State carried between stages.
pub struct Context<'a> {
    /// The World Source directory being compiled.
    pub root: &'a Path,
    /// Options of this run.
    pub options: &'a Options,
    /// Parse result, set by the parse stage.
    pub parsed: Option<parse::Parsed>,
    /// Symbol table, set by the resolve stage.
    pub symbols: Option<resolve::SymbolTable>,
    /// Contract-test report (`tests.json`), set by the contract_tests stage.
    pub tests: Option<contract_tests::TestReport>,
    /// ContractDiff (`contract_diff.json`), set by the diff stage.
    pub diff: Option<diff::ContractDiff>,
    /// Generated files, set by the generate stage.
    pub generated: Option<manifest::Generated>,
    /// The packaged Build, set by the package stage.
    pub built: Option<package::Built>,
}

impl<'a> Context<'a> {
    /// A context with no stage results yet.
    pub fn new(root: &'a Path, options: &'a Options) -> Self {
        Context {
            root,
            options,
            parsed: None,
            symbols: None,
            tests: None,
            diff: None,
            generated: None,
            built: None,
        }
    }
}

/// The output of `stage`'s prerequisite `needs`, or a located diagnostic for `stage` when
/// `needs` has not run (a stage invoked out of pipeline order).
pub fn prerequisite<T>(
    slot: &Option<T>,
    stage: Stage,
    needs: Stage,
) -> Result<&T, Vec<Diagnostic>> {
    slot.as_ref().ok_or_else(|| {
        vec![Diagnostic::new(
            stage,
            None,
            "internal",
            Location {
                file: "world.kvx".into(),
                line: 1,
                col: 1,
                section: None,
                key: None,
            },
            format!("the {stage} stage ran before the {needs} stage it depends on"),
        )]
    })
}

/// The result of running one stage.
pub enum StageRun {
    /// The stage succeeded and stored its output in the context.
    Passed,
    /// The stage failed with these diagnostics.
    Failed(Vec<Diagnostic>),
}

/// Runs one stage against the context. A stage whose prerequisite has not run fails with
/// an `internal` diagnostic.
pub fn run_stage(stage: Stage, cx: &mut Context) -> StageRun {
    match stage {
        Stage::Parse => match parse::parse(cx.root, cx.options.allow_example) {
            Ok(p) => {
                cx.parsed = Some(p);
                StageRun::Passed
            }
            Err(d) => StageRun::Failed(d),
        },
        Stage::Resolve => {
            match prerequisite(&cx.parsed, stage, Stage::Parse).and_then(resolve::resolve) {
                Ok(t) => {
                    cx.symbols = Some(t);
                    StageRun::Passed
                }
                Err(d) => StageRun::Failed(d),
            }
        }
        Stage::Lint => {
            let (parsed, symbols) = match (
                prerequisite(&cx.parsed, stage, Stage::Parse),
                prerequisite(&cx.symbols, stage, Stage::Resolve),
            ) {
                (Ok(p), Ok(s)) => (p, s),
                (Err(d), _) | (_, Err(d)) => return StageRun::Failed(d),
            };
            let d = lint::lint(parsed, symbols);
            if d.is_empty() {
                StageRun::Passed
            } else {
                StageRun::Failed(d)
            }
        }
        Stage::ContractTests => contract_tests::run_stage(cx),
        Stage::Diff => diff::run_stage(cx),
        Stage::ChangelogGate => changelog_gate::run_stage(cx),
        Stage::Generate => manifest::run_stage(cx),
        Stage::Package => package::run_stage(cx),
    }
}

/// Runs the pipeline over a World Source directory up to `options.through`.
pub fn compile(root: &Path, options: &Options) -> Report {
    let mut cx = Context::new(root, options);
    let mut report = Report::start(root, options.through);
    for stage in Stage::ALL {
        if stage > options.through {
            break;
        }
        match run_stage(stage, &mut cx) {
            StageRun::Passed => report.pass(stage),
            StageRun::Failed(diags) => {
                report.fail(stage, diags);
                break;
            }
        }
    }
    if let Some(b) = &cx.built {
        if report.exit_code == 0 {
            report.summary = format!(
                "{} {} at {}",
                if b.already_built {
                    "already built:"
                } else {
                    "built"
                },
                b.hash,
                b.dir
            );
        }
    }
    report.build = cx.built;
    report
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn stage_status_names_match_the_json_report() {
        for s in [
            StageStatus::Passed,
            StageStatus::Failed,
            StageStatus::NotRun,
        ] {
            assert_eq!(serde_json::to_value(s).ok(), Some(s.as_str().into()));
        }
    }

    #[test]
    fn a_stage_run_out_of_order_fails_with_a_located_diagnostic() {
        let options = Options::default();
        let root = Path::new(".");
        for (stage, needs) in [
            (Stage::Resolve, "parse"),
            (Stage::ContractTests, "parse"),
            (Stage::Diff, "parse"),
            (Stage::ChangelogGate, "parse"),
            (Stage::Generate, "parse"),
        ] {
            let mut cx = Context::new(root, &options);
            match run_stage(stage, &mut cx) {
                StageRun::Failed(d) => {
                    assert_eq!(d.len(), 1, "{stage}");
                    assert_eq!(
                        (d[0].file.as_str(), d[0].line, d[0].col),
                        ("world.kvx", 1, 1)
                    );
                    assert!(d[0].message.contains(needs), "{stage}: {}", d[0].message);
                }
                _ => panic!("{stage} ran without its prerequisite"),
            }
        }
    }

    #[test]
    fn report_stage_lookup_does_not_index() {
        let report = Report {
            source: String::new(),
            through: Stage::Lint,
            stages: Vec::new(),
            stopped_at: None,
            exit_code: 0,
            summary: String::new(),
            build: None,
        };
        assert!(report.stage(Stage::Parse).is_none());
        assert!(report.to_json().ends_with("}\n"));
    }
}
