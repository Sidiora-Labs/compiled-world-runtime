// Copyright Sidiora Labs Inc. CWR® 2026-2027
// SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0

//! `cwr release pin` and `cwr release check` (task 6.1; spec clause 14.3).
//!
//! ```text
//! cwr release pin   [--root <dir>] [--file <release.kvx>] [--write] [--json]
//! cwr release check [--root <dir>] [--file <release.kvx>] [--claim] [--json]
//! ```
//!
//! Both commands recompute every release input from the tree under `--root` (default: the
//! working directory): the contract, compiler, kernel, CLI and SDK revisions, the `Cargo.lock`
//! digest, the refworld control, experimental and upgrade-pair Build hashes (by running
//! `cwr build` from this executable into private temporary directories), the cassette set and
//! task suite digests (each also compared with the value frozen in `benchmark/suite.kvx`), the
//! packaged CLI and SDK checksums, the campaign result digests, the specification digest and
//! the pre-registration digest. `pin` prints the computed values and, with `--write`, rewrites
//! the value line of every pin whose computation succeeded; a pin that failed keeps its recorded
//! line and is reported. Comments and layout are kept, and the new file replaces the old one
//! atomically (sibling temporary file, fsync, rename), so an interrupted write never truncates it.
//! `check` compares the recorded values with the recomputed ones.
//!
//! A registration source the release file names (`campaigns`, `registered_in`, `schedules`) that
//! is missing or unreadable is a diagnostic on the pin it guards, never a skipped comparison.
//!
//! Every input that is missing, unrecorded or different from its recomputation produces one
//! named diagnostic `CWR-Rnnn` with the release-file key and a reason; the codes and messages
//! are listed in [`PINS`] and repeated in the `[diagnostic.*]` sections of the release file and
//! in `RELEASE.md`. `--claim` adds `CWR-R015`, closure and owner acceptance (task 6.2), which a
//! release claim needs on top of complete pins: the closure report (the JSON of
//! `tests/cwr/closure.py --json`) must say `ACCEPTED` with no blocking item and recorded owner
//! acceptance, and the owner-acceptance file must record `owner_acceptance = true` with an owner.
//!
//! A revision is the `sha256:` tree digest of the component's directory in the layout of
//! `benchmark/predicates/common.py::tree_digest`: relative POSIX path, NUL, decimal length, NUL,
//! bytes, per file, sorted by path bytes. A campaign result digest uses the same layout. Both
//! cover the published files only (see [`published_digest`]): in a git checkout the files the release
//! bundler selects, in an unpacked bundle every regular file and every symbolic link to one,
//! `__pycache__` skipped and symbolic links to directories not entered. A content digest is the
//! revision, so the live tree and its bundle agree on it.
//!
//! Exit codes: 0 every pin recorded and verified (`check`) or computed (`pin`); 2 at least one
//! diagnostic; 1 usage error or an unreadable release file.

use std::collections::BTreeMap;
use std::fs;
use std::io::Read;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Mutex;

use cwr_contract::kvx;

use crate::cli;
use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};

/// One-line help text for `cwr release`, shown by `cwr help`.
pub const SUMMARY: &str =
    "pin or verify release inputs: cwr release (pin [--write] | check [--claim]) [--root <dir>] [--file <path>] [--json]";

const USAGE: &str = "usage: cwr release pin [--root <dir>] [--file <release.kvx>] [--write] [--json]\n       cwr release check [--root <dir>] [--file <release.kvx>] [--claim] [--json]";

/// Default location of the release file, relative to `--root`.
pub const RELEASE_FILE: &str = "benchmark/release.kvx";

/// One named release diagnostic: its code, the release-file key it guards and its message.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct PinSpec {
    /// `CWR-Rnnn`.
    pub code: &'static str,
    /// Section of the release file holding the pinned value.
    pub section: &'static str,
    /// Key within `section`.
    pub key: &'static str,
    /// Human message printed with the code.
    pub message: &'static str,
}

/// Every release diagnostic, in the order `check` reports them. `CWR-R015` is evaluated only
/// with `--claim`.
pub const PINS: [PinSpec; 21] = [
    PinSpec {
        code: "CWR-R001",
        section: "pin.compiler",
        key: "revision",
        message: "compiler revision is not pinned or differs from the tree",
    },
    PinSpec {
        code: "CWR-R002",
        section: "pin.kernel",
        key: "revision",
        message: "kernel revision is not pinned or differs from the tree",
    },
    PinSpec {
        code: "CWR-R003",
        section: "pin.sdk",
        key: "revision",
        message: "SDK revision is not pinned or differs from the tree",
    },
    PinSpec {
        code: "CWR-R004",
        section: "pin.cli",
        key: "revision",
        message: "CLI revision is not pinned or differs from the tree",
    },
    PinSpec {
        code: "CWR-R005",
        section: "build.refworld_control",
        key: "hash",
        message: "refworld control Build hash is missing or fails verification",
    },
    PinSpec {
        code: "CWR-R006",
        section: "build.x_drift",
        key: "hashes",
        message: "a drift Build hash is missing or fails verification",
    },
    PinSpec {
        code: "CWR-R007",
        section: "build.x_one_primitive",
        key: "hash",
        message: "one-primitive Build hash is missing or fails verification",
    },
    PinSpec {
        code: "CWR-R008",
        section: "build.x_version_only",
        key: "hashes",
        message: "a version-only Build hash is missing or fails verification",
    },
    PinSpec {
        code: "CWR-R009",
        section: "build.x_silent_change",
        key: "hashes",
        message: "a silent-change Build hash is missing or fails verification",
    },
    PinSpec {
        code: "CWR-R010",
        section: "build.upgrade_pairs",
        key: "hashes",
        message: "an upgrade pair Build hash is missing or fails verification",
    },
    PinSpec {
        code: "CWR-R011",
        section: "cassettes",
        key: "digest",
        message: "cassette set digest is missing or differs from benchmark/suite.kvx",
    },
    PinSpec {
        code: "CWR-R012",
        section: "suite",
        key: "digest",
        message: "task suite digest is missing or differs from benchmark/suite.kvx",
    },
    PinSpec {
        code: "CWR-R013",
        section: "artifact.cli",
        key: "sha256",
        message: "packaged cwr CLI is missing or its checksum does not match",
    },
    PinSpec {
        code: "CWR-R014",
        section: "artifact.sdk",
        key: "sha256",
        message: "packaged Python SDK is missing or its checksum does not match",
    },
    PinSpec {
        code: "CWR-R015",
        section: "evidence",
        key: "closure",
        message: "closure or owner acceptance is missing or names blocking items; no release claim may be made",
    },
    PinSpec {
        code: "CWR-R016",
        section: "pin.contract",
        key: "revision",
        message: "contract crate revision is not pinned or differs from the tree",
    },
    PinSpec {
        code: "CWR-R017",
        section: "dependencies",
        key: "cargo_lock",
        message: "Cargo.lock digest is not pinned or differs from the tree",
    },
    PinSpec {
        code: "CWR-R018",
        section: "results",
        key: "digests",
        message: "a campaign result digest is missing or differs from benchmark/results",
    },
    PinSpec {
        code: "CWR-R019",
        section: "meta",
        key: "release",
        message: "release version is missing or disagrees with the cwr-cli and SDK versions",
    },
    PinSpec {
        code: "CWR-R020",
        section: "meta",
        key: "spec_sha256",
        message: "specification digest is not pinned or differs from spec/cwr/spec.kvx",
    },
    PinSpec {
        code: "CWR-R021",
        section: "evidence",
        key: "preregistration_sha256",
        message: "pre-registration digest is not pinned or differs from the registered file",
    },
];

/// The release file, parsed: section name to key to value.
type Sections = BTreeMap<String, BTreeMap<String, Value>>;

/// The outcome of recomputing one pin.
#[derive(Debug, Clone, PartialEq)]
pub struct PinResult {
    /// The pin this result is for.
    pub spec: PinSpec,
    /// Value recomputed from the tree, or the reason it cannot be computed.
    pub computed: Result<Value, String>,
    /// Value recorded in the release file (`Null` when absent).
    pub recorded: Value,
}

impl PinResult {
    /// `None` when the recorded value equals the recomputed one; otherwise the reason.
    pub fn problem(&self) -> Option<String> {
        match &self.computed {
            Err(reason) => Some(reason.clone()),
            Ok(value) if self.recorded.is_null() => {
                Some(format!("not recorded; computed {}", compact(value)))
            }
            Ok(value) if *value != self.recorded => Some(format!(
                "recorded {} but computed {}",
                compact(&self.recorded),
                compact(value)
            )),
            Ok(_) => None,
        }
    }
}

