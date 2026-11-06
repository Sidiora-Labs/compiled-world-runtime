// Copyright Sidiora Labs Inc. CWR® 2026-2027
// SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0

//! Experimental ablation Builds (compiler.md section 10; clauses 10.1 to 10.5; task 3.2, case
//! CWR-013).
//!
//! An experimental Build is the control Build of the same World Source with exactly one pillar
//! ablated by one flag. [`build`] runs the ordinary pipeline (parse through generate, so the
//! contract tests, the diff against the control's previous Build and the changelog gate all run
//! on the unmodified source), applies the flag's transform and packages the result under the
//! experimental root `xbuild/` beside the Build root, with `experimental = true` and
//! `experimental_flag = "<flag>"` in `manifest.kvx`:
//!
//! | Flag | Transform | Module |
//! |---|---|---|
//! | `--x-drift <file>` | the drift file is validated with the source (parse, resolve, lint, and every entry applied to the compiled table) and byte-copied to [`cwr_kernel::drift_injector::SCHEDULE_PATH`]; nothing else changes | [`drift`] |
//! | `--x-silent-change` | none besides the manifest marker; the kernel's surface renders no recompile notice | [`notice`] |
//! | `--x-version-only` | none besides the manifest marker; the surface renders only the notice's version line | [`notice`] |
//! | `--x-one-primitive` | `limits.primitive_calls = 1` in `contracts.json`, manual and SDK regenerated from it, and the sentence stating the limit appended to the manual's limits section | [`one_primitive`] |
//!
//! [`attribution`] is the check of clause 10.5: it compares a control Build with an experimental
//! Build file by file and through the ContractDiff, lists the entries the flag explains, and lists
//! every other difference as unattributable. A pair with an unattributable difference was not
//! built from one source and is refused by the harness.
//!
//! Every experimental Build hashes over the same inputs as its control except what its flag
//! changes, so two runs of one flag over one source produce one hash.

pub mod drift;
pub mod notice;
pub mod one_primitive;

use std::fs;
use std::path::{Path, PathBuf};

use cwr_contract::kvx;
use serde_json::{json, Value};

use crate::diff::{compute, Surface};
use crate::manifest::Generated;
use crate::package;
use crate::{run_stage, Context, Diagnostic, Location, Options, Report, Stage, StageRun};

/// One experimental flag.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Flag {
    /// `--x-drift <file>`: the drift schedule file.
    Drift(PathBuf),
    /// `--x-silent-change`.
    SilentChange,
    /// `--x-version-only`.
    VersionOnly,
    /// `--x-one-primitive`.
    OnePrimitive,
}

/// The four flag names as `manifest.kvx` and every ledger record carry them.
pub const FLAG_NAMES: [&str; 4] = [
    cwr_kernel::drift_injector::FLAG,
    notice::SILENT_CHANGE,
    notice::VERSION_ONLY,
    one_primitive::FLAG,
];

impl Flag {
    /// The flag name recorded in `manifest.kvx` and stamped on every ledger record: the command
    /// line flag without its leading dashes.
    pub fn name(&self) -> &'static str {
        match self {
            Flag::Drift(_) => cwr_kernel::drift_injector::FLAG,
            Flag::SilentChange => notice::SILENT_CHANGE,
            Flag::VersionOnly => notice::VERSION_ONLY,
            Flag::OnePrimitive => one_primitive::FLAG,
        }
    }

    /// The flag for a command line argument `--x-...`; `value` is the argument after it, used
    /// only by `--x-drift`. `None` for anything else.
    pub fn from_arg(arg: &str, value: Option<&str>) -> Option<Flag> {
        match arg {
            "--x-drift" => value.map(|v| Flag::Drift(PathBuf::from(v))),
            "--x-silent-change" => Some(Flag::SilentChange),
            "--x-version-only" => Some(Flag::VersionOnly),
            "--x-one-primitive" => Some(Flag::OnePrimitive),
            _ => None,
        }
    }
}

/// The experimental Build root for `options`: `xbuild/` beside the Build root (`--out`, default
/// `build/`), the same place [`crate::diff::build_roots`] resolves Build hashes in.
pub fn xbuild_root(options: &Options) -> PathBuf {
    let roots = crate::diff::build_roots(options);
    roots
        .get(1)
        .cloned()
        .unwrap_or_else(|| PathBuf::from("xbuild"))
}

