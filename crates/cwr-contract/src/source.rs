// Copyright Sidiora Labs Inc. CWR® 2026-2027
// SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0

//! The World Source directory (world-source.md section 2, clause 3.1) and its typed model.
//!
//! Layout accepted, every other path is CWR-C001 `unknown-file`:
//!
//! | Path | Class |
//! |---|---|
//! | `world.kvx` (required) | world |
//! | `CHANGELOG.kvx` (required) | changelog |
//! | `lib/<module>.py` | lib |
//! | `contracts/<module>.kvx` | contract |
//! | `adapters/<module>.kvx` | adapter |
//! | `adapters/<module>/live.py` | adapter_live |
//! | `migrations/<name>.py`, name in `[a-z0-9_]+` | migration |
//! | `fixtures/<module>/<name>.kvx` | fixture |
//! | `manual/{intro,conventions,types,examples}.md` | manual |
//! | `drift/<name>.kvx` (compiler.md section 10) | drift |
//!
//! Directories may be absent. Symbolic links and other non-regular files are rejected.
//! Every KVX file is parsed strictly and typed; Python and Markdown files are inventoried
//! with their SHA-256 and checked for UTF-8; resolve-stage questions about their content
//! (L001, L002, D001) belong to the compiler.

use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::path::Path;

use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

use crate::adapter::{self, AdapterFile};
use crate::changelog::{self, Changelog, MigrationRefError};
use crate::error::{Error, ErrorKind, Errors};
use crate::fixture::{self, DriftFile, FixtureFile};
use crate::kvx;
use crate::primitive::{self, ContractFile};
use crate::read::{self, Ctx};
use crate::world::{self, WorldFile};

/// What a source file is.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum FileClass {
    /// `world.kvx`.
    World,
    /// `CHANGELOG.kvx`.
    Changelog,
    /// `lib/<module>/*.py`.
    Lib,
    /// `contracts/<module>.kvx`.
    Contract,
    /// `adapters/<module>.kvx`.
    Adapter,
    /// `adapters/<module>/live.py`.
    AdapterLive,
    /// `migrations/<name>.py`.
    Migration,
    /// `fixtures/<module>/*.kvx`.
    Fixture,
    /// `manual/*.md`.
    Manual,
    /// `drift/*.kvx`.
    Drift,
}

impl FileClass {
    /// The path pattern of the class, as world-source.md section 2 writes it.
    pub fn label(self) -> &'static str {
        match self {
            FileClass::World => "world.kvx",
            FileClass::Changelog => "CHANGELOG.kvx",
            FileClass::Lib => "lib/<module>.py",
            FileClass::Contract => "contracts/<module>.kvx",
            FileClass::Adapter => "adapters/<module>.kvx",
            FileClass::AdapterLive => "adapters/<module>/live.py",
            FileClass::Migration => "migrations/<name>.py",
            FileClass::Fixture => "fixtures/<module>/<name>.kvx",
            FileClass::Manual => "manual/<section>.md",
            FileClass::Drift => "drift/<name>.kvx",
        }
    }

    /// True for the classes whose files are parsed as KVX into the typed model.
    pub fn is_kvx(self) -> bool {
        matches!(
            self,
            FileClass::World
                | FileClass::Changelog
                | FileClass::Contract
                | FileClass::Adapter
                | FileClass::Fixture
                | FileClass::Drift
        )
    }
}

/// One inventoried file. `sha256` is recorded for files whose bytes are not represented by
/// the typed model (Python and Markdown); KVX files are represented by their typed model.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SourceFile {
    /// Source-relative, `/`-separated path.
    pub path: String,
    /// What the file is.
    pub class: FileClass,
    /// Lowercase hex SHA-256 of the bytes for non-KVX files; `None` for KVX files.
    pub sha256: Option<String>,
}

/// A migration file `migrations/<name>.py` defining `migrate(state)`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct MigrationFile {
    /// Source-relative path, `migrations/<name>.py`.
    pub path: String,
    /// Ids of the changelog changes that name this migration.
    pub changes: Vec<String>,
}