fn compact(v: &Value) -> String {
    serde_json::to_string(v).unwrap_or_else(|_| "?".into())
}

struct Args {
    command: String,
    root: PathBuf,
    file: Option<PathBuf>,
    write: bool,
    json: bool,
    claim: bool,
}

fn usage_error(message: &str) -> i32 {
    eprintln!("cwr release: {message}");
    eprintln!("{USAGE}");
    1
}

fn parse_args(args: &[String]) -> Result<Args, String> {
    let mut it = args.iter();
    let command = match it.next().map(String::as_str) {
        Some(c @ ("pin" | "check")) => c.to_string(),
        Some(other) => return Err(format!("unknown release command {other:?}")),
        None => return Err("missing release command".into()),
    };
    let mut parsed = Args {
        command,
        root: PathBuf::from("."),
        file: None,
        write: false,
        json: false,
        claim: false,
    };
    while let Some(a) = it.next() {
        match a.as_str() {
            "--root" => parsed.root = PathBuf::from(it.next().ok_or("--root needs a directory")?),
            "--file" => parsed.file = Some(PathBuf::from(it.next().ok_or("--file needs a path")?)),
            "--write" if parsed.command == "pin" => parsed.write = true,
            "--claim" if parsed.command == "check" => parsed.claim = true,
            "--json" => parsed.json = true,
            other => return Err(format!("unexpected argument {other:?}")),
        }
    }
    Ok(parsed)
}

/// Runs `cwr release`; `args` follow `release`. Returns 0, 1 or 2 as documented in the module.
pub fn run(args: &[String]) -> i32 {
    let a = match parse_args(args) {
        Ok(a) => a,
        Err(m) => return usage_error(&m),
    };
    // Builds run with a private working directory, so every source path must be absolute.
    let root = match fs::canonicalize(&a.root) {
        Ok(r) => r,
        Err(e) => {
            eprintln!("cwr release: --root {}: {e}", a.root.display());
            return 1;
        }
    };
    let file = a.file.clone().unwrap_or_else(|| root.join(RELEASE_FILE));
    let text = match fs::read_to_string(&file) {
        Ok(t) => t,
        Err(e) => {
            eprintln!("cwr release: cannot read {}: {e}", file.display());
            return 1;
        }
    };
    let sections = match load_sections(&file.display().to_string(), &text) {
        Ok(s) => s,
        Err(m) => {
            eprintln!("cwr release: {m}");
            return 1;
        }
    };
    let builder = match std::env::current_exe() {
        Ok(p) => p,
        Err(e) => {
            eprintln!("cwr release: cannot locate the cwr executable: {e}");
            return 1;
        }
    };
    let include_claim = a.command == "check" && a.claim;
    let results = compute_all(&root, &sections, &builder, include_claim);
    let check = a.command == "check";
    let failing: Vec<&PinResult> = results
        .iter()
        .filter(|r| {
            if check {
                r.problem().is_some()
            } else {
                r.computed.is_err()
            }
        })
        .collect();
    if a.write {
        match rewrite(&text, &write_updates(&results)) {
            Ok(new_text) => {
                if let Err(e) = write_atomic(&file, new_text.as_bytes()) {
                    eprintln!("cwr release: cannot write {}: {e}", file.display());
                    return 1;
                }
            }
            Err(m) => {
                eprintln!("cwr release: {m}");
                return 1;
            }
        }
    }
    if a.json {
        let pins: Vec<Value> = results
            .iter()
            .map(|r| {
                let problem = if check {
                    r.problem()
                } else {
                    r.computed.clone().err()
                };
                json!({
                    "diagnostic": r.spec.code,
                    "key": format!("{}.{}", r.spec.section, r.spec.key),
                    "message": r.spec.message,
                    "status": if problem.is_none() { "ok" } else { "fail" },
                    "recorded": r.recorded,
                    "computed": r.computed.as_ref().ok(),
                    "detail": problem,
                })
            })
            .collect();
        let written = cli::emit_line(&format!(
            "{:#}",
            json!({"command": a.command, "ok": failing.is_empty(), "pins": pins})
        ));
        if written != 0 {
            return written;
        }
    } else {
        let mut out = String::new();
        for r in &results {
            let key = format!("{}.{}", r.spec.section, r.spec.key);
            let problem = if check {
                r.problem()
            } else {
                r.computed.clone().err()
            };
            out.push_str(&match (problem, &r.computed) {
                (None, Ok(v)) => format!("ok   {} {key} = {}\n", r.spec.code, compact(v)),
                (None, Err(_)) => format!("ok   {} {key}\n", r.spec.code),
                (Some(p), _) => format!("{}: {}: {key}: {p}\n", r.spec.code, r.spec.message),
            });
        }
        if failing.is_empty() {
            out.push_str(&format!("release {}: every pin verified\n", a.command));
        } else {
            let codes: Vec<&str> = failing.iter().map(|r| r.spec.code).collect();
            out.push_str(&format!(
                "release {}: {} diagnostic(s): {}\n",
                a.command,
                codes.len(),
                codes.join(" ")
            ));
        }
        let written = cli::emit(&out);
        if written != 0 {
            return written;
        }
    }
    if failing.is_empty() {
        0
    } else {
        2
    }
}

/// Parses a KVX release file into section → key → JSON value.
pub fn load_sections(file: &str, text: &str) -> Result<Sections, String> {
    let doc = kvx::parse(file, text).map_err(|errors| {
        errors
            .iter()
            .map(ToString::to_string)
            .collect::<Vec<_>>()
            .join("; ")
    })?;
    let mut out = Sections::new();
    for s in &doc.sections {
        let entries = out.entry(s.name.clone()).or_default();
        for e in &s.entries {
            entries.insert(e.key.clone(), e.value.value.to_value());
        }
    }
    Ok(out)
}

fn get<'a>(sections: &'a Sections, section: &str, key: &str) -> Option<&'a Value> {
    sections.get(section).and_then(|s| s.get(key))
}

fn get_str(sections: &Sections, section: &str, key: &str, default: &str) -> String {
    get(sections, section, key)
        .and_then(Value::as_str)
        .unwrap_or(default)
        .to_string()
}

fn compute_all(
    root: &Path,
    sections: &Sections,
    builder: &Path,
    include_claim: bool,
) -> Vec<PinResult> {
    let builds = BuildPlan::from_sections(root, sections).run(builder);
    PINS.iter()
        .filter(|spec| include_claim || spec.code != "CWR-R015")
        .map(|spec| {
            let computed = compute_one(root, sections, spec, &builds);
            let recorded = get(sections, spec.section, spec.key)
                .cloned()
                .unwrap_or(Value::Null);
            let recorded = if spec.code == "CWR-R019" || spec.code == "CWR-R015" {
                // Version agreement and closure are verified, not recomputed: the computed
                // value is the recorded value when it holds.
                match &computed {
                    Ok(v) => v.clone(),
                    Err(_) => recorded,
                }
            } else {
                recorded
            };
            PinResult {
                spec: *spec,
                computed,
                recorded,
            }
        })
        .collect()
}

fn compute_one(
    root: &Path,
    sections: &Sections,
    spec: &PinSpec,
    builds: &BuildResults,
) -> Result<Value, String> {
    match spec.code {
        "CWR-R001" => revision(
            root,
            &get_str(sections, "pin.compiler", "path", "crates/cwr-compiler"),
        ),
        "CWR-R002" => revision(
            root,
            &get_str(sections, "pin.kernel", "path", "crates/cwr-kernel"),
        ),
        "CWR-R003" => revision(root, &get_str(sections, "pin.sdk", "path", "sdk/python")),
        "CWR-R004" => revision(
            root,
            &get_str(sections, "pin.cli", "path", "crates/cwr-cli"),
        ),
        "CWR-R016" => revision(
            root,
            &get_str(sections, "pin.contract", "path", "crates/cwr-contract"),
        ),
        "CWR-R017" => file_digest(&root.join("Cargo.lock")),
        "CWR-R020" => file_digest(&root.join("spec/cwr/spec.kvx")),
        "CWR-R021" => file_digest(&root.join(get_str(
            sections,
            "evidence",
            "preregistration",
            "benchmark/preregistration.kvx",
        ))),
        "CWR-R005" => builds.control.clone(),
        "CWR-R006" => builds.drift.clone(),
        "CWR-R007" => builds.one_primitive.clone(),
        "CWR-R008" => builds.version_only.clone(),
        "CWR-R009" => builds.silent_change.clone(),
        "CWR-R010" => builds.pairs.clone(),
        "CWR-R011" => cassette_pin(root, sections),
        "CWR-R012" => suite_pin(root, sections),
        "CWR-R013" => artifact_pin(root, sections, "artifact.cli"),
        "CWR-R014" => artifact_pin(root, sections, "artifact.sdk"),
        "CWR-R018" => results_pin(root, sections),
        "CWR-R019" => version_pin(root, sections),
        "CWR-R015" => claim_pin(root, sections),
        other => Err(format!("no computation for {other}")),
    }
}