fn at(file: &str) -> Location {
    Location {
        file: file.to_string(),
        line: 1,
        col: 1,
        section: None,
        key: None,
    }
}

/// A single diagnostic of `stage` located at the top of `file`.
pub(crate) fn diagnostic(stage: Stage, kind: &str, file: &str, message: String) -> Diagnostic {
    Diagnostic::new(stage, None, kind, at(file), message)
}

/// Runs the pipeline over `root` with one experimental flag and packages the experimental Build
/// under [`xbuild_root`]. Stages and exit codes are those of `cwr build` (compiler.md section 9):
/// a drift file that does not parse, names an unknown primitive or cannot be applied stops the
/// run at parse (exit 2); one that would itself be a lint error stops it at lint (exit 3).
pub fn build(root: &Path, options: &Options, flag: &Flag) -> Report {
    let options = Options {
        through: Stage::Package,
        ..options.clone()
    };
    let mut report = Report::start(root, Stage::Package);
    let xroot = xbuild_root(&options);
    let staged = match flag {
        Flag::Drift(file) => match drift::Staged::new(root, file, &xroot) {
            Ok(s) => Some(s),
            Err(d) => {
                report.fail(Stage::Parse, vec![*d]);
                return report;
            }
        },
        _ => None,
    };
    let source = staged.as_ref().map(|s| s.dir.as_path()).unwrap_or(root);
    let mut cx = Context::new(source, &options);
    for stage in Stage::ALL {
        if stage == Stage::Package {
            break;
        }
        let run = match run_stage(stage, &mut cx) {
            StageRun::Passed if stage == Stage::Parse => match &staged {
                Some(s) => s.check(&cx),
                None => StageRun::Passed,
            },
            other => other,
        };
        match run {
            StageRun::Passed => report.pass(stage),
            StageRun::Failed(mut diags) => {
                if let Some(s) = &staged {
                    s.relocate(&mut diags);
                }
                report.fail(stage, diags);
                return report;
            }
        }
    }
    let transformed = match transform(&cx, flag) {
        Ok(t) => t,
        Err(m) => {
            let d = diagnostic(Stage::Generate, "generator", "world.kvx", m);
            report.fail(Stage::Generate, vec![d]);
            return report;
        }
    };
    let extra = staged
        .as_ref()
        .map(|s| {
            vec![(
                cwr_kernel::drift_injector::SCHEDULE_PATH,
                s.schedule.clone(),
            )]
        })
        .unwrap_or_default();
    let packaged = package::Contents::of(&cx).and_then(|c| {
        let c = package::Contents {
            generated: &transformed,
            experimental_flag: Some(flag.name()),
            extra: &extra,
            ..c
        };
        package::package_into(&xroot, &c)
    });
    match packaged {
        Ok(built) => {
            report.pass(Stage::Package);
            report.summary = format!(
                "{} {} ({}) at {}",
                if built.already_built {
                    "already built:"
                } else {
                    "built experimental"
                },
                built.hash,
                flag.name(),
                built.dir
            );
            report.build = Some(built);
        }
        Err(m) => {
            let d = diagnostic(Stage::Package, "package", "world.kvx", m);
            report.fail(Stage::Package, vec![d]);
        }
    }
    report
}

fn transform(cx: &Context, flag: &Flag) -> Result<Generated, String> {
    let g = cx.generated.as_ref().ok_or("generate did not run")?;
    match flag {
        Flag::OnePrimitive => {
            let parsed = cx.parsed.as_ref().ok_or("parse did not run")?;
            one_primitive::transform(g, &crate::manifest::prose(parsed))
        }
        Flag::Drift(_) | Flag::SilentChange | Flag::VersionOnly => Ok(g.clone()),
    }
}

// ---------------------------------------------------------------------------------------------
// Attribution (clause 10.5)
// ---------------------------------------------------------------------------------------------

