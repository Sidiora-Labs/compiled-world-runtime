// Copyright Sidiora Labs Inc. CWR® 2026-2027
// SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0

//! Package stage and Build verification (compiler.md sections 7 and 11; clauses 1.4 and 4.5;
//! task 1.3).
//!
//! Packaging writes every file into a temporary sibling directory of the Build root, computes the
//! Build hash with the kernel's algorithm ([`cwr_kernel::Build::compute_hash`]: in-hash paths
//! sorted bytewise, each fed as path, NUL, decimal length, NUL, bytes; `manifest.kvx` without its
//! `hash` key), writes `manifest.kvx` last with the hash, re-verifies, makes every file `0444` and
//! every directory `0555`, and renames the directory to `<out>/<hex>`. No partial Build directory
//! is ever visible under its hash name.
//!
//! When `<out>/<hex>` already exists (a rebuild of the same source, or a racing build), its content
//! is verified: a verifying directory is reported as already built and left untouched (exit 0); a
//! directory that does not verify is a package failure (exit 7) and is also left untouched.
//!
//! `tests.json` is inside the hash (clause 4.5, task 1.3 do_3). It is written without the host
//! durations of the contract-test runs, so it is a deterministic function of the World Source.
//!
//! A Build directory holds exactly [`LAYOUT`]: [`verify`] refuses a missing or an additional
//! top-level entry, and the hash itself (kernel) refuses unexpected entries, symbolic links and
//! special files and counts empty directories, so no later modification goes unnoticed.

use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};

use cwr_contract::kvx;

use crate::manifest::Manifest;
use crate::{Context, Diagnostic, Location, Stage, StageRun};

/// The source directories byte-copied into the Build.
pub const COPIED_DIRS: [&str; 4] = ["lib", "adapters", "fixtures", "migrations"];

/// Every top-level entry of a packaged Build (compiler.md section 7), all inside the hash.
pub const LAYOUT: [&str; 10] = [
    "manifest.kvx",
    "contracts.json",
    "contract_diff.json",
    "manual.md",
    "tests.json",
    "sdk",
    "lib",
    "adapters",
    "fixtures",
    "migrations",
];

/// `tests.json` bytes as packaged: the contract-test report without host durations.
pub fn tests_json(report: &crate::contract_tests::TestReport) -> Result<String, String> {
    let mut v: serde_json::Value =
        serde_json::from_str(&report.to_json()?).map_err(|e| format!("tests.json: {e}"))?;
    if let Some(runs) = v.get_mut("runs").and_then(|r| r.as_array_mut()) {
        for r in runs {
            if let Some(o) = r.as_object_mut() {
                o.remove("duration_us");
            }
        }
    }
    Ok(crate::diff::canonical_pretty(&v))
}

/// The result of a successful package stage.
#[derive(Debug, Clone, serde::Serialize)]
pub struct Built {
    /// `sha256:<hex>`.
    pub hash: String,
    /// The Build directory, as a path string.
    pub dir: String,
    /// True when an identical, verifying Build already existed and nothing was written.
    pub already_built: bool,
}

fn is_hex64(s: &str) -> bool {
    s.len() == 64
        && s.bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}

/// The `hash` recorded in a Build's `manifest.kvx`, without the `sha256:` prefix: the manifest's
/// last line, which must read exactly `hash = "sha256:<64 lowercase hex>"` (compiler.md section 7,
/// "written last with the key present"). Any other spelling, position or an additional `hash` key
/// elsewhere is not accepted as the record; an additional key also enters the hash.
pub fn recorded_hash(dir: &Path) -> Result<String, String> {
    let bytes = fs::read(dir.join("manifest.kvx"))
        .map_err(|e| format!("Build {}: cannot read manifest.kvx: {e}", dir.display()))?;
    let text = String::from_utf8(bytes)
        .map_err(|_| format!("Build {}: manifest.kvx is not UTF-8", dir.display()))?;
    kvx::parse("manifest.kvx", &text).map_err(|e| {
        format!(
            "Build {}: manifest.kvx does not parse: {}",
            dir.display(),
            e.iter()
                .map(|x| x.to_string())
                .collect::<Vec<_>>()
                .join("; ")
        )
    })?;
    cwr_kernel::state::manifest_recorded_hash(text.as_bytes())
        .ok_or_else(|| format!("Build {}: manifest.kvx does not end with its recorded hash line `hash = \"sha256:<hex>\"`", dir.display()))
}