// ---------------------------------------------------------------------------------------------
// Digests

fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

/// `sha256:<hex>` of a file's bytes.
pub fn file_digest(path: &Path) -> Result<Value, String> {
    let mut file = fs::File::open(path).map_err(|e| format!("{}: {e}", path.display()))?;
    let mut hasher = Sha256::new();
    let mut buf = vec![0u8; 1 << 16];
    loop {
        let n = file
            .read(&mut buf)
            .map_err(|e| format!("{}: {e}", path.display()))?;
        if n == 0 {
            break;
        }
        hasher.update(&buf[..n]);
    }
    Ok(Value::String(format!("sha256:{}", hex(&hasher.finalize()))))
}

/// The only directory name a tree digest skips, as in `common.py::tree_digest`.
const SKIPPED_DIR: &str = "__pycache__";

/// Every regular file under `dir`, and every symbolic link to one, as (relative POSIX path from
/// `base`, path), sorted by path bytes; `__pycache__` and symbolic links to directories are not
/// entered (the `Path.rglob` and `Path.is_file` semantics of `common.py::tree_digest`).
fn walk(dir: &Path, base: &Path) -> Result<Vec<(String, PathBuf)>, String> {
    let mut out = Vec::new();
    let mut stack = vec![dir.to_path_buf()];
    while let Some(d) = stack.pop() {
        let entries = fs::read_dir(&d).map_err(|e| format!("{}: {e}", d.display()))?;
        for entry in entries {
            let entry = entry.map_err(|e| format!("{}: {e}", d.display()))?;
            let path = entry.path();
            let name = entry.file_name().to_string_lossy().into_owned();
            let kind = entry
                .file_type()
                .map_err(|e| format!("{}: {e}", path.display()))?;
            let file = kind.is_file()
                || (kind.is_symlink() && fs::metadata(&path).is_ok_and(|m| m.is_file()));
            if kind.is_dir() {
                if name != SKIPPED_DIR {
                    stack.push(path);
                }
            } else if file {
                let rel = path
                    .strip_prefix(base)
                    .map_err(|_| format!("{} is outside {}", path.display(), base.display()))?;
                let rel: Vec<String> = rel
                    .components()
                    .map(|c| c.as_os_str().to_string_lossy().into_owned())
                    .collect();
                out.push((rel.join("/"), path));
            }
        }
    }
    out.sort_by(|a, b| a.0.as_bytes().cmp(b.0.as_bytes()));
    Ok(out)
}

/// Feeds `files` into `hasher` in the `common.py::tree_digest` layout.
fn feed_tree(hasher: &mut Sha256, files: &[(String, PathBuf)]) -> Result<(), String> {
    for (rel, path) in files {
        let data = fs::read(path).map_err(|e| format!("{}: {e}", path.display()))?;
        hasher.update(rel.as_bytes());
        hasher.update(format!("\0{}\0", data.len()).as_bytes());
        hasher.update(&data);
    }
    Ok(())
}

/// `sha256:` tree digest of every file under `dir`, paths relative to `dir`.
pub fn tree_digest(dir: &Path) -> Result<String, String> {
    if !dir.is_dir() {
        return Err(format!("{} is not a directory", dir.display()));
    }
    let files = walk(dir, dir)?;
    if files.is_empty() {
        return Err(format!("{} holds no file", dir.display()));
    }
    let mut hasher = Sha256::new();
    feed_tree(&mut hasher, &files)?;
    Ok(format!("sha256:{}", hex(&hasher.finalize())))
}

/// The published files under `rel` in the git checkout `root`, as (path relative to `rel`,
/// path): the selection `tools/release.py` puts in a release bundle, that is `git ls-files
/// --cached --others --exclude-standard` minus `__pycache__`, hidden path components and paths
/// that are not regular files. Ignored output (build directories, logs, unpublished campaign
/// files) is never selected.
fn git_published(root: &Path, rel: &str) -> Result<Vec<(String, PathBuf)>, String> {
    let output = Command::new("git")
        .arg("-C")
        .arg(root)
        .args([
            "ls-files",
            "-z",
            "--cached",
            "--others",
            "--exclude-standard",
            "--",
        ])
        .arg(rel)
        .output()
        .map_err(|e| format!("git ls-files {rel}: {e}"))?;
    if !output.status.success() {
        return Err(format!(
            "git ls-files {rel} failed: {}",
            String::from_utf8_lossy(&output.stderr).trim()
        ));
    }
    let prefix = format!("{}/", rel.trim_end_matches('/'));
    let mut out = Vec::new();
    for entry in output.stdout.split(|b| *b == 0).filter(|e| !e.is_empty()) {
        let listed = std::str::from_utf8(entry)
            .map_err(|_| format!("git ls-files {rel} listed a path that is not UTF-8"))?;
        let Some(inner) = listed.strip_prefix(&prefix) else {
            continue;
        };
        let hidden = listed
            .split('/')
            .any(|part| part == SKIPPED_DIR || part.starts_with('.'));
        let path = root.join(listed);
        if hidden || !fs::metadata(&path).is_ok_and(|m| m.is_file()) {
            continue;
        }
        out.push((inner.to_string(), path));
    }
    out.sort_by(|a, b| a.0.as_bytes().cmp(b.0.as_bytes()));
    out.dedup_by(|a, b| a.0 == b.0);
    Ok(out)
}

/// `sha256:` digest of the published files under `rel`, in the [`tree_digest`] layout. In a git
/// checkout (`root/.git` exists) the files are the [`git_published`] selection; in an unpacked
/// bundle, which holds only that selection, they are every file [`tree_digest`] reads. The live
/// tree and its bundle therefore compute the same digest.
fn published_digest(root: &Path, rel: &str) -> Result<String, String> {
    let dir = root.join(rel);
    if !dir.is_dir() {
        return Err(format!("{} is not a directory", dir.display()));
    }
    if !root.join(".git").exists() {
        return tree_digest(&dir);
    }
    let files = git_published(root, rel)?;
    if files.is_empty() {
        return Err(format!(
            "{} holds no published file",
            root.join(rel).display()
        ));
    }
    let mut hasher = Sha256::new();
    feed_tree(&mut hasher, &files)?;
    Ok(format!("sha256:{}", hex(&hasher.finalize())))
}

fn revision(root: &Path, rel: &str) -> Result<Value, String> {
    published_digest(root, rel).map(Value::String)
}

/// `^t[0-9]{3}-[a-z0-9-]+$`, the task directory pattern of `benchmark/predicates/common.py`.
fn is_task_dir(name: &str) -> bool {
    let Some(rest) = name.strip_prefix('t') else {
        return false;
    };
    let (Some(digits), Some(slug)) = (rest.get(..3), rest.get(3..)) else {
        return false;
    };
    let Some(slug) = slug.strip_prefix('-') else {
        return false;
    };
    digits.bytes().all(|c| c.is_ascii_digit())
        && !slug.is_empty()
        && slug
            .bytes()
            .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == b'-')
}

/// `common.py::suite_digest`: every file of every `tNNN-slug` directory, task directories in
/// name order, files by path bytes, paths relative to `tasks`. Returns (digest, task count).
pub fn suite_digest(tasks: &Path) -> Result<(String, usize), String> {
    let mut dirs: Vec<String> = fs::read_dir(tasks)
        .map_err(|e| format!("{}: {e}", tasks.display()))?
        .filter_map(Result::ok)
        .filter(|e| e.file_type().map(|t| t.is_dir()).unwrap_or(false))
        .map(|e| e.file_name().to_string_lossy().into_owned())
        .filter(|n| is_task_dir(n))
        .collect();
    dirs.sort();
    let mut hasher = Sha256::new();
    for d in &dirs {
        feed_tree(&mut hasher, &walk(&tasks.join(d), tasks)?)?;
    }
    Ok((format!("sha256:{}", hex(&hasher.finalize())), dirs.len()))
}