/// The typed World Source.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct WorldSource {
    /// Typed `world.kvx`.
    pub world: WorldFile,
    /// Typed `CHANGELOG.kvx`.
    pub changelog: Changelog,
    /// Primitive contract files, in path order when read from a source tree.
    pub contracts: Vec<ContractFile>,
    /// Adapter contract files, in path order when read from a source tree.
    pub adapters: Vec<AdapterFile>,
    /// Migration files, sorted by path.
    pub migrations: Vec<MigrationFile>,
    /// Fixture files, in path order when read from a source tree.
    pub fixtures: Vec<FixtureFile>,
    /// Drift schedules, in path order when read from a source tree.
    pub drift: Vec<DriftFile>,
    /// Every file of the source, sorted by path.
    pub files: Vec<SourceFile>,
}

/// An in-memory source tree: relative `/`-separated path to file bytes, or `None` for a
/// path that exists but is not a regular file.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct SourceTree {
    /// Files keyed by path, so iteration is in sorted path order.
    pub files: BTreeMap<String, Option<Vec<u8>>>,
}

impl SourceTree {
    /// Reads a directory recursively. Symbolic links are recorded as non-regular files.
    pub fn read_dir(root: &Path) -> Result<SourceTree, Errors> {
        let meta = fs::metadata(root).map_err(|e| {
            Errors(vec![Error::new(
                root.display().to_string(),
                1,
                1,
                ErrorKind::Io,
                format!("cannot read World Source directory: {e}"),
            )])
        })?;
        if !meta.is_dir() {
            return Err(Errors(vec![Error::new(
                root.display().to_string(),
                1,
                1,
                ErrorKind::Io,
                "World Source must be a directory",
            )]));
        }
        let mut tree = SourceTree::default();
        let mut errors = Vec::new();
        walk(root, "", &mut tree, &mut errors);
        if errors.is_empty() {
            Ok(tree)
        } else {
            Err(Errors::sorted(errors))
        }
    }
}

fn walk(dir: &Path, prefix: &str, tree: &mut SourceTree, errors: &mut Vec<Error>) {
    let entries = match fs::read_dir(dir) {
        Ok(e) => e,
        Err(e) => {
            errors.push(Error::new(
                prefix.trim_end_matches('/'),
                1,
                1,
                ErrorKind::Io,
                format!("cannot list directory: {e}"),
            ));
            return;
        }
    };
    let mut names = Vec::new();
    for entry in entries {
        match entry {
            Ok(entry) => names.push(entry),
            Err(e) => errors.push(Error::new(
                prefix.trim_end_matches('/'),
                1,
                1,
                ErrorKind::Io,
                format!("cannot read directory entry: {e}"),
            )),
        }
    }
    names.sort_by_key(|e| e.file_name());
    for entry in names {
        let name = entry.file_name().to_string_lossy().into_owned();
        let rel = format!("{prefix}{name}");
        let Ok(ft) = entry.file_type() else {
            tree.files.insert(rel, None);
            continue;
        };
        if ft.is_symlink() {
            tree.files.insert(rel, None);
        } else if ft.is_dir() {
            walk(&entry.path(), &format!("{rel}/"), tree, errors);
        } else if ft.is_file() {
            match fs::read(entry.path()) {
                Ok(bytes) => {
                    tree.files.insert(rel, Some(bytes));
                }
                Err(e) => errors.push(Error::new(
                    rel,
                    1,
                    1,
                    ErrorKind::Io,
                    format!("cannot read file: {e}"),
                )),
            }
        } else {
            tree.files.insert(rel, None);
        }
    }
}

const MANUAL_FILES: [&str; 4] = ["intro.md", "conventions.md", "types.md", "examples.md"];

