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
use crate::changelog::{self, Changelog};
use crate::error::{Error, ErrorKind, Errors};
use crate::fixture::{self, DriftFile, FixtureFile};
use crate::kvx;
use crate::primitive::{self, ContractFile};
use crate::read::{self, Ctx};
use crate::types::SideEffect;
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
    let mut names: Vec<_> = entries.filter_map(|e| e.ok()).collect();
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
        let migration_ok = |m: &str| -> Result<(), String> {
            let Some(name) = m.strip_prefix("migrations/") else {
                return Err(format!("migration {m:?} must be a path under migrations/"));
            };
            if name.contains('/') || classify(m).map(|c| c.0) != Some(FileClass::Migration) {
                return Err(format!("migration {m:?} must be migrations/<name>.py"));
            }
            match texts.get(m) {
                None => Err(format!("migration file {m} does not exist")),
                Some((_, _, text)) if !defines_migrate(text) => {
                    Err(format!("migration file {m} does not define migrate(state)"))
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
                            if let Err(message) = migration_ok(mig) {
                                meta_error(&mut ctx, meta, "migration", message);
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

    /// Re-checks every rule that the typed model alone cannot carry, for a model that did
    /// not come from KVX (for example one deserialised from JSON). Returns messages.
    pub fn check(&self) -> Vec<String> {
        let mut out = Vec::new();
        let modules = &self.world.world.modules;
        if !read::is_ident(&self.world.world.name) {
            out.push(format!(
                "world.name {:?} must be a lowercase identifier",
                self.world.world.name
            ));
        }
        if modules.is_empty() {
            out.push("world.modules must list at least one module".to_string());
        }
        for (i, m) in modules.iter().enumerate() {
            if !read::is_ident(m) || modules[..i].contains(m) {
                out.push(format!("world.modules entry {m:?} is invalid or repeated"));
            }
        }
        let mut names: Vec<&str> = Vec::new();
        for c in &self.contracts {
            if !modules.contains(&c.module) {
                out.push(format!(
                    "CWR-C003: {} declares module {} absent from world.modules",
                    c.path, c.module
                ));
            }
            if c.path != format!("contracts/{}.kvx", c.module) {
                out.push(format!("{} must be contracts/{}.kvx", c.path, c.module));
            }
            for p in &c.primitives {
                if names.contains(&p.name.as_str()) {
                    out.push(format!("CWR-C002: {} is declared more than once", p.name));
                }
                names.push(&p.name);
                if !p.name.starts_with(&format!("{}.", c.module)) {
                    out.push(format!("{} must belong to module {}", p.name, c.module));
                }
                match p.side_effect {
                    SideEffect::External => out.push(format!(
                        "CWR-C001: {} side_effect external is reserved for adapters",
                        p.name
                    )),
                    SideEffect::ReadOnly if !p.effects.is_empty() => {
                        out.push(format!("CWR-C001: read_only {} declares effects", p.name))
                    }
                    SideEffect::Mutating if p.effects.is_empty() => {
                        out.push(format!("CWR-C001: mutating {} declares no effects", p.name))
                    }
                    _ => {}
                }
                if p.doc.trim().is_empty() {
                    out.push(format!("CWR-D001: {} has an empty doc", p.name));
                }
            }
        }
        for f in &self.adapters {
            if !modules.contains(&f.module) {
                out.push(format!(
                    "CWR-C003: {} declares module {} absent from world.modules",
                    f.path, f.module
                ));
            }
            if f.path != format!("adapters/{}.kvx", f.module) {
                out.push(format!("{} must be adapters/{}.kvx", f.path, f.module));
            }
            for a in &f.adapters {
                if names.contains(&a.name.as_str()) {
                    out.push(format!("CWR-C002: {} is declared more than once", a.name));
                }
                names.push(&a.name);
                if !a.name.starts_with(&format!("{}.", f.module)) {
                    out.push(format!("{} must belong to module {}", a.name, f.module));
                }
                out.extend(adapter::adapter_shape_errors(a));
                if a.doc.trim().is_empty() {
                    out.push(format!("CWR-D001: {} has an empty doc", a.name));
                }
            }
        }
        let migration_paths: Vec<&str> = self.migrations.iter().map(|m| m.path.as_str()).collect();
        out.extend(
            self.changelog
                .consistency_errors(&|m| migration_paths.contains(&m)),
        );
        match self.changelog.newest() {
            None => out.push("CHANGELOG.kvx declares no release".to_string()),
            Some(r) if r.version != self.world.world.version => out.push(format!(
                "world.version {} must equal the newest release {}",
                self.world.world.version, r.version
            )),
            _ => {}
        }
        for d in &self.drift {
            for e in &d.entries {
                if !names.contains(&e.primitive.as_str()) {
                    out.push(format!(
                        "{}: drift names unknown primitive {}",
                        d.path, e.primitive
                    ));
                }
                if !e.change.drift_applicable() {
                    out.push(format!(
                        "{}: drift change {} cannot be applied to a running table",
                        d.path,
                        e.change.as_str()
                    ));
                }
            }
        }
        out
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

    /// Parses a model from its JSON form and re-checks the rules the types cannot carry.
    pub fn from_json(text: &str) -> Result<WorldSource, String> {
        let model: WorldSource = serde_json::from_str(text).map_err(|e| e.to_string())?;
        let problems = model.check();
        if problems.is_empty() {
            Ok(model)
        } else {
            Err(problems.join("\n"))
        }
    }
}