/// `benchmark/cassettes/FORMAT.md` section 5: sha256 over sorted "<relpath>\t<sha256>\n"
/// lines of every `*.jsonl` file under `dir`. Errors when the set holds no cassette.
pub fn cassette_set_digest(dir: &Path) -> Result<String, String> {
    if !dir.is_dir() {
        return Err(format!("cassette set {} does not exist", dir.display()));
    }
    let mut lines: Vec<String> = Vec::new();
    for (rel, path) in walk(dir, dir)? {
        if rel.ends_with(".jsonl") {
            let data = fs::read(&path).map_err(|e| format!("{}: {e}", path.display()))?;
            lines.push(format!("{rel}\t{}\n", hex(&Sha256::digest(&data))));
        }
    }
    if lines.is_empty() {
        return Err(format!("no cassette (*.jsonl) under {}", dir.display()));
    }
    lines.sort();
    Ok(format!(
        "sha256:{}",
        hex(&Sha256::digest(lines.concat().as_bytes()))
    ))
}

fn frozen_suite(root: &Path, sections: &Sections, section: &str) -> Result<Sections, String> {
    let path = root.join(get_str(
        sections,
        section,
        "frozen_in",
        "benchmark/suite.kvx",
    ));
    let text = fs::read_to_string(&path).map_err(|e| format!("{}: {e}", path.display()))?;
    load_sections(&path.display().to_string(), &text)
}

fn cassette_pin(root: &Path, sections: &Sections) -> Result<Value, String> {
    let dir = root.join(get_str(
        sections,
        "cassettes",
        "path",
        "benchmark/cassettes/suite",
    ));
    let digest = cassette_set_digest(&dir)?;
    let suite = frozen_suite(root, sections, "cassettes")?;
    match get(&suite, "suite", "cassette_digest") {
        Some(Value::String(frozen)) if *frozen == digest => Ok(Value::String(digest)),
        Some(Value::String(frozen)) => Err(format!(
            "cassette set digest {digest} differs from the frozen {frozen}"
        )),
        _ => Err(format!(
            "benchmark/suite.kvx freezes no cassette_digest (computed {digest})"
        )),
    }
}

fn suite_pin(root: &Path, sections: &Sections) -> Result<Value, String> {
    let dir = root.join(get_str(sections, "suite", "path", "benchmark/tasks"));
    let (digest, count) = suite_digest(&dir)?;
    let suite = frozen_suite(root, sections, "suite")?;
    let frozen = get(&suite, "suite", "suite_digest").and_then(Value::as_str);
    let frozen_count = get(&suite, "suite", "task_count").and_then(Value::as_u64);
    if frozen != Some(digest.as_str()) {
        return Err(format!(
            "task suite digest {digest} differs from the frozen {}",
            frozen.unwrap_or("null")
        ));
    }
    if frozen_count != Some(count as u64) {
        return Err(format!(
            "{count} task directories but benchmark/suite.kvx freezes task_count {}",
            frozen_count.map_or("null".to_string(), |c| c.to_string())
        ));
    }
    let recorded_tasks = get(sections, "suite", "tasks").and_then(Value::as_u64);
    if recorded_tasks != Some(count as u64) {
        return Err(format!(
            "suite.tasks records {} but the suite has {count} tasks",
            recorded_tasks.map_or("null".to_string(), |c| c.to_string())
        ));
    }
    Ok(Value::String(digest))
}

fn artifact_pin(root: &Path, sections: &Sections, section: &str) -> Result<Value, String> {
    let Some(rel) = get(sections, section, "path").and_then(Value::as_str) else {
        return Err(format!("{section}.path is not recorded"));
    };
    let path = root.join(rel);
    if !path.is_file() {
        return Err(format!(
            "{rel} does not exist; package it first (RELEASE.md)"
        ));
    }
    file_digest(&path)
}

fn results_pin(root: &Path, sections: &Sections) -> Result<Value, String> {
    let paths = get(sections, "results", "paths")
        .and_then(Value::as_object)
        .ok_or("results.paths is not recorded")?;
    if paths.is_empty() {
        return Err("results.paths is empty".into());
    }
    let mut digests = Map::new();
    let mut problems = Vec::new();
    for (label, rel) in paths {
        let Some(rel) = rel.as_str() else {
            problems.push(format!("{label}: path is not a string"));
            continue;
        };
        match published_digest(root, rel) {
            Ok(d) => {
                digests.insert(label.clone(), Value::String(d));
            }
            Err(e) => problems.push(format!("{label}: {e}")),
        }
    }
    if problems.is_empty() {
        Ok(Value::Object(digests))
    } else {
        Err(problems.join("; "))
    }
}

/// Reads `version = "..."` from the named TOML table with a line scan (no TOML dependency);
/// `Some(None)` means the table sets `version.workspace = true`.
fn toml_version(text: &str, table: &str) -> Option<Option<String>> {
    let mut in_table = false;
    for line in text.lines() {
        let line = line.trim();
        if line.starts_with('[') {
            in_table = line == format!("[{table}]");
            continue;
        }
        if !in_table {
            continue;
        }
        let Some((key, value)) = line.split_once('=') else {
            continue;
        };
        let (key, value) = (key.trim(), value.trim());
        if key == "version.workspace" && value == "true" {
            return Some(None);
        }
        if key == "version" {
            return Some(Some(value.trim_matches('"').to_string()));
        }
    }
    None
}

fn version_pin(root: &Path, sections: &Sections) -> Result<Value, String> {
    let read = |rel: &str| fs::read_to_string(root.join(rel)).map_err(|e| format!("{rel}: {e}"));
    let cli = match toml_version(&read("crates/cwr-cli/Cargo.toml")?, "package") {
        Some(Some(v)) => v,
        Some(None) => toml_version(&read("Cargo.toml")?, "workspace.package")
            .flatten()
            .ok_or("Cargo.toml [workspace.package] declares no version")?,
        None => return Err("crates/cwr-cli/Cargo.toml declares no version".into()),
    };
    let sdk_rel = format!(
        "{}/pyproject.toml",
        get_str(sections, "pin.sdk", "path", "sdk/python")
    );
    let sdk = toml_version(&read(&sdk_rel)?, "project")
        .flatten()
        .ok_or(format!("{sdk_rel} [project] declares no version"))?;
    let release = get(sections, "meta", "release").and_then(Value::as_str);
    match release {
        None => Err(format!("meta.release is null (cwr-cli {cli}, SDK {sdk})")),
        Some(r) if r == cli && r == sdk => Ok(Value::String(r.to_string())),
        Some(r) => Err(format!(
            "meta.release {r}, cwr-cli {cli}, SDK {sdk}: all three must be equal"
        )),
    }
}

/// Problems of a closure report (the JSON `tests/cwr/closure.py --json` prints): it must say
/// `ACCEPTED`, list no blocking item and record owner acceptance.
fn closure_problems(rel: &str, text: &str) -> Vec<String> {
    let report: Value = match serde_json::from_str(text) {
        Ok(v) => v,
        Err(e) => {
            return vec![format!(
                "evidence.closure {rel} is not a closure report: {e}"
            )]
        }
    };
    let mut problems = Vec::new();
    if report["closure"] != "cwr" {
        problems.push(format!(
            "evidence.closure {rel} is not a CWR closure report"
        ));
    }
    if report["verdict"] != "ACCEPTED" {
        problems.push(format!(
            "evidence.closure {rel} verdict is {}, not ACCEPTED",
            compact(&report["verdict"])
        ));
    }
    match report["blocking"].as_array() {
        Some(items) if items.is_empty() => {}
        Some(items) => problems.push(format!(
            "evidence.closure {rel} names {} blocking item(s), first: {}",
            items.len(),
            items.first().map(compact).unwrap_or_default()
        )),
        None => problems.push(format!("evidence.closure {rel} has no blocking list")),
    }
    if report["owner_acceptance"]["recorded"] != true {
        problems.push(format!(
            "evidence.closure {rel} does not record owner acceptance"
        ));
    }
    problems
}

/// Problems of an owner-acceptance record: a KVX file with `[progress.owner_acceptance]` (as
/// `tests/cwr/closure.py --accept` writes it) or a JSON object, carrying `owner_acceptance =
/// true` and a non-empty `accepted_by`.
fn acceptance_problems(rel: &str, text: &str) -> Vec<String> {
    let record: Option<Value> = if rel.ends_with(".kvx") {
        match load_sections(rel, text) {
            Ok(s) => s
                .get("progress.owner_acceptance")
                .map(|m| Value::Object(m.clone().into_iter().collect())),
            Err(e) => return vec![format!("evidence.owner_acceptance {e}")],
        }
    } else {
        match serde_json::from_str::<Value>(text) {
            Ok(v) => Some(v),
            Err(e) => return vec![format!("evidence.owner_acceptance {rel}: {e}")],
        }
    };
    let Some(record) = record else {
        return vec![format!(
            "evidence.owner_acceptance {rel} holds no [progress.owner_acceptance]"
        )];
    };
    let mut problems = Vec::new();
    if record["owner_acceptance"] != true {
        problems.push(format!(
            "evidence.owner_acceptance {rel}: owner_acceptance is not true"
        ));
    }
    if record["accepted_by"]
        .as_str()
        .is_none_or(|o| o.trim().is_empty())
    {
        problems.push(format!(
            "evidence.owner_acceptance {rel}: accepted_by names no owner"
        ));
    }
    problems
}