/// Classifies a relative path; `None` means the grammar does not define it.
pub fn classify(path: &str) -> Option<(FileClass, Option<String>)> {
    let parts: Vec<&str> = path.split('/').collect();
    let stem = |name: &str, ext: &str| {
        name.strip_suffix(ext)
            .filter(|s| read::is_ident(s))
            .map(str::to_string)
    };
    match parts.as_slice() {
        ["world.kvx"] => Some((FileClass::World, None)),
        ["CHANGELOG.kvx"] => Some((FileClass::Changelog, None)),
        ["lib", f] => stem(f, ".py").map(|m| (FileClass::Lib, Some(m))),
        ["contracts", f] => stem(f, ".kvx").map(|m| (FileClass::Contract, Some(m))),
        ["adapters", f] => stem(f, ".kvx").map(|m| (FileClass::Adapter, Some(m))),
        ["adapters", m, "live.py"] if read::is_ident(m) => {
            Some((FileClass::AdapterLive, Some(m.to_string())))
        }
        ["migrations", f] => f
            .strip_suffix(".py")
            .filter(|s| {
                !s.is_empty()
                    && s.chars()
                        .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '_')
            })
            .map(|_| (FileClass::Migration, None)),
        ["fixtures", m, f] if read::is_ident(m) => f
            .strip_suffix(".kvx")
            .filter(|s| read::is_member(s))
            .map(|_| (FileClass::Fixture, Some(m.to_string()))),
        ["manual", f] if MANUAL_FILES.contains(f) => Some((FileClass::Manual, None)),
        ["drift", f] => f
            .strip_suffix(".kvx")
            .filter(|s| {
                !s.is_empty()
                    && s.chars().all(|c| {
                        c.is_ascii_lowercase() || c.is_ascii_digit() || c == '_' || c == '-'
                    })
            })
            .map(|_| (FileClass::Drift, None)),
        _ => None,
    }
}

fn sha256_hex(bytes: &[u8]) -> String {
    let digest = Sha256::digest(bytes);
    digest.iter().map(|b| format!("{b:02x}")).collect()
}

/// Records a fixture error at the `[meta]` entry `key`, or at 1:1 when the entry is absent.
fn meta_error(ctx: &mut Ctx, meta: Option<&kvx::Section>, key: &str, message: String) {
    match meta.and_then(|s| s.get(key)) {
        Some(e) => ctx.at(e, &e.value, ErrorKind::Fixture, message),
        None => ctx.push(1, 1, ErrorKind::Fixture, message),
    }
}

fn defines_migrate(text: &str) -> bool {
    text.lines()
        .any(|l| l.starts_with("def migrate(") || l.starts_with("def migrate ("))
}

impl WorldSource {
    /// Loads and types a World Source directory.
    pub fn load_dir(root: &Path) -> Result<WorldSource, Errors> {
        let tree = SourceTree::read_dir(root)?;
        WorldSource::load_tree(&tree)
    }