/// Verifies a Build directory: recomputes its hash and requires it to equal the manifest's
/// recorded hash and, when the directory is named by a hash, its name. Returns `sha256:<hex>`.
pub fn verify(dir: &Path) -> Result<String, String> {
    if !dir.join("manifest.kvx").is_file() {
        return Err(format!(
            "{} is not a Build directory (no manifest.kvx)",
            dir.display()
        ));
    }
    let mut entries: Vec<String> = fs::read_dir(dir)
        .map_err(|e| format!("Build {}: {e}", dir.display()))?
        .filter_map(|e| e.ok())
        .map(|e| e.file_name().to_string_lossy().into_owned())
        .collect();
    entries.sort();
    let mut want: Vec<String> = LAYOUT.iter().map(|s| s.to_string()).collect();
    want.sort();
    if entries != want {
        let extra: Vec<&String> = entries.iter().filter(|e| !want.contains(e)).collect();
        let missing: Vec<&String> = want.iter().filter(|w| !entries.contains(w)).collect();
        return Err(format!(
            "Build {} does not have the packaged layout: unexpected {extra:?}, missing {missing:?}",
            dir.display()
        ));
    }
    let computed = cwr_kernel::Build::compute_hash(dir)
        .map_err(|e| format!("Build {}: {e}", dir.display()))?;
    let recorded = recorded_hash(dir)?;
    if recorded != computed {
        return Err(format!("Build {} hashes to sha256:{computed} but its manifest records sha256:{recorded}; refusing an unverifiable Build", dir.display()));
    }
    let base = dir.file_name().and_then(|n| n.to_str()).unwrap_or("");
    if is_hex64(base) && base != computed {
        return Err(format!(
            "Build directory {} hashes to sha256:{computed}, not to its name",
            dir.display()
        ));
    }
    Ok(format!("sha256:{computed}"))
}

/// Resolves a Build reference (a directory, or `sha256:<hex>` / `<hex>` under one of `roots`).
pub fn resolve(reference: &str, roots: &[PathBuf]) -> Result<PathBuf, String> {
    let p = Path::new(reference);
    if p.is_dir() {
        return Ok(p.to_path_buf());
    }
    let hex = reference.trim_start_matches("sha256:");
    if is_hex64(hex) {
        for r in roots {
            let c = r.join(hex);
            if c.is_dir() {
                return Ok(c);
            }
        }
        return Err(format!(
            "no Build directory for sha256:{hex} under {}",
            roots
                .iter()
                .map(|r| r.display().to_string())
                .collect::<Vec<_>>()
                .join(", ")
        ));
    }
    Err(format!(
        "{reference:?} is neither a Build directory nor a Build hash"
    ))
}

fn write(path: &Path, bytes: &[u8]) -> Result<(), String> {
    if let Some(p) = path.parent() {
        fs::create_dir_all(p).map_err(|e| format!("create {}: {e}", p.display()))?;
    }
    fs::write(path, bytes).map_err(|e| format!("write {}: {e}", path.display()))
}

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

/// Sets every file `0444` and every directory `0555`, depth first, the root last.
pub fn seal(dir: &Path) -> Result<(), String> {
    let mut entries: Vec<_> = fs::read_dir(dir)
        .map_err(|e| format!("read {}: {e}", dir.display()))?
        .filter_map(|e| e.ok())
        .collect();
    entries.sort_by_key(|e| e.file_name());
    for e in entries {
        let p = e.path();
        if e.file_type().map(|t| t.is_dir()).unwrap_or(false) {
            seal(&p)?;
        } else {
            fs::set_permissions(&p, fs::Permissions::from_mode(0o444))
                .map_err(|err| format!("seal {}: {err}", p.display()))?;
        }
    }
    fs::set_permissions(dir, fs::Permissions::from_mode(0o555))
        .map_err(|e| format!("seal {}: {e}", dir.display()))
}