/// The `[build]` keys of a Build's `manifest.kvx`, the recorded `hash` excluded.
fn manifest_keys(dir: &Path) -> Result<Vec<(String, Value)>, String> {
    let text = fs::read_to_string(dir.join("manifest.kvx"))
        .map_err(|e| format!("Build {}: manifest.kvx: {e}", dir.display()))?;
    let doc = kvx::parse("manifest.kvx", &text)
        .map_err(|_| format!("Build {}: manifest.kvx does not parse", dir.display()))?;
    Ok(doc
        .sections
        .iter()
        .flat_map(|s| s.entries.iter())
        .filter(|e| e.key != "hash")
        .map(|e| (e.key.clone(), e.value.value.to_value()))
        .collect())
}

/// The experimental flag a Build's manifest records; `None` for a control Build.
pub fn flag_of(dir: &Path) -> Result<Option<String>, String> {
    let keys = manifest_keys(dir)?;
    let get = |k: &str| keys.iter().find(|(n, _)| n == k).map(|(_, v)| v.clone());
    Ok(match get("experimental") {
        Some(Value::Bool(true)) => Some(
            get("experimental_flag")
                .and_then(|v| v.as_str().map(str::to_string))
                .unwrap_or_default(),
        ),
        _ => None,
    })
}

fn files(dir: &Path, rel: &str, out: &mut Vec<String>) -> Result<(), String> {
    let here = if rel.is_empty() {
        dir.to_path_buf()
    } else {
        dir.join(rel)
    };
    let mut entries: Vec<_> = fs::read_dir(&here)
        .map_err(|e| format!("read {}: {e}", here.display()))?
        .filter_map(|e| e.ok())
        .collect();
    entries.sort_by_key(|e| e.file_name());
    for e in entries {
        let name = e.file_name().to_string_lossy().into_owned();
        let child = if rel.is_empty() {
            name
        } else {
            format!("{rel}/{name}")
        };
        if e.file_type().map(|t| t.is_dir()).unwrap_or(false) {
            out.push(format!("{child}/"));
            files(dir, &child, out)?;
        } else {
            out.push(child);
        }
    }
    Ok(())
}

fn read(dir: &Path, rel: &str) -> Vec<u8> {
    fs::read(dir.join(rel)).unwrap_or_default()
}

/// Compares a control Build with an experimental Build (clause 10.5). Both must verify; the first
/// must be a control Build and the second an experimental one. Returns a canonical JSON report:
///
/// | Key | Content |
/// |---|---|
/// | `control`, `experimental` | the two Build hashes |
/// | `flag` | the experimental Build's flag |
/// | `entries`, `doc_entries` | the ContractDiff from control to experimental (compiler.md section 5) |
/// | `experimental_entries` | what the flag changes outside the compared surface: the embedded drift schedule, the notice-rendering marker or the manual sentence |
/// | `unattributable` | every ContractDiff entry and every file difference the flag does not explain |
/// | `attributable` | true when `unattributable` is empty |
pub fn attribution(control: &Path, experimental: &Path) -> Result<Value, String> {
    let hc = package::verify(control)?;
    let hx = package::verify(experimental)?;
    if let Some(f) = flag_of(control)? {
        return Err(format!(
            "{} is an experimental Build ({f}), not a control Build",
            control.display()
        ));
    }
    let flag = flag_of(experimental)?.ok_or_else(|| {
        format!(
            "{} is a control Build, not an experimental Build",
            experimental.display()
        )
    })?;
    let diff = compute(
        &Surface::from_build(control)?,
        &Surface::from_build(experimental)?,
        None,
    );
    let mut unattributable: Vec<String> = Vec::new();
    for e in &diff.entries {
        if !(flag == one_primitive::FLAG && one_primitive::is_flag_entry(e)) {
            unattributable.push(format!("contract diff entry {}", e.label()));
        }
    }
    if flag == one_primitive::FLAG && diff.entries.len() != 1 {
        unattributable.push(format!(
            "{} expects exactly one limit_changed entry, found {}",
            flag,
            diff.entries.len()
        ));
    }
    for e in &diff.doc_entries {
        unattributable.push(format!("doc entry {}", e.label()));
    }
    let experimental_entries = match flag.as_str() {
        cwr_kernel::drift_injector::FLAG => vec![drift::entry(experimental)?],
        notice::SILENT_CHANGE | notice::VERSION_ONLY => vec![notice::entry(&flag)],
        one_primitive::FLAG => vec![one_primitive::entry()],
        other => {
            unattributable.push(format!("unknown experimental flag {other:?}"));
            Vec::new()
        }
    };
    let (mut fc, mut fx) = (Vec::new(), Vec::new());
    files(control, "", &mut fc)?;
    files(experimental, "", &mut fx)?;
    let mut all: Vec<&String> = fc.iter().chain(fx.iter()).collect();
    all.sort();
    all.dedup();
    let (mc, mx) = (manifest_keys(control)?, manifest_keys(experimental)?);
    for rel in all {
        let (in_c, in_x) = (fc.contains(rel), fx.contains(rel));
        let explained = match rel.as_str() {
            "manifest.kvx" => {
                let strip = |m: &[(String, Value)]| -> Vec<(String, Value)> {
                    m.iter()
                        .filter(|(k, _)| k != "experimental" && k != "experimental_flag")
                        .cloned()
                        .collect()
                };
                strip(&mc) == strip(&mx)
            }
            cwr_kernel::drift_injector::SCHEDULE_PATH
                if flag == cwr_kernel::drift_injector::FLAG =>
            {
                !in_c && in_x
            }
            _ if in_c != in_x => false,
            _ if rel.ends_with('/') => true,
            _ => {
                let (bc, bx) = (read(control, rel), read(experimental, rel));
                bc == bx
                    || (flag == one_primitive::FLAG
                        && one_primitive::explains(rel, &bc, &bx, control, experimental))
            }
        };
        if !explained {
            unattributable.push(match (in_c, in_x) {
                (true, false) => format!("file {rel} only in the control Build"),
                (false, true) => format!("file {rel} only in the experimental Build"),
                _ => format!("file {rel} differs"),
            });
        }
    }
    let dv = diff.to_value();
    Ok(json!({
        "control": hc,
        "experimental": hx,
        "flag": flag,
        "entries": dv["entries"],
        "doc_entries": dv["doc_entries"],
        "experimental_entries": experimental_entries,
        "unattributable": unattributable,
        "attributable": unattributable.is_empty(),
    }))
}