    /// Loads and types an in-memory source tree. Returns every parse-stage error found.
    pub fn load_tree(tree: &SourceTree) -> Result<WorldSource, Errors> {
        let mut errors: Vec<Error> = Vec::new();
        let mut inventory: Vec<SourceFile> = Vec::new();
        let mut texts: BTreeMap<String, (FileClass, Option<String>, String)> = BTreeMap::new();
        for (path, bytes) in &tree.files {
            let Some(bytes) = bytes else {
                errors.push(Error::new(path.clone(), 1, 1, ErrorKind::UnknownFile, "not a regular file; symbolic links and special files are not part of the grammar"));
                continue;
            };
            let Some((class, module)) = classify(path) else {
                errors.push(Error::new(
                    path.clone(),
                    1,
                    1,
                    ErrorKind::UnknownFile,
                    "file is not part of the World Source grammar (world-source.md section 2)",
                ));
                continue;
            };
            let text = match String::from_utf8(bytes.clone()) {
                Ok(t) => t,
                Err(e) => {
                    let upto = &bytes[..e.utf8_error().valid_up_to()];
                    let line = upto.iter().filter(|b| **b == b'\n').count() + 1;
                    errors.push(Error::new(
                        path.clone(),
                        line,
                        1,
                        ErrorKind::Io,
                        "file is not valid UTF-8",
                    ));
                    continue;
                }
            };
            let sha256 = (!class.is_kvx()).then(|| sha256_hex(bytes));
            inventory.push(SourceFile {
                path: path.clone(),
                class,
                sha256,
            });
            texts.insert(path.clone(), (class, module, text));
        }
        for required in ["world.kvx", "CHANGELOG.kvx"] {
            if !tree.files.contains_key(required) {
                errors.push(Error::new(
                    required,
                    1,
                    1,
                    ErrorKind::MissingKey,
                    format!("required file {required} is missing"),
                ));
            }
        }
        // Strict KVX syntax for every KVX file.
        let mut docs: BTreeMap<String, kvx::Document> = BTreeMap::new();
        for (path, (class, _, text)) in &texts {
            if class.is_kvx() {
                match kvx::parse(path, text) {
                    Ok(doc) => {
                        docs.insert(path.clone(), doc);
                    }
                    Err(mut e) => errors.append(&mut e),
                }
            }
        }

        // world.kvx
        let world_doc = docs.get("world.kvx");
        let world = world_doc
            .and_then(|doc| world::read_world(&mut Ctx::new("world.kvx", &mut errors), doc));
        let modules: Option<Vec<String>> = world.as_ref().map(|w| w.world.modules.clone());
        let module_declared = |m: &str| modules.as_ref().is_none_or(|ms| ms.iter().any(|x| x == m));

        // contracts and adapters
        let mut contracts = Vec::new();
        let mut adapters = Vec::new();
        for (path, (class, module, _)) in &texts {
            let Some(doc) = docs.get(path) else { continue };
            let stem = module.clone().unwrap_or_default();
            match class {
                FileClass::Contract | FileClass::Adapter => {
                    if !module_declared(&stem) {
                        let loc = doc
                            .section("meta")
                            .and_then(|s| s.get("module"))
                            .map(|e| (e.line, e.value.col))
                            .unwrap_or((1, 1));
                        errors.push(Error::new(
                            path.clone(),
                            loc.0,
                            loc.1,
                            ErrorKind::UndeclaredModule,
                            format!(
                                "module {stem} has a {} file but is not declared in world.modules",
                                if *class == FileClass::Contract {
                                    "contracts/"
                                } else {
                                    "adapters/"
                                }
                            ),
                        ));
                        continue;
                    }
                    let mut ctx = Ctx::new(path, &mut errors);
                    if *class == FileClass::Contract {
                        if let Some(c) = primitive::read_contract(&mut ctx, doc, &stem) {
                            contracts.push(c);
                        }
                    } else if let Some(a) = adapter::read_adapter_file(&mut ctx, doc, &stem) {
                        adapters.push(a);
                    }
                }
                _ => {}
            }
        }
        // lib/, adapters/<m>/live.py and fixtures/<m>/ must name declared modules.
        for (path, (class, module, text)) in &texts {
            let m = module.clone().unwrap_or_default();
            match class {
                FileClass::Lib | FileClass::Fixture if !module_declared(&m) => {
                    errors.push(Error::new(
                        path.clone(),
                        1,
                        1,
                        ErrorKind::UndeclaredModule,
                        format!("module {m} is not declared in world.modules"),
                    ))
                }
                FileClass::AdapterLive if !texts.contains_key(&format!("adapters/{m}.kvx")) => {
                    errors.push(Error::new(
                        path.clone(),
                        1,
                        1,
                        ErrorKind::UnknownFile,
                        format!("adapters/{m}/live.py has no adapter contract adapters/{m}.kvx"),
                    ))
                }
                FileClass::Migration if !defines_migrate(text) => errors.push(Error::new(
                    path.clone(),
                    1,
                    1,
                    ErrorKind::Migration,
                    "a migration must define the pure function migrate(state: dict) -> dict \
                     at top level",
                )),
                _ => {}
            }
        }

        // Duplicate primitive or adapter names across all files (CWR-C002).
        // Primitive contracts are read before adapter contracts, so a clash is reported at the
        // adapter declaration and names the contract location.
        let mut seen: BTreeMap<String, (String, usize)> = BTreeMap::new();
        let mut ordered: Vec<(&String, &kvx::Document, &str)> = Vec::new();
        for (class, prefix) in [
            (FileClass::Contract, "primitive."),
            (FileClass::Adapter, "adapter."),
        ] {
            for (path, doc) in &docs {
                if texts[path].0 == class {
                    ordered.push((path, doc, prefix));
                }
            }
        }
        // Names are complete only when every contract and adapter file passed KVX syntax;
        // otherwise references to unknown names are not judged, to avoid cascading errors.
        let names_complete = texts
            .iter()
            .filter(|(_, (c, _, _))| matches!(c, FileClass::Contract | FileClass::Adapter))
            .all(|(p, _)| docs.contains_key(p));
        for (path, doc, prefix) in ordered {
            for section in &doc.sections {
                if let Some(name) = section.name.strip_prefix(prefix) {
                    if let Some((first_path, first_line)) = seen.get(name) {
                        errors.push(Error::new(
                            path.clone(),
                            section.line,
                            1,
                            ErrorKind::DuplicatePrimitive,
                            format!("{name} is already declared at {first_path}:{first_line}"),
                        ));
                    } else {
                        seen.insert(name.to_string(), (path.clone(), section.line));
                    }
                }
            }
        }

        // CHANGELOG.kvx
        let migration_ok = |m: &str| -> Result<(), MigrationRefError> {
            let Some(name) = m.strip_prefix("migrations/") else {
                return Err(MigrationRefError::NotUnderMigrations(m.to_string()));
            };
            if name.contains('/') || classify(m).map(|c| c.0) != Some(FileClass::Migration) {
                return Err(MigrationRefError::NotAMigrationPath(m.to_string()));
            }
            match texts.get(m) {
                None => Err(MigrationRefError::Missing(m.to_string())),
                Some((_, _, text)) if !defines_migrate(text) => {
                    Err(MigrationRefError::NoMigrate(m.to_string()))
                }
                Some(_) => Ok(()),
            }
        };
        let changelog = docs.get("CHANGELOG.kvx").and_then(|doc| {
            changelog::read_changelog(
                &mut Ctx::new("CHANGELOG.kvx", &mut errors),
                doc,
                &migration_ok,
            )
        });
        if let (Some(w), Some(log), Some(doc)) = (&world, &changelog, world_doc) {
            if let Some(newest) = log.newest() {
                if newest.version != w.world.version {
                    let (line, col) = world::version_location(doc);
                    errors.push(Error::new(
                        "world.kvx",
                        line,
                        col,
                        ErrorKind::Changelog,
                        format!(
                            "world.version {} must equal the newest CHANGELOG.kvx release {}",
                            w.world.version, newest.version
                        ),
                    ));
                }
            }
        }

        // Fixtures and drift need the primitive and adapter names.
        let names: BTreeSet<String> = seen.keys().cloned().collect();
        let mut fixtures = Vec::new();
        let mut drift = Vec::new();
        for (path, (class, module, _)) in &texts {
            let Some(doc) = docs.get(path) else { continue };
            match class {
                FileClass::Fixture => {
                    let m = module.clone().unwrap_or_default();
                    if !module_declared(&m) {
                        continue;
                    }
                    let mut ctx = Ctx::new(path, &mut errors);
                    if let Some(f) = fixture::read_fixture(&mut ctx, doc, &m) {
                        let meta = doc.section("meta");
                        if let Some(p) = &f.primitive {
                            if names_complete && !names.contains(p) {
                                meta_error(
                                    &mut ctx,
                                    meta,
                                    "primitive",
                                    format!(
                                        "fixture names {p}, which is not a primitive or adapter \
                                         of this source"
                                    ),
                                );
                                continue;
                            }
                            if !p.starts_with(&format!("{m}.")) {
                                meta_error(
                                    &mut ctx,
                                    meta,
                                    "primitive",
                                    format!(
                                        "fixture for {p} must live under fixtures/{}/",
                                        p.split('.').next().unwrap_or("")
                                    ),
                                );
                                continue;
                            }
                        }
                        if let Some(mig) = &f.migration {
                            if let Err(problem) = migration_ok(mig) {
                                meta_error(&mut ctx, meta, "migration", problem.to_string());
                                continue;
                            }
                        }
                        fixtures.push(f);
                    }
                }
                FileClass::Drift => {
                    let exists = |n: &str| !names_complete || names.contains(n);
                    if let Some(d) =
                        fixture::read_drift(&mut Ctx::new(path, &mut errors), doc, &exists)
                    {
                        drift.push(d);
                    }
                }
                _ => {}
            }
        }

        if !errors.is_empty() {
            return Err(Errors::sorted(errors));
        }
        let (Some(world), Some(changelog)) = (world, changelog) else {
            // Unreachable: a missing typed part always records an error.
            return Err(Errors(vec![Error::new(
                "world.kvx",
                1,
                1,
                ErrorKind::Io,
                "internal: incomplete model without errors",
            )]));
        };
        let mut migrations: Vec<MigrationFile> = inventory
            .iter()
            .filter(|f| f.class == FileClass::Migration)
            .map(|f| MigrationFile {
                path: f.path.clone(),
                changes: changelog
                    .changes
                    .iter()
                    .filter(|c| c.migration == f.path)
                    .map(|c| c.id.clone())
                    .collect(),
            })
            .collect();
        migrations.sort_by(|a, b| a.path.cmp(&b.path));
        Ok(WorldSource {
            world,
            changelog,
            contracts,
            adapters,
            migrations,
            fixtures,
            drift,
            files: inventory,
        })
    }

