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
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};

use cwr_contract::kvx;
use serde_json::{json, Value};

use crate::diff::{compute, Surface};
use crate::manifest::{Generated, Manifest};
use crate::package::{self, Built, COPIED_DIRS};
use crate::{
    run_stage, sort_diagnostics, Context, Diagnostic, Location, Options, Report, Stage,
    StageReport, StageRun, StageStatus,
};

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
    let xroot = xbuild_root(&options);
    let staged = match flag {
        Flag::Drift(file) => match drift::Staged::new(root, file, &xroot) {
            Ok(s) => Some(s),
            Err(d) => return stopped(root, Stage::Parse, vec![d], Vec::new()),
        },
        _ => None,
    };
    let source = staged.as_ref().map(|s| s.dir.as_path()).unwrap_or(root);
    let mut cx = Context::new(source, &options);
    let mut passed = Vec::new();
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
            StageRun::Passed => passed.push(stage),
            StageRun::Failed(mut diags) => {
                if let Some(s) = &staged {
                    s.relocate(&mut diags);
                }
                return stopped(root, stage, diags, passed);
            }
            StageRun::Pending => return pending(root, stage, passed),
        }
    }
    let transformed = match transform(&cx, flag) {
        Ok(t) => t,
        Err(m) => {
            let d = diagnostic(Stage::Generate, "generator", "world.kvx", m);
            return stopped(root, Stage::Generate, vec![d], passed);
        }
    };
    let extra = staged
        .as_ref()
        .map(|s| vec![(cwr_kernel::drift_injector::SCHEDULE_PATH, s.schedule.clone())])
        .unwrap_or_default();
    match package_experimental(&cx, &transformed, flag, &extra, &xroot) {
        Ok(built) => {
            passed.push(Stage::Package);
            let summary = format!(
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
            report(root, passed, None, 0, summary, Some(built))
        }
        Err(m) => {
            let d = diagnostic(Stage::Package, "package", "world.kvx", m);
            stopped(root, Stage::Package, vec![d], passed)
        }
    }
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

fn report(
    root: &Path,
    passed: Vec<Stage>,
    stop: Option<(Stage, StageStatus, Vec<Diagnostic>)>,
    exit_code: i32,
    summary: String,
    build: Option<Built>,
) -> Report {
    let mut stages: Vec<StageReport> = Stage::ALL
        .iter()
        .map(|&stage| StageReport {
            stage,
            status: if passed.contains(&stage) {
                StageStatus::Passed
            } else {
                StageStatus::NotRun
            },
            diagnostics: Vec::new(),
        })
        .collect();
    let stopped_at = stop.as_ref().map(|(s, _, _)| *s);
    if let Some((stage, status, diags)) = stop {
        stages[stage as usize].status = status;
        stages[stage as usize].diagnostics = diags;
    }
    Report {
        source: root.display().to_string(),
        through: Stage::Package,
        stages,
        stopped_at,
        exit_code,
        summary,
        build,
    }
}

fn stopped(root: &Path, stage: Stage, mut diags: Vec<Diagnostic>, passed: Vec<Stage>) -> Report {
    sort_diagnostics(&mut diags);
    for d in &mut diags {
        d.stage = stage;
    }
    let n = diags.len();
    let summary = format!(
        "failed at stage {stage}: {n} diagnostic{}; nothing written",
        if n == 1 { "" } else { "s" }
    );
    report(
        root,
        passed,
        Some((stage, StageStatus::Failed, diags)),
        stage.exit_code(),
        summary,
        None,
    )
}

fn pending(root: &Path, stage: Stage, passed: Vec<Stage>) -> Report {
    let summary = format!(
        "stopped at stage {stage}: not implemented in this compiler revision; no Build written"
    );
    report(
        root,
        passed,
        Some((stage, StageStatus::Pending, Vec::new())),
        1,
        summary,
        None,
    )
}

fn write(path: &Path, bytes: &[u8]) -> Result<(), String> {
    if let Some(p) = path.parent() {
        fs::create_dir_all(p).map_err(|e| format!("create {}: {e}", p.display()))?;
    }
    fs::write(path, bytes).map_err(|e| format!("write {}: {e}", path.display()))
}

/// Copies a source directory into a Build the way the package stage does: regular files and
/// directories in name order, symbolic links and `__pycache__` skipped, a missing source
/// directory becoming an empty one.
fn copy_tree(from: &Path, to: &Path) -> Result<(), String> {
    fs::create_dir_all(to).map_err(|e| format!("create {}: {e}", to.display()))?;
    if !from.is_dir() {
        return Ok(());
    }
    let mut entries: Vec<_> = fs::read_dir(from)
        .map_err(|e| format!("read {}: {e}", from.display()))?
        .filter_map(|e| e.ok())
        .collect();
    entries.sort_by_key(|e| e.file_name());
    for e in entries {
        let ft = e
            .file_type()
            .map_err(|err| format!("stat {}: {err}", e.path().display()))?;
        let name = e.file_name();
        if ft.is_symlink() || name == "__pycache__" {
            continue;
        }
        if ft.is_dir() {
            copy_tree(&e.path(), &to.join(&name))?;
        } else {
            let bytes =
                fs::read(e.path()).map_err(|err| format!("read {}: {err}", e.path().display()))?;
            write(&to.join(&name), &bytes)?;
        }
    }
    Ok(())
}

fn discard(dir: &Path) {
    fn unseal(d: &Path) {
        let _ = fs::set_permissions(d, fs::Permissions::from_mode(0o755));
        if let Ok(rd) = fs::read_dir(d) {
            for e in rd.flatten() {
                if e.file_type().map(|t| t.is_dir()).unwrap_or(false) {
                    unseal(&e.path());
                }
            }
        }
    }
    unseal(dir);
    let _ = fs::remove_dir_all(dir);
}

fn fill(
    cx: &Context,
    g: &Generated,
    flag: &Flag,
    extra: &[(&str, Vec<u8>)],
    tmp: &Path,
) -> Result<String, String> {
    let diff = cx.diff.as_ref().ok_or("diff did not run")?;
    let tests = cx.tests.as_ref().ok_or("contract tests did not run")?;
    write(&tmp.join("contracts.json"), g.contracts_json.as_bytes())?;
    write(&tmp.join("contract_diff.json"), diff.to_json().as_bytes())?;
    write(&tmp.join("manual.md"), g.manual.as_bytes())?;
    fs::create_dir_all(tmp.join("sdk")).map_err(|e| format!("create sdk/: {e}"))?;
    for (rel, text) in &g.sdk {
        write(&tmp.join("sdk").join(rel), text.as_bytes())?;
    }
    for d in COPIED_DIRS {
        copy_tree(&cx.root.join(d), &tmp.join(d))?;
    }
    for (rel, bytes) in extra {
        let path = tmp.join(rel);
        if path.exists() {
            return Err(format!(
                "the World Source already has {rel}, which an experimental Build reserves"
            ));
        }
        write(&path, bytes)?;
    }
    write(
        &tmp.join("tests.json"),
        package::tests_json(tests)?.as_bytes(),
    )?;
    let previous = diff.previous.clone().filter(|p| p.starts_with("sha256:"));
    let mut manifest = Manifest::from_contracts(&g.contracts, previous);
    manifest.experimental_flag = Some(flag.name().to_string());
    write(&tmp.join("manifest.kvx"), manifest.render(None).as_bytes())?;
    let hex = cwr_kernel::Build::compute_hash(tmp).map_err(|e| e.to_string())?;
    write(
        &tmp.join("manifest.kvx"),
        manifest.render(Some(&format!("sha256:{hex}"))).as_bytes(),
    )?;
    let check = cwr_kernel::Build::compute_hash(tmp).map_err(|e| e.to_string())?;
    if check != hex {
        return Err(format!(
            "compiler defect: hash changed after writing the manifest ({hex} then {check})"
        ));
    }
    Ok(hex)
}

/// Packages an experimental Build into `xroot/<hex>` with the guarantees of the package stage:
/// written into a temporary sibling, manifest last, sealed read-only, renamed into place, and an
/// existing verifying directory reported as already built and left untouched.
fn package_experimental(
    cx: &Context,
    g: &Generated,
    flag: &Flag,
    extra: &[(&str, Vec<u8>)],
    xroot: &Path,
) -> Result<Built, String> {
    fs::create_dir_all(xroot)
        .map_err(|e| format!("cannot create the experimental root {}: {e}", xroot.display()))?;
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_nanos())
        .unwrap_or(0);
    let tmp = xroot.join(format!(".tmp-{}-{nanos}", std::process::id()));
    let hex = match fill(cx, g, flag, extra, &tmp) {
        Ok(h) => h,
        Err(m) => {
            discard(&tmp);
            return Err(m);
        }
    };
    let target = xroot.join(&hex);
    let built = |already| Built {
        hash: format!("sha256:{hex}"),
        dir: target.display().to_string(),
        already_built: already,
    };
    let existing = |t: &Path| t.exists().then(|| package::verify(t).map(|_| ()));
    let untouched = |m: String| {
        format!(
            "the output directory {} exists and does not verify: {m}; left untouched",
            target.display()
        )
    };
    if let Some(r) = existing(&target) {
        discard(&tmp);
        return r.map(|_| built(true)).map_err(untouched);
    }
    if let Err(m) = package::seal(&tmp) {
        discard(&tmp);
        return Err(m);
    }
    if let Err(e) = fs::rename(&tmp, &target) {
        discard(&tmp);
        return match existing(&target) {
            Some(r) => r.map(|_| built(true)).map_err(untouched),
            None => Err(format!(
                "cannot rename the packaged Build to {}: {e}",
                target.display()
            )),
        };
    }
    let _ = fs::File::open(xroot).and_then(|d| d.sync_all());
    package::verify(&target)
        .map_err(|m| format!("compiler defect: the packaged Build does not verify: {m}"))?;
    if !package::sealed(&target) {
        return Err(format!(
            "Build {} has a writable file after sealing",
            target.display()
        ));
    }
    Ok(built(false))
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
        assert_eq!(xbuild_root(&o(Some("/w/build"))), PathBuf::from("/w/xbuild"));
    }
}