/// Removes a (possibly sealed) temporary directory.
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

/// Every file and directory of a Build is without write permission bits.
pub fn sealed(dir: &Path) -> bool {
    let Ok(md) = fs::symlink_metadata(dir) else {
        return false;
    };
    if md.permissions().mode() & 0o222 != 0 {
        return false;
    }
    if md.is_dir() {
        if let Ok(rd) = fs::read_dir(dir) {
            return rd.flatten().all(|e| sealed(&e.path()));
        }
        return false;
    }
    true
}

fn fail(m: String) -> StageRun {
    StageRun::Failed(vec![Diagnostic::new(
        Stage::Package,
        None,
        "package",
        Location {
            file: "world.kvx".into(),
            line: 1,
            col: 1,
            section: None,
            key: None,
        },
        m,
    )])
}

fn package(cx: &Context, tmp: &Path) -> Result<String, String> {
    let g = cx.generated.as_ref().ok_or("generate did not run")?;
    let diff = cx.diff.as_ref().ok_or("diff did not run")?;
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
    let tests = cx
        .tests
        .as_ref()
        .ok_or("contract tests did not run; a Build carries tests.json")?;
    write(&tmp.join("tests.json"), tests_json(tests)?.as_bytes())?;
    let previous = diff.previous.clone().filter(|p| p.starts_with("sha256:"));
    let manifest = Manifest::from_contracts(&g.contracts, previous);
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

/// The package stage.
pub fn run_stage(cx: &mut Context) -> StageRun {
    let out = cx
        .options
        .out
        .clone()
        .unwrap_or_else(|| PathBuf::from("build"));
    if let Err(e) = fs::create_dir_all(&out) {
        return fail(format!(
            "cannot create the Build root {}: {e}",
            out.display()
        ));
    }
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_nanos())
        .unwrap_or(0);
    // The Build is assembled one level below a private staging directory, so that no directory
    // directly under the Build root ever holds a manifest.kvx before it is the finished, sealed
    // Build: a racing compilation of the same world scans the Build root for its previous Build
    // (diff stage) and must not pick up a half-written one.
    let holder = out.join(format!(".tmp-{}-{nanos}", std::process::id()));
    let tmp = holder.join("build");
    if let Err(e) = fs::create_dir_all(&tmp) {
        discard(&holder);
        return fail(format!("cannot create {}: {e}", tmp.display()));
    }
    let hex = match package(cx, &tmp) {
        Ok(h) => h,
        Err(m) => {
            discard(&holder);
            return fail(m);
        }
    };
    let finalize = |cx: &mut Context, already: bool| {
        cx.built = Some(Built {
            hash: format!("sha256:{hex}"),
            dir: out.join(&hex).display().to_string(),
            already_built: already,
        });
        StageRun::Passed
    };
    let target = out.join(&hex);
    let existing = |target: &Path| -> Option<Result<(), String>> {
        if target.exists() {
            Some(verify(target).map(|_| ()))
        } else {
            None
        }
    };
    if let Some(r) = existing(&target) {
        discard(&holder);
        return match r {
            Ok(()) => finalize(cx, true),
            Err(m) => fail(format!(
                "the output directory {} exists and does not verify: {m}; left untouched",
                target.display()
            )),
        };
    }
    if let Err(m) = seal(&tmp) {
        discard(&holder);
        return fail(m);
    }
    if let Err(e) = fs::rename(&tmp, &target) {
        discard(&holder);
        // A racing build of the same source may have renamed first.
        return match existing(&target) {
            Some(Ok(())) => finalize(cx, true),
            Some(Err(m)) => fail(format!(
                "the output directory {} exists and does not verify: {m}; left untouched",
                target.display()
            )),
            None => fail(format!(
                "cannot rename the packaged Build to {}: {e}",
                target.display()
            )),
        };
    }
    discard(&holder);
    let _ = fs::File::open(&out).and_then(|d| d.sync_all());
    match verify(&target) {
        Ok(_) if sealed(&target) => finalize(cx, false),
        Ok(_) => fail(format!(
            "Build {} has a writable file after sealing",
            target.display()
        )),
        Err(m) => fail(format!(
            "compiler defect: the packaged Build does not verify: {m}"
        )),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::contract_tests::{RunReport, StreamItem, TestReport};

    #[test]
    fn packaged_tests_json_has_no_host_durations() {
        let item = StreamItem {
            target: "fs.read".into(),
            case: "ok".into(),
            status: "ok".into(),
            shape: true,
            side_effect: "read_only".into(),
        };
        let mk = |d| TestReport {
            primitives: 1,
            adapters: 0,
            cases: vec![],
            migration_cases: 0,
            runs: vec![RunReport {
                stream: vec![item.clone()],
                duration_us: d,
            }],
            deterministic: true,
            passed: true,
        };
        let a = tests_json(&mk(1)).unwrap();
        let b = tests_json(&mk(987_654)).unwrap();
        assert_eq!(a, b);
        assert!(!a.contains("duration"));
        assert!(a.contains("\"format\": \"cwr.tests\""));
    }

    #[test]
    fn layout_lists_every_in_hash_entry() {
        let mut all: Vec<&str> = cwr_kernel::state::IN_HASH_FILES
            .iter()
            .chain(cwr_kernel::state::IN_HASH_DIRS.iter())
            .copied()
            .collect();
        all.sort();
        let mut l = LAYOUT.to_vec();
        l.sort();
        assert_eq!(all, l);
    }

    #[test]
    fn verify_refuses_an_extra_or_missing_top_level_entry() {
        let d = std::env::temp_dir().join(format!("cwr-pkg-layout-{}", std::process::id()));
        let _ = fs::remove_dir_all(&d);
        for f in [
            "manifest.kvx",
            "contracts.json",
            "contract_diff.json",
            "manual.md",
            "tests.json",
        ] {
            write(&d.join(f), b"x\n").unwrap();
        }
        for s in ["sdk", "lib", "adapters", "fixtures", "migrations"] {
            fs::create_dir_all(d.join(s)).unwrap();
        }
        write(&d.join("extra.txt"), b"x").unwrap();
        assert!(verify(&d)
            .unwrap_err()
            .contains("unexpected [\"extra.txt\"]"));
        fs::remove_file(d.join("extra.txt")).unwrap();
        fs::remove_dir(d.join("migrations")).unwrap();
        assert!(verify(&d).unwrap_err().contains("missing [\"migrations\"]"));
        let _ = fs::remove_dir_all(&d);
    }

    #[test]
    fn only_the_canonical_last_hash_line_is_the_record() {
        let h = "ab".repeat(32);
        let base = "[meta]\nschema_version = 1\n\n[build]\nname = \"w\"\n";
        let rec = |t: &str| cwr_kernel::state::manifest_recorded_hash(t.as_bytes());
        assert_eq!(
            rec(&format!("{base}hash = \"sha256:{h}\"\n")),
            Some(h.clone())
        );
        for bad in [
            format!("{base}hash  =  \"sha256:{h}\"\n"),
            format!("{base}hash = \"sha256:{h}\" # c\n"),
            format!("{base}hash = \"sha256:{h}\"\r\n"),
            format!("{base}hash = \"sha256:{h}\""),
            format!("{base}hash = \"sha256:{}\"\n", h.to_uppercase()),
            format!("[meta]\nhash = \"sha256:{h}\"\n{base}"),
            format!("{base}hash = \"sha256:{h}\"\n\n"),
        ] {
            assert_eq!(rec(&bad), None, "{bad:?}");
        }
    }
}