    /// Every primitive contract, in file then declared order.
    pub fn primitives(&self) -> impl Iterator<Item = &primitive::Primitive> {
        self.contracts.iter().flat_map(|c| c.primitives.iter())
    }

    /// Every adapter contract, in file then declared order.
    pub fn adapter_contracts(&self) -> impl Iterator<Item = &adapter::Adapter> {
        self.adapters.iter().flat_map(|a| a.adapters.iter())
    }

    /// Whether this source is an illustrative example (`meta.example = true` in world.kvx).
    pub fn is_example(&self) -> bool {
        self.world.example
    }

    /// Checks a model that did not come from KVX (for example one deserialised from JSON)
    /// against every rule the KVX reader applies, by running that reader: each typed file is
    /// rendered in canonical KVX at its own path, the inventory supplies the remaining paths,
    /// and [`WorldSource::load_tree`] reads the result. The model is valid exactly when that
    /// reading succeeds and equals the model. Errors are the reader's own, typed and located
    /// in the canonical rendering of the file they concern, plus located errors for an
    /// inventory or a value the reader would not reproduce.
    ///
    /// Python and Markdown bytes are not part of the model; their inventory entries must carry
    /// a 64-digit lowercase hex SHA-256, and each migration is read as a file that defines
    /// `migrate(state)`.
    pub fn check(&self) -> Result<(), Errors> {
        let mut errors = Vec::new();
        let kvx_files = self.to_kvx_files();
        let mut typed: Vec<(&str, FileClass)> = vec![
            ("world.kvx", FileClass::World),
            ("CHANGELOG.kvx", FileClass::Changelog),
        ];
        typed.extend(
            self.contracts
                .iter()
                .map(|c| (c.path.as_str(), FileClass::Contract)),
        );
        typed.extend(
            self.adapters
                .iter()
                .map(|a| (a.path.as_str(), FileClass::Adapter)),
        );
        typed.extend(
            self.fixtures
                .iter()
                .map(|f| (f.path.as_str(), FileClass::Fixture)),
        );
        typed.extend(
            self.drift
                .iter()
                .map(|d| (d.path.as_str(), FileClass::Drift)),
        );
        for (i, (path, class)) in typed.iter().enumerate() {
            if classify(path).map(|c| c.0) != Some(*class) {
                errors.push(Error::new(
                    *path,
                    1,
                    1,
                    ErrorKind::UnknownFile,
                    format!(
                        "the model holds a {} file at {path:?}, which is not a {} path",
                        class.label(),
                        class.label()
                    ),
                ));
            }
            if typed[..i].iter().any(|(p, _)| p == path) {
                errors.push(Error::new(
                    *path,
                    1,
                    1,
                    ErrorKind::UnknownFile,
                    "the model holds two typed files at this path",
                ));
            }
        }
        let mut tree = SourceTree::default();
        for f in &self.files {
            let digest_ok = match &f.sha256 {
                None => f.class.is_kvx(),
                Some(h) => !f.class.is_kvx() && is_sha256_hex(h),
            };
            if !digest_ok {
                errors.push(Error::new(
                    f.path.clone(),
                    1,
                    1,
                    ErrorKind::BadValue,
                    if f.class.is_kvx() {
                        "a KVX file carries no sha256; its typed model represents it"
                    } else {
                        "sha256 must be the 64 lowercase hex digits of the file's SHA-256"
                    },
                ));
            }
            let bytes = if f.class.is_kvx() {
                match kvx_files.get(&f.path) {
                    Some(text) => text.clone().into_bytes(),
                    None => {
                        errors.push(Error::new(
                            f.path.clone(),
                            1,
                            1,
                            ErrorKind::UnknownFile,
                            format!(
                                "the inventory lists a {} file the model does not hold",
                                f.class.label()
                            ),
                        ));
                        continue;
                    }
                }
            } else if f.class == FileClass::Migration {
                MIGRATION_STAND_IN.as_bytes().to_vec()
            } else {
                Vec::new()
            };
            if tree.files.insert(f.path.clone(), Some(bytes)).is_some() {
                errors.push(Error::new(
                    f.path.clone(),
                    1,
                    1,
                    ErrorKind::UnknownFile,
                    "the inventory lists this path twice",
                ));
            }
        }
        for path in kvx_files.keys() {
            if !self.files.iter().any(|f| f.path == *path) {
                errors.push(Error::new(
                    path.clone(),
                    1,
                    1,
                    ErrorKind::UnknownFile,
                    "the model holds a typed file the inventory does not list",
                ));
            }
        }
        if errors.is_empty() {
            match WorldSource::load_tree(&tree) {
                Err(reader) => errors.extend(reader.0),
                Ok(read) => self.differences(&read, &mut errors),
            }
        }
        if errors.is_empty() {
            Ok(())
        } else {
            Err(Errors::sorted(errors))
        }
    }