fn claim_pin(root: &Path, sections: &Sections) -> Result<Value, String> {
    let mut problems = Vec::new();
    for key in ["closure", "owner_acceptance"] {
        let Some(rel) = get(sections, "evidence", key).and_then(Value::as_str) else {
            problems.push(format!("evidence.{key} is null"));
            continue;
        };
        let text = match fs::read_to_string(root.join(rel)) {
            Ok(t) => t,
            Err(e) => {
                problems.push(format!("evidence.{key} {rel}: {e}"));
                continue;
            }
        };
        problems.extend(if key == "closure" {
            closure_problems(rel, &text)
        } else {
            acceptance_problems(rel, &text)
        });
    }
    if problems.is_empty() {
        Ok(get(sections, "evidence", "closure")
            .cloned()
            .unwrap_or(Value::Null))
    } else {
        Err(problems.join("; "))
    }
}

// ---------------------------------------------------------------------------------------------
// Builds

/// Every Build the release pins, computed by running `cwr build` from this executable.
struct BuildResults {
    control: Result<Value, String>,
    one_primitive: Result<Value, String>,
    drift: Result<Value, String>,
    pairs: Result<Value, String>,
    version_only: Result<Value, String>,
    silent_change: Result<Value, String>,
}

/// One drift Build: label `<task>/<arm>`, the drift file text and the hash registered for it.
struct DriftJob {
    label: String,
    text: String,
    registered: Option<String>,
}

struct BuildPlan {
    control_source: PathBuf,
    upgrades_source: PathBuf,
    pairs: Vec<String>,
    drifts: Result<Vec<DriftJob>, String>,
    /// `[registration] control_build` of every listed campaign file, by path; an error when a
    /// listed file is missing or unreadable.
    registered_controls: Result<Vec<(String, String)>, String>,
    /// The one-primitive Build hash registered by the H2 campaign, when registered; an error when
    /// the named registration file is missing or unreadable.
    registered_one_primitive: Result<Option<String>, String>,
    /// `[pair.<id>]` sections of the H4 campaign file (`build_a`, `build_b` per notice arm); an
    /// error when the named registration file is missing or unreadable.
    registered_pairs: Result<Sections, String>,
}

/// Concurrent `cwr build` processes; each runs the contract tests in its own Python processes.
const BUILD_WORKERS: usize = 12;

static SCRATCH_SEQ: AtomicUsize = AtomicUsize::new(0);

/// A private temporary directory removed on drop.
struct Scratch(PathBuf);