/// `cwr diff` output for references `a` and `b` (compiler.md section 10, attribution rule): the
/// ContractDiff `d` exactly as for any pair, plus, when `a` is a control Build and `b` an
/// experimental Build, an `experimental` key holding their [`attribution`]. Output for every other
/// pair is `d.to_json()` byte for byte.
pub fn diff_output(
    d: &crate::diff::ContractDiff,
    a: &str,
    b: &str,
    roots: &[PathBuf],
) -> Result<String, String> {
    use crate::diff::{resolve_reference, Reference};
    let (Ok(Reference::Build(ca)), Ok(Reference::Build(xb))) =
        (resolve_reference(a, roots), resolve_reference(b, roots))
    else {
        return Ok(d.to_json());
    };
    if flag_of(&ca)?.is_some() || flag_of(&xb)?.is_none() {
        return Ok(d.to_json());
    }
    let mut v = d.to_value();
    v["experimental"] = attribution(&ca, &xb)?;
    Ok(crate::diff::canonical_pretty(&v))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn flag_names_are_the_command_line_flags_without_dashes() {
        for (arg, name) in [
            ("--x-drift", "x-drift"),
            ("--x-silent-change", "x-silent-change"),
            ("--x-version-only", "x-version-only"),
            ("--x-one-primitive", "x-one-primitive"),
        ] {
            let f = Flag::from_arg(arg, Some("d.kvx")).expect(arg);
            assert_eq!(f.name(), name);
            assert!(FLAG_NAMES.contains(&name));
        }
        assert_eq!(Flag::from_arg("--x-drift", None), None);
        assert_eq!(Flag::from_arg("--x-other", None), None);
    }

    #[test]
    fn the_experimental_root_is_beside_the_build_root() {
        let o = |out: Option<&str>| Options {
            out: out.map(PathBuf::from),
            ..Options::default()
        };
        assert_eq!(xbuild_root(&o(None)), PathBuf::from("xbuild"));
        assert_eq!(
            xbuild_root(&o(Some("/w/build"))),
            PathBuf::from("/w/xbuild")
        );
    }
}