    /// Records one located error per top-level item of `self` that differs from `read`, the
    /// reader's view of the same model. File digests are compared by [`WorldSource::check`]
    /// itself, because the stand-in bytes it reads have digests of their own.
    fn differences(&self, read: &WorldSource, errors: &mut Vec<Error>) {
        let (Ok(mut ours), Ok(mut theirs)) =
            (serde_json::to_value(self), serde_json::to_value(read))
        else {
            errors.push(Error::new(
                "world.kvx",
                1,
                1,
                ErrorKind::Io,
                "the model cannot be serialised for comparison",
            ));
            return;
        };
        for value in [&mut ours, &mut theirs] {
            if let Some(serde_json::Value::Array(files)) = value.get_mut("files") {
                for f in files {
                    if let Some(obj) = f.as_object_mut() {
                        obj.remove("sha256");
                    }
                }
            }
        }
        let (serde_json::Value::Object(ours), serde_json::Value::Object(theirs)) = (ours, theirs)
        else {
            return;
        };
        for (key, mine) in &ours {
            let other = theirs.get(key).unwrap_or(&serde_json::Value::Null);
            match (mine, other) {
                (serde_json::Value::Array(a), serde_json::Value::Array(b)) => {
                    for i in 0..a.len().max(b.len()) {
                        let (x, y) = (a.get(i), b.get(i));
                        if x == y {
                            continue;
                        }
                        let file = x
                            .or(y)
                            .and_then(|v| v.get("path"))
                            .and_then(serde_json::Value::as_str)
                            .unwrap_or(model_file(key));
                        let pointer = format!("/{key}/{i}");
                        errors.push(Error::new(
                            file,
                            1,
                            1,
                            difference_kind(key),
                            describe_difference(&pointer, x, y),
                        ));
                    }
                }
                _ if mine != other => errors.push(Error::new(
                    model_file(key),
                    1,
                    1,
                    difference_kind(key),
                    describe_difference(&format!("/{key}"), Some(mine), Some(other)),
                )),
                _ => {}
            }
        }
    }