impl Scratch {
    fn new() -> Result<Scratch, String> {
        let n = SCRATCH_SEQ.fetch_add(1, Ordering::SeqCst);
        let dir = std::env::temp_dir().join(format!("cwr-release-{}-{n}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).map_err(|e| format!("{}: {e}", dir.display()))?;
        Ok(Scratch(dir))
    }
}

impl Drop for Scratch {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

/// Runs `cwr build <source> --out <scratch>/out --json [extra...]` with the scratch directory
/// as working directory and returns the Build hash.
fn build_hash(
    builder: &Path,
    scratch: &Path,
    source: &Path,
    extra: &[&str],
) -> Result<String, String> {
    let output = Command::new(builder)
        .current_dir(scratch)
        .arg("build")
        .arg(source)
        .arg("--out")
        .arg(scratch.join("out"))
        .arg("--json")
        .args(extra)
        .output()
        .map_err(|e| format!("cannot run {}: {e}", builder.display()))?;
    let what = format!("cwr build {} {}", source.display(), extra.join(" "));
    let report: Value = serde_json::from_slice(&output.stdout).map_err(|_| {
        format!(
            "{} exited {:?}: {}",
            what.trim_end(),
            output.status.code(),
            String::from_utf8_lossy(&output.stderr).trim()
        )
    })?;
    match report.pointer("/build/hash").and_then(Value::as_str) {
        Some(h) if output.status.success() => Ok(h.to_string()),
        _ => Err(format!(
            "{} exited {:?}: {}",
            what.trim_end(),
            output.status.code(),
            report
                .get("summary")
                .and_then(Value::as_str)
                .unwrap_or("no summary")
        )),
    }
}

/// The `--x-drift` file for one task and arm, byte-identical to the file
/// `benchmark/campaigns/h1h2.py::drift_file_text` compiled at registration (the schedule bytes
/// are embedded in the Build, so the text decides the hash): a two-line header comment, `[meta]`,
/// then one `[drift.<n>]` section per entry, values as JSON with `", "` and `": "` separators.
fn drift_file_text(task: &str, arm: &str, entries: &[Value]) -> Result<String, String> {
    let mut out = format!(
        "# H1 {arm} drift schedule for {task}, generated from benchmark/campaigns/h1.kvx by\n\
         # benchmark/campaigns/h1h2.py; the manual of the Build is unchanged.\n\n\
         [meta]\nformat = \"cwr.drift\"\nschema_version = 1\nexample = false\n"
    );
    for e in entries {
        let n = e
            .get("n")
            .and_then(Value::as_u64)
            .ok_or(format!("{task} {arm}: a drift entry has no n"))?;
        out.push_str(&format!("\n[drift.{n}]\n"));
        for key in ["turn", "primitive", "change", "expr", "failure", "doc"] {
            let v = e
                .get(key)
                .ok_or(format!("{task} {arm} drift {n}: no {key}"))?;
            out.push_str(&format!("{key} = {}\n", kvx_literal(v)));
        }
    }
    Ok(out)
}

fn load_kvx_file(path: &Path) -> Result<Sections, String> {
    let text = fs::read_to_string(path).map_err(|e| format!("{}: {e}", path.display()))?;
    load_sections(&path.display().to_string(), &text)
}

/// The registration file `[section] registered_in` names; `Ok(None)` when the release file names
/// none, an error naming the file when it is missing or unreadable.
fn registration(
    root: &Path,
    sections: &Sections,
    section: &str,
) -> Result<Option<Sections>, String> {
    match get(sections, section, "registered_in").and_then(Value::as_str) {
        None | Some("") => Ok(None),
        Some(rel) => load_kvx_file(&root.join(rel))
            .map(Some)
            .map_err(|e| format!("{section}.registered_in {rel} cannot be read: {e}")),
    }
}

fn drift_jobs(root: &Path, sections: &Sections) -> Result<Vec<DriftJob>, String> {
    let rel = get(sections, "build.x_drift", "schedules")
        .and_then(Value::as_str)
        .ok_or("build.x_drift.schedules names no campaign file")?;
    let campaign = load_kvx_file(&root.join(rel))?;
    let arms: Vec<String> = get(sections, "build.x_drift", "arms")
        .and_then(Value::as_array)
        .map(|a| {
            a.iter()
                .filter_map(Value::as_str)
                .map(String::from)
                .collect()
        })
        .unwrap_or_default();
    let mut jobs = Vec::new();
    for (name, body) in &campaign {
        let Some(task) = name.strip_prefix("task.") else {
            continue;
        };
        for arm in &arms {
            let key = arm.replace('-', "_");
            let entries = body
                .get(&key)
                .and_then(Value::as_array)
                .ok_or(format!("{rel} [{name}] has no {key} schedule"))?;
            jobs.push(DriftJob {
                label: format!("{task}/{arm}"),
                text: drift_file_text(task, arm, entries)?,
                registered: body
                    .get(&format!("{key}_build"))
                    .and_then(Value::as_str)
                    .map(String::from),
            });
        }
    }
    if jobs.is_empty() {
        return Err(format!("{rel} registers no drift schedule"));
    }
    Ok(jobs)
}

impl BuildPlan {
    fn from_sections(root: &Path, sections: &Sections) -> BuildPlan {
        let control_source = root.join(get_str(
            sections,
            "build.refworld_control",
            "source",
            "examples/refworld",
        ));
        let upgrades_source = root.join(get_str(
            sections,
            "build.upgrade_pairs",
            "source",
            "examples/refworld-upgrades",
        ));
        let pairs = get(sections, "build.upgrade_pairs", "pairs")
            .and_then(Value::as_array)
            .map(|a| {
                a.iter()
                    .filter_map(Value::as_str)
                    .map(String::from)
                    .collect()
            })
            .unwrap_or_default();
        let campaigns = get(sections, "build.refworld_control", "campaigns")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default();
        let registered_controls = campaigns
            .iter()
            .filter_map(Value::as_str)
            .map(|rel| {
                let c = load_kvx_file(&root.join(rel)).map_err(|e| {
                    format!(
                        "campaign file {rel} listed in build.refworld_control cannot be read: {e}"
                    )
                })?;
                Ok(get(&c, "registration", "control_build")
                    .and_then(Value::as_str)
                    .map(|h| (rel.to_string(), h.to_string())))
            })
            .filter_map(Result::transpose)
            .collect::<Result<Vec<_>, String>>();
        let registered_one_primitive =
            registration(root, sections, "build.x_one_primitive").map(|c| {
                c.and_then(|c| {
                    get(&c, "registration", "builds")
                        .and_then(|b| b.get("one-primitive"))
                        .and_then(Value::as_str)
                        .map(String::from)
                })
            });
        BuildPlan {
            control_source,
            upgrades_source,
            pairs,
            drifts: drift_jobs(root, sections),
            registered_controls,
            registered_one_primitive,
            registered_pairs: registration(root, sections, "build.upgrade_pairs")
                .map(Option::unwrap_or_default),
        }
    }

    fn run(&self, builder: &Path) -> BuildResults {
        enum Job<'a> {
            Control,
            OnePrimitive,
            Drift(&'a DriftJob),
            Pair(&'a str),
        }
        enum Done {
            Hash(Result<String, String>),
            Pair(Result<PairHashes, String>),
        }
        let mut jobs: Vec<(usize, Job)> = vec![(0, Job::Control), (1, Job::OnePrimitive)];
        for p in &self.pairs {
            jobs.push((jobs.len(), Job::Pair(p)));
        }
        if let Ok(drifts) = &self.drifts {
            for d in drifts {
                jobs.push((jobs.len(), Job::Drift(d)));
            }
        }
        let total = jobs.len();
        let queue = Mutex::new(jobs);
        let done: Mutex<BTreeMap<usize, Done>> = Mutex::new(BTreeMap::new());
        std::thread::scope(|scope| {
            for _ in 0..BUILD_WORKERS.min(total) {
                scope.spawn(|| loop {
                    let next = queue.lock().ok().and_then(|mut q| q.pop());
                    let Some((index, job)) = next else {
                        break;
                    };
                    let result = match job {
                        Job::Control => Done::Hash(
                            Scratch::new()
                                .and_then(|s| build_hash(builder, &s.0, &self.control_source, &[])),
                        ),
                        Job::OnePrimitive => Done::Hash(Scratch::new().and_then(|s| {
                            build_hash(builder, &s.0, &self.control_source, &["--x-one-primitive"])
                        })),
                        Job::Drift(d) => Done::Hash(Scratch::new().and_then(|s| {
                            let file = s.0.join("drift.kvx");
                            fs::write(&file, &d.text)
                                .map_err(|e| format!("{}: {e}", file.display()))?;
                            let file = file.display().to_string();
                            build_hash(builder, &s.0, &self.control_source, &["--x-drift", &file])
                        })),
                        Job::Pair(p) => Done::Pair(self.build_pair(builder, p)),
                    };
                    if let Ok(mut d) = done.lock() {
                        d.insert(index, result);
                    }
                });
            }
        });
        let mut done = done.into_inner().unwrap_or_default();
        let mut hash = |i: usize| match done.remove(&i) {
            Some(Done::Hash(h)) => h,
            _ => Err("the build did not run".to_string()),
        };
        let control = hash(0);
        let one = hash(1)
            .and_then(|h| distinct(h, &control, "--x-one-primitive"))
            .and_then(|h| match &self.registered_one_primitive {
                Err(e) => Err(e.clone()),
                Ok(Some(r)) if *r != h => Err(format!(
                    "--x-one-primitive Build {h} differs from the {r} registered for the H2 campaign"
                )),
                Ok(_) => Ok(h),
            });
        let control = control.and_then(|h| {
            let registered = self.registered_controls.as_ref().map_err(String::clone)?;
            let stale: Vec<String> = registered
                .iter()
                .filter(|(_, r)| *r != h)
                .map(|(f, r)| format!("{f} registered {r}"))
                .collect();
            if stale.is_empty() {
                Ok(h)
            } else {
                Err(format!("the control Build is {h} but {}", stale.join(", ")))
            }
        });
        let pair_base = 2;
        let drift_base = pair_base + self.pairs.len();
        let drift = match &self.drifts {
            Err(e) => Err(e.clone()),
            Ok(drifts) => {
                let mut map = Map::new();
                let mut errors = Vec::new();
                for (k, d) in drifts.iter().enumerate() {
                    match hash(drift_base + k).and_then(|h| distinct(h, &control, "--x-drift")) {
                        Ok(h) => match &d.registered {
                            Some(r) if *r != h => errors.push(format!(
                                "{}: Build {h} differs from the registered {r}",
                                d.label
                            )),
                            _ => {
                                map.insert(d.label.clone(), Value::String(h));
                            }
                        },
                        Err(e) => errors.push(format!("{}: {e}", d.label)),
                    }
                }
                if errors.is_empty() {
                    Ok(Value::Object(map))
                } else {
                    Err(format!(
                        "{} of {}: {}",
                        errors.len(),
                        drifts.len(),
                        errors.join("; ")
                    ))
                }
            }
        };
        let mut pairs = Map::new();
        let mut vo = Map::new();
        let mut sc = Map::new();
        let (mut pair_errors, mut vo_errors, mut sc_errors) = (Vec::new(), Vec::new(), Vec::new());
        for (k, pair) in self.pairs.iter().enumerate() {
            match done.remove(&(pair_base + k)) {
                Some(Done::Pair(Ok(p))) => {
                    pairs.insert(pair.clone(), json!({"a": p.a, "b": p.b}));
                    match p.version_only {
                        Ok(h) => {
                            vo.insert(pair.clone(), Value::String(h));
                        }
                        Err(e) => vo_errors.push(format!("{pair}: {e}")),
                    }
                    match p.silent {
                        Ok(h) => {
                            sc.insert(pair.clone(), Value::String(h));
                        }
                        Err(e) => sc_errors.push(format!("{pair}: {e}")),
                    }
                }
                Some(Done::Pair(Err(e))) => {
                    pair_errors.push(format!("{pair}: {e}"));
                    vo_errors.push(format!("{pair}: the pair failed to build or verify"));
                    sc_errors.push(format!("{pair}: the pair failed to build or verify"));
                }
                _ => pair_errors.push(format!("{pair}: the build did not run")),
            }
        }
        let finish = |map: Map<String, Value>, errors: Vec<String>| {
            if self.pairs.is_empty() {
                Err("build.upgrade_pairs.pairs lists no pair".to_string())
            } else if errors.is_empty() {
                Ok(Value::Object(map))
            } else {
                Err(errors.join("; "))
            }
        };
        BuildResults {
            control: control.map(Value::String),
            one_primitive: one.map(Value::String),
            drift,
            pairs: finish(pairs, pair_errors),
            version_only: finish(vo, vo_errors),
            silent_change: finish(sc, sc_errors),
        }
    }

    /// Builds side A, then side B with `--previous A`, then B again under each notice flag,
    /// all in one Build root so the diff stage sees A.
    fn build_pair(&self, builder: &Path, pair: &str) -> Result<PairHashes, String> {
        let s = Scratch::new()?;
        let a_src = self.upgrades_source.join(pair).join("A");
        let b_src = self.upgrades_source.join(pair).join("B");
        let registered = self
            .registered_pairs
            .as_ref()
            .map_err(String::clone)?
            .get(&format!("pair.{pair}"));
        let expect = |side: &str, arm: Option<&str>, hash: String| -> Result<String, String> {
            let want = registered
                .and_then(|r| r.get(side))
                .and_then(|v| match arm {
                    Some(arm) => v.get(arm).and_then(Value::as_str),
                    None => v.as_str(),
                });
            match want {
                Some(w) if w != hash => Err(format!(
                    "{side}{} Build {hash} differs from the {w} registered for the H4 campaign",
                    arm.map(|a| format!(" {a}")).unwrap_or_default()
                )),
                _ => Ok(hash),
            }
        };
        let a = build_hash(builder, &s.0, &a_src, &[])?;
        let b = build_hash(builder, &s.0, &b_src, &["--previous", &a])?;
        let flagged = |flag: &str, arm: &str| {
            build_hash(builder, &s.0, &b_src, &["--previous", &a, flag])
                .and_then(|h| distinct(h, &Ok(b.clone()), flag))
                .and_then(|h| expect("build_b", Some(arm), h))
        };
        let version_only = flagged("--x-version-only", "version-only");
        let silent = flagged("--x-silent-change", "silent");
        Ok(PairHashes {
            a: expect("build_a", None, a)?,
            b: expect("build_b", Some("changelog"), b)?,
            version_only,
            silent,
        })
    }
}

struct PairHashes {
    a: String,
    b: String,
    version_only: Result<String, String>,
    silent: Result<String, String>,
}

/// An experimental Build whose hash equals the Build compiled without the flag was compiled
/// without the flag taking effect; it cannot stand for an ablation arm.
fn distinct(hash: String, plain: &Result<String, String>, flag: &str) -> Result<String, String> {
    match plain {
        Ok(c) if *c == hash => Err(format!(
            "{flag} produced the same Build {hash} as without the flag: the flag had no effect"
        )),
        _ => Ok(hash),
    }
}

// ---------------------------------------------------------------------------------------------
// Writing

/// Renders a JSON value as a KVX literal: `", "` between items and `": "` after keys, object
/// keys sorted.
pub fn kvx_literal(v: &Value) -> String {
    match v {
        Value::Array(items) => format!(
            "[{}]",
            items.iter().map(kvx_literal).collect::<Vec<_>>().join(", ")
        ),
        Value::Object(map) => {
            let mut entries: Vec<(&String, &Value)> = map.iter().collect();
            entries.sort_by(|a, b| a.0.cmp(b.0));
            let body: Vec<String> = entries
                .into_iter()
                .map(|(k, v)| format!("{}: {}", kvx::quote(k), kvx_literal(v)))
                .collect();
            format!("{{{}}}", body.join(", "))
        }
        Value::String(s) => kvx::quote(s),
        other => other.to_string(),
    }
}

/// The lines `pin --write` rewrites: every computed pin except the version (`CWR-R019`) and the
/// claim (`CWR-R015`). A pin whose computation failed is left out, so its recorded line stays.
fn write_updates(results: &[PinResult]) -> Vec<(&'static str, &'static str, Value)> {
    results
        .iter()
        .filter(|r| r.spec.code != "CWR-R019" && r.spec.code != "CWR-R015")
        .filter_map(|r| {
            r.computed
                .as_ref()
                .ok()
                .map(|v| (r.spec.section, r.spec.key, v.clone()))
        })
        .collect()
}

/// Replaces `path` with `bytes` atomically: the bytes go to a sibling temporary file, which is
/// fsynced and renamed over `path`, then the directory is fsynced. A crash leaves either the old
/// or the new file, never a truncated one.
fn write_atomic(path: &Path, bytes: &[u8]) -> std::io::Result<()> {
    use std::io::Write;
    let dir = match path.parent() {
        Some(d) if !d.as_os_str().is_empty() => d.to_path_buf(),
        _ => PathBuf::from("."),
    };
    let name = path
        .file_name()
        .map(|n| n.to_string_lossy().into_owned())
        .unwrap_or_default();
    let tmp = dir.join(format!(".{name}.{}.tmp", std::process::id()));
    let written = fs::File::create(&tmp).and_then(|mut f| {
        f.write_all(bytes)?;
        f.sync_all()
    });
    if let Err(e) = written.and_then(|()| fs::rename(&tmp, path)) {
        let _ = fs::remove_file(&tmp);
        return Err(e);
    }
    fs::File::open(&dir).and_then(|d| d.sync_all())
}

/// Replaces the value of each `(section, key)` line in a KVX text, keeping every other byte.
/// Errors when an update names a key the text does not contain.
pub fn rewrite(text: &str, updates: &[(&str, &str, Value)]) -> Result<String, String> {
    let mut done = vec![false; updates.len()];
    let mut section = String::new();
    let mut out = String::with_capacity(text.len());
    for line in text.split_inclusive('\n') {
        let body = line.trim_end_matches('\n');
        if let Some(name) = body.strip_prefix('[').and_then(|b| b.strip_suffix(']')) {
            section = name.to_string();
            out.push_str(line);
            continue;
        }
        let mut replaced = false;
        if let Some((key, _)) = body.split_once(" = ") {
            for (i, (s, k, v)) in updates.iter().enumerate() {
                if *s == section && *k == key {
                    out.push_str(&format!("{key} = {}\n", kvx_literal(v)));
                    if let Some(d) = done.get_mut(i) {
                        *d = true;
                    }
                    replaced = true;
                    break;
                }
            }
        }
        if !replaced {
            out.push_str(line);
        }
    }
    let missing: Vec<String> = updates
        .iter()
        .zip(&done)
        .filter(|(_, d)| !**d)
        .map(|((s, k, _), _)| format!("{s}.{k}"))
        .collect();
    if missing.is_empty() {
        Ok(out)
    } else {
        Err(format!(
            "the release file has no line for {}",
            missing.join(", ")
        ))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tmp(name: &str) -> PathBuf {
        let d =
            std::env::temp_dir().join(format!("cwr-release-test-{}-{name}", std::process::id()));
        let _ = fs::remove_dir_all(&d);
        fs::create_dir_all(&d).unwrap();
        d
    }

    #[test]
    fn tree_digest_matches_the_python_layout() {
        let d = tmp("tree");
        fs::create_dir_all(d.join("b")).unwrap();
        fs::create_dir_all(d.join("__pycache__")).unwrap();
        fs::write(d.join("a.txt"), "x").unwrap();
        fs::write(d.join("b/c.txt"), "yz").unwrap();
        fs::write(d.join("__pycache__/skip.pyc"), "no").unwrap();
        let mut h = Sha256::new();
        h.update(b"a.txt\x001\x00x");
        h.update(b"b/c.txt\x002\x00yz");
        let want = format!("sha256:{}", hex(&h.finalize()));
        assert_eq!(tree_digest(&d).unwrap(), want);
        fs::remove_dir_all(&d).unwrap();
    }

    #[test]
    fn suite_digest_counts_only_task_directories() {
        let d = tmp("suite");
        fs::create_dir_all(d.join("t001-a")).unwrap();
        fs::create_dir_all(d.join("_authoring")).unwrap();
        fs::write(d.join("t001-a/task.kvx"), "k").unwrap();
        fs::write(d.join("_authoring/x.py"), "ignored").unwrap();
        fs::write(d.join("README.md"), "ignored").unwrap();
        let mut h = Sha256::new();
        h.update(b"t001-a/task.kvx\x001\x00k");
        let (digest, count) = suite_digest(&d).unwrap();
        assert_eq!(count, 1);
        assert_eq!(digest, format!("sha256:{}", hex(&h.finalize())));
        assert!(is_task_dir("t060-report-pipeline"));
        assert!(!is_task_dir("t60-x") && !is_task_dir("t001_x") && !is_task_dir("t001-"));
        fs::remove_dir_all(&d).unwrap();
    }

    #[test]
    fn cassette_digest_follows_format_md_and_refuses_an_empty_set() {
        let d = tmp("cass");
        assert!(cassette_set_digest(&d).unwrap_err().contains("no cassette"));
        fs::create_dir_all(d.join("t1")).unwrap();
        fs::write(d.join("t1/seed-1.jsonl"), "{}\n").unwrap();
        fs::write(d.join("notes.md"), "ignored").unwrap();
        let line = format!("t1/seed-1.jsonl\t{}\n", hex(&Sha256::digest(b"{}\n")));
        let want = format!("sha256:{}", hex(&Sha256::digest(line.as_bytes())));
        assert_eq!(cassette_set_digest(&d).unwrap(), want);
        fs::remove_dir_all(&d).unwrap();
    }

    #[test]
    fn rewrite_replaces_only_the_named_lines() {
        let text = "# c\n[a]\nx = null\ny = 1\n\n[b]\nx = null\n";
        let out = rewrite(
            text,
            &[
                ("b", "x", json!({"k": "v", "a": ["s", 2]})),
                ("a", "y", json!("s")),
            ],
        )
        .unwrap();
        assert_eq!(
            out,
            "# c\n[a]\nx = null\ny = \"s\"\n\n[b]\nx = {\"a\": [\"s\", 2], \"k\": \"v\"}\n"
        );
        assert!(load_sections("t", &out).is_ok());
        assert!(rewrite(text, &[("c", "x", Value::Null)])
            .unwrap_err()
            .contains("c.x"));
    }

    #[test]
    fn toml_versions_resolve_workspace_inheritance() {
        let ws = "[workspace]\nmembers = []\n\n[workspace.package]\nversion = \"0.1.0\"\n";
        let cli = "[package]\nname = \"cwr-cli\"\nversion.workspace = true\n";
        assert_eq!(toml_version(cli, "package"), Some(None));
        assert_eq!(
            toml_version(ws, "workspace.package"),
            Some(Some("0.1.0".into()))
        );
        assert_eq!(toml_version(ws, "project"), None);
    }

    #[test]
    fn tree_digest_equals_common_py_with_build_dirs_and_symlinks() {
        let d = tmp("tree-py");
        for sub in [
            "build",
            "dist",
            "pkg.egg-info",
            ".pytest_cache",
            "src",
            "__pycache__",
        ] {
            fs::create_dir_all(d.join(sub)).unwrap();
            fs::write(d.join(sub).join("f.txt"), sub).unwrap();
        }
        std::os::unix::fs::symlink(d.join("src/f.txt"), d.join("link.txt")).unwrap();
        std::os::unix::fs::symlink(d.join("src"), d.join("dirlink")).unwrap();
        std::os::unix::fs::symlink(d.join("missing"), d.join("broken")).unwrap();
        let common = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../benchmark/predicates");
        let py = Command::new("python3")
            .arg("-I")
            .arg("-c")
            .arg("import sys; sys.path.insert(0, sys.argv[1]); import common; print(common.tree_digest(sys.argv[2]))")
            .arg(&common)
            .arg(&d)
            .output();
        let rust = tree_digest(&d).unwrap();
        if let Ok(out) = py {
            if out.status.success() {
                assert_eq!(String::from_utf8_lossy(&out.stdout).trim(), rust);
            }
        }
        let mut h = Sha256::new();
        for sub in [".pytest_cache", "build", "dist"] {
            h.update(format!("{sub}/f.txt\0{}\0{sub}", sub.len()).as_bytes());
        }
        h.update(b"link.txt\x003\x00src");
        h.update(b"pkg.egg-info/f.txt\x0012\x00pkg.egg-info");
        h.update(b"src/f.txt\x003\x00src");
        assert_eq!(rust, format!("sha256:{}", hex(&h.finalize())));
        fs::remove_dir_all(&d).unwrap();
    }

    #[test]
    fn published_digest_skips_ignored_files_in_a_checkout_and_equals_the_bundle() {
        let repo = tmp("published-git");
        let bundle = tmp("published-bundle");
        for root in [&repo, &bundle] {
            fs::create_dir_all(root.join("res/cell")).unwrap();
            fs::write(root.join("res/cell/ledger.jsonl"), "{}\n").unwrap();
            fs::write(root.join("res/analysis.json"), "[]").unwrap();
        }
        fs::write(repo.join(".gitignore"), "*.log\n/res/_pilot/\n").unwrap();
        fs::create_dir_all(repo.join("res/_pilot")).unwrap();
        fs::write(repo.join("res/_pilot/x.json"), "pilot").unwrap();
        fs::write(repo.join("res/cell/kernel.log"), "noise").unwrap();
        fs::write(repo.join("res/.hidden"), "hidden").unwrap();
        let init = Command::new("git")
            .arg("-C")
            .arg(&repo)
            .args(["init", "-q"])
            .status();
        if !init.is_ok_and(|s| s.success()) {
            return;
        }
        let checkout = published_digest(&repo, "res").unwrap();
        let unpacked = published_digest(&bundle, "res").unwrap();
        assert_eq!(checkout, unpacked);
        assert_eq!(unpacked, tree_digest(&bundle.join("res")).unwrap());
        fs::write(repo.join("res/cell/kernel.log"), "more noise").unwrap();
        assert_eq!(published_digest(&repo, "res").unwrap(), checkout);
        fs::write(repo.join("res/analysis.json"), "[1]").unwrap();
        assert_ne!(published_digest(&repo, "res").unwrap(), checkout);
        assert!(published_digest(&repo, "missing").is_err());
        fs::remove_dir_all(&repo).unwrap();
        fs::remove_dir_all(&bundle).unwrap();
    }

    #[test]
    fn pin_write_keeps_the_line_of_a_failed_pin() {
        let text = "[pin.compiler]\nrevision = \"sha256:old\"\n\n[pin.kernel]\nrevision = \"sha256:keep\"\n";
        let results = vec![
            PinResult {
                spec: PINS[0],
                computed: Ok(json!("sha256:new")),
                recorded: json!("sha256:old"),
            },
            PinResult {
                spec: PINS[1],
                computed: Err("the build did not run".into()),
                recorded: json!("sha256:keep"),
            },
        ];
        let updates = write_updates(&results);
        assert_eq!(updates.len(), 1);
        let out = rewrite(text, &updates).unwrap();
        assert!(out.contains("revision = \"sha256:new\""), "{out}");
        assert!(out.contains("revision = \"sha256:keep\""), "{out}");
        assert!(!out.contains("null"), "{out}");
        let d = tmp("atomic");
        let f = d.join("release.kvx");
        fs::write(&f, text).unwrap();
        write_atomic(&f, out.as_bytes()).unwrap();
        assert_eq!(fs::read_to_string(&f).unwrap(), out);
        assert_eq!(
            fs::read_dir(&d).unwrap().count(),
            1,
            "no temporary file left"
        );
        fs::remove_dir_all(&d).unwrap();
    }

    #[test]
    fn an_unreadable_registration_source_is_an_error() {
        let d = tmp("registration");
        let text = "[build.refworld_control]\ncampaigns = [\"missing/h1.kvx\"]\n\n[build.x_one_primitive]\nregistered_in = \"missing/h2.kvx\"\n\n[build.upgrade_pairs]\nregistered_in = \"missing/h4.kvx\"\n";
        let sections = load_sections("release.kvx", text).unwrap();
        let plan = BuildPlan::from_sections(&d, &sections);
        assert!(plan
            .registered_controls
            .unwrap_err()
            .contains("missing/h1.kvx"));
        assert!(plan
            .registered_one_primitive
            .unwrap_err()
            .contains("missing/h2.kvx"));
        assert!(plan
            .registered_pairs
            .unwrap_err()
            .contains("missing/h4.kvx"));
        let none = load_sections("release.kvx", "[meta]\nrelease = null\n").unwrap();
        let plan = BuildPlan::from_sections(&d, &none);
        assert_eq!(plan.registered_controls, Ok(Vec::new()));
        assert_eq!(plan.registered_one_primitive, Ok(None));
        assert!(plan.registered_pairs.unwrap().is_empty());
        fs::remove_dir_all(&d).unwrap();
    }

    #[test]
    fn a_claim_needs_accepted_closure_and_owner_acceptance() {
        let d = tmp("claim");
        let sections = load_sections(
            "release.kvx",
            "[evidence]\nclosure = \"closure.json\"\nowner_acceptance = \"qualification.kvx\"\n",
        )
        .unwrap();
        fs::write(
            d.join("closure.json"),
            r#"{"closure": "cwr", "verdict": "REFUSED", "blocking": ["CWR-001 is UNRUN"], "owner_acceptance": {"recorded": false}}"#,
        )
        .unwrap();
        fs::write(
            d.join("qualification.kvx"),
            "[progress.owner_acceptance]\nowner_acceptance = false\naccepted_by = \"\"\n",
        )
        .unwrap();
        let e = claim_pin(&d, &sections).unwrap_err();
        for part in [
            "REFUSED",
            "1 blocking item",
            "owner acceptance",
            "not true",
            "no owner",
        ] {
            assert!(e.contains(part), "{part}: {e}");
        }
        fs::write(
            d.join("closure.json"),
            r#"{"closure": "cwr", "verdict": "ACCEPTED", "blocking": [], "owner_acceptance": {"recorded": true}}"#,
        )
        .unwrap();
        fs::write(
            d.join("qualification.kvx"),
            "[progress.owner_acceptance]\nowner_acceptance = true\naccepted_by = \"Owner\"\n",
        )
        .unwrap();
        assert_eq!(claim_pin(&d, &sections), Ok(json!("closure.json")));
        fs::remove_dir_all(&d).unwrap();
    }

    #[test]
    fn a_missing_pin_is_named_and_a_match_is_clean() {
        let spec = PINS[0];
        let missing = PinResult {
            spec,
            computed: Ok(json!("sha256:00")),
            recorded: Value::Null,
        };
        assert!(missing.problem().unwrap().contains("not recorded"));
        let differs = PinResult {
            spec,
            computed: Ok(json!("sha256:00")),
            recorded: json!("sha256:11"),
        };
        assert!(differs.problem().unwrap().contains("recorded"));
        let ok = PinResult {
            spec,
            computed: Ok(json!("sha256:00")),
            recorded: json!("sha256:00"),
        };
        assert_eq!(ok.problem(), None);
        let codes: Vec<&str> = PINS.iter().map(|p| p.code).collect();
        let mut sorted = codes.clone();
        sorted.sort();
        sorted.dedup();
        assert_eq!(sorted.len(), PINS.len(), "codes are unique");
    }
}