    /// Renders every KVX file of the model in canonical form (comments are not part of the
    /// model). Non-KVX files are not rendered; their bytes are inventoried by digest.
    pub fn to_kvx_files(&self) -> BTreeMap<String, String> {
        let mut out = BTreeMap::new();
        out.insert("world.kvx".to_string(), world::emit(&self.world));
        out.insert(
            "CHANGELOG.kvx".to_string(),
            changelog::emit(&self.changelog),
        );
        for c in &self.contracts {
            out.insert(c.path.clone(), primitive::emit(c));
        }
        for a in &self.adapters {
            out.insert(a.path.clone(), adapter::emit(a));
        }
        for f in &self.fixtures {
            out.insert(f.path.clone(), fixture::emit_fixture(f));
        }
        for d in &self.drift {
            out.insert(d.path.clone(), fixture::emit_drift(d));
        }
        out
    }

    /// Canonical JSON of the typed model (two-space indentation, declared order, final
    /// newline). The output is deterministic for a given model; the error case is reserved for
    /// a serializer failure and does not occur for models built by this crate.
    pub fn to_json(&self) -> Result<String, serde_json::Error> {
        let mut s = serde_json::to_string_pretty(self)?;
        s.push('\n');
        Ok(s)
    }

    /// Parses a model from its JSON form and accepts it exactly when the KVX reader would
    /// accept its canonical rendering and read back the same model ([`WorldSource::check`]).
    /// A JSON syntax or type error is located at its line and column in `text`, under the
    /// file name `<json>`.
    pub fn from_json(text: &str) -> Result<WorldSource, Errors> {
        let model: WorldSource = serde_json::from_str(text).map_err(|e| {
            Errors(vec![Error::new(
                "<json>",
                e.line().max(1),
                e.column().max(1),
                ErrorKind::Json,
                e.to_string(),
            )])
        })?;
        model.check()?;
        Ok(model)
    }
}

/// The text [`WorldSource::check`] reads for a migration file, whose bytes the model does not
/// carry: the smallest file that defines `migrate(state)`.
const MIGRATION_STAND_IN: &str = "def migrate(state):\n    return state\n";

fn is_sha256_hex(s: &str) -> bool {
    s.len() == 64
        && s.chars()
            .all(|c| c.is_ascii_digit() || ('a'..='f').contains(&c))
}

/// The file a top-level model key that is not a list of files is rendered to.
fn model_file(key: &str) -> &'static str {
    match key {
        "changelog" | "migrations" => "CHANGELOG.kvx",
        _ => "world.kvx",
    }
}

fn difference_kind(key: &str) -> ErrorKind {
    match key {
        "changelog" | "migrations" => ErrorKind::Changelog,
        "files" => ErrorKind::UnknownFile,
        _ => ErrorKind::BadValue,
    }
}

/// Describes where two JSON values first differ, with both sides shortened to 120 characters.
fn describe_difference(
    pointer: &str,
    ours: Option<&serde_json::Value>,
    theirs: Option<&serde_json::Value>,
) -> String {
    let (at, a, b) = first_difference(pointer.to_string(), ours, theirs);
    let show = |v: Option<&serde_json::Value>| -> String {
        let text = v.map_or_else(|| "nothing".to_string(), serde_json::Value::to_string);
        if text.chars().count() > 120 {
            format!("{}...", text.chars().take(120).collect::<String>())
        } else {
            text
        }
    };
    format!(
        "the KVX reader does not reproduce the model at {at}: the model has {}, the reader reads {}",
        show(a),
        show(b)
    )
}

/// The JSON pointer of the first difference between two values and the values there. The
/// walk is iterative, so no input depth can exhaust the stack.
fn first_difference<'a>(
    mut at: String,
    mut ours: Option<&'a serde_json::Value>,
    mut theirs: Option<&'a serde_json::Value>,
) -> (
    String,
    Option<&'a serde_json::Value>,
    Option<&'a serde_json::Value>,
) {
    use serde_json::Value;
    loop {
        let next = match (ours, theirs) {
            (Some(Value::Object(a)), Some(Value::Object(b))) => a
                .iter()
                .map(|(k, v)| (k.clone(), Some(v), b.get(k)))
                .chain(
                    b.iter()
                        .filter(|(k, _)| !a.contains_key(*k))
                        .map(|(k, v)| (k.clone(), None, Some(v))),
                )
                .find(|(_, x, y)| x != y),
            (Some(Value::Array(a)), Some(Value::Array(b))) if a.len() == b.len() => a
                .iter()
                .zip(b)
                .enumerate()
                .find(|(_, (x, y))| x != y)
                .map(|(i, (x, y))| (i.to_string(), Some(x), Some(y))),
            _ => None,
        };
        match next {
            Some((step, x, y)) => {
                at = format!("{at}/{step}");
                ours = x;
                theirs = y;
            }
            None => return (at, ours, theirs),
        }
    }
}

/// Parses one KVX file of class `class` on its own: the path must classify as that class,
/// the text must be strict KVX, and `read` (the class's reader, given the module or file
/// stem) must accept it without recording an error.
pub(crate) fn parse_file<T>(
    path: &str,
    text: &str,
    class: FileClass,
    read: impl FnOnce(&mut Ctx, &kvx::Document, &str) -> Option<T>,
) -> Result<T, Errors> {
    let stem = match classify(path) {
        Some((c, module)) if c == class => module.unwrap_or_default(),
        _ => {
            return Err(Errors(vec![Error::new(
                path,
                1,
                1,
                ErrorKind::UnknownFile,
                format!("{path:?} is not a {} path", class.label()),
            )]))
        }
    };
    let doc = kvx::parse(path, text).map_err(Errors::sorted)?;
    let mut errors = Vec::new();
    let value = read(&mut Ctx::new(path, &mut errors), &doc, &stem);
    match value {
        Some(v) if errors.is_empty() => Ok(v),
        _ if errors.is_empty() => Err(Errors(vec![Error::new(
            path,
            1,
            1,
            ErrorKind::Io,
            "the reader produced no model and no error",
        )])),
        _ => Err(Errors::sorted(errors)),
    }
}
