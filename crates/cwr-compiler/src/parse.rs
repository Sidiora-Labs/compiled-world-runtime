// Copyright Sidiora Labs Inc. CWR® 2026-2027
// SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0

//! Parse stage (compiler.md section 2): the World Source directory to typed sections and a
//! file inventory.
//!
//! The strict reader and the typed model are the `cwr-contract` crate (task 0.3). This stage
//! runs it, keeps the raw KVX documents for the later stages, and sorts the reader's errors
//! by pipeline stage: syntax, unknown file, section or key (CWR-C001), duplicate section or
//! key, missing key, bad value, shape (CWR-C001), changelog and fixture consistency stop the
//! run here; errors the reader reports under a resolve or lint code (C002, C003, V002, F001,
//! F002, D001, M001) are deferred to their stage, where the compiler's own checks run on
//! the complete tree and these are merged in. A source with `meta.example = true` is refused
//! here unless `--allow-example` is given.
//!
//! The layout itself is checked here as well (clause 3.1, world-source.md sections 2, 9 and
//! 10): `lib/`, `contracts/`, `fixtures/` and `manual/` must be present, `manual/` must hold
//! the four prose files `intro.md`, `conventions.md`, `types.md` and `examples.md`, and every
//! module `world.kvx` declares must have at least one fixture under `fixtures/<module>/`.
//! `adapters/` and `migrations/` hold sets that are legitimately empty (a world without
//! adapters, a release without a state-shape change); an absent directory is that empty set,
//! and every reference into them (an adapter contract's `live.py`, a changelog migration) is
//! checked by the reader and by resolve.

use std::collections::{BTreeMap, BTreeSet};
use std::path::Path;

use cwr_contract::kvx::{self, Document, Json};
use cwr_contract::source::{classify, SourceTree};
use cwr_contract::{ErrorKind, FileClass, WorldSource};

use crate::{Diagnostic, Location, Stage};

/// One classified, UTF-8 source file.
#[derive(Debug, Clone)]
pub struct File {
    /// The class [`classify`] assigns to the path.
    pub class: FileClass,
    /// The module the path names (`lib/<m>.py`, `contracts/<m>.kvx`, ...), if any.
    pub module: Option<String>,
    /// The file's contents, valid UTF-8.
    pub text: String,
}

/// The parse stage's output.
#[derive(Debug, Clone)]
pub struct Parsed {
    /// Every classified file, by source-relative path.
    pub files: BTreeMap<String, File>,
    /// Every KVX file, parsed.
    pub docs: BTreeMap<String, Document>,
    /// The typed model, when the contract reader accepted the whole source.
    pub model: Option<WorldSource>,
    /// Reader errors that belong to a later stage.
    pub deferred: Vec<Diagnostic>,
}

impl Parsed {
    /// Files of one class, in path order.
    pub fn of_class(&self, class: FileClass) -> impl Iterator<Item = (&String, &File)> {
        self.files.iter().filter(move |(_, f)| f.class == class)
    }

    /// The typed model, or a located `internal` diagnostic for `stage` when the contract
    /// reader rejected the source (a later stage invoked although parse reported errors).
    pub fn require_model(&self, stage: Stage) -> Result<&WorldSource, Vec<Diagnostic>> {
        self.model.as_ref().ok_or_else(|| {
            vec![Diagnostic::new(
                stage,
                None,
                "internal",
                locate(self.docs.get("world.kvx"), "world.kvx", 1, 1),
                format!("the {stage} stage needs the typed model, which the parse stage did not produce"),
            )]
        })
    }

    /// Section and key enclosing a line of a KVX file.
    pub fn locate(&self, file: &str, line: usize, col: usize) -> Location {
        locate(self.docs.get(file), file, line, col)
    }
}

pub(crate) fn locate(doc: Option<&Document>, file: &str, line: usize, col: usize) -> Location {
    let mut loc = Location {
        file: file.to_string(),
        line,
        col,
        section: None,
        key: None,
    };
    if let Some(doc) = doc {
        if let Some(section) = doc
            .sections
            .iter()
            .filter(|s| s.line <= line)
            .max_by_key(|s| s.line)
        {
            loc.section = Some(section.name.clone());
            loc.key = section
                .entries
                .iter()
                .find(|e| e.line == line)
                .map(|e| e.key.clone());
        }
    }
    loc
}

/// The stage a reader error kind belongs to.
pub fn stage_of(kind: ErrorKind) -> Stage {
    match kind {
        ErrorKind::DuplicatePrimitive | ErrorKind::UndeclaredModule | ErrorKind::Expression => {
            Stage::Resolve
        }
        ErrorKind::Envelope | ErrorKind::Adapter | ErrorKind::Doc | ErrorKind::Migration => {
            Stage::Lint
        }
        _ => Stage::Parse,
    }
}

fn from_reader(e: &cwr_contract::Error, docs: &BTreeMap<String, Document>) -> Diagnostic {
    let loc = locate(docs.get(&e.file), &e.file, e.line, e.col);
    Diagnostic::new(
        stage_of(e.kind),
        e.code,
        e.kind.label(),
        loc,
        e.message.clone(),
    )
}

/// The prose files `manual/` must hold (world-source.md section 10), in manual order.
pub const MANUAL_PROSE: [&str; 4] = ["intro.md", "conventions.md", "types.md", "examples.md"];

/// The required-layout check (clause 3.1): one `layout` diagnostic per missing part, located
/// at the missing path (or at the `world.kvx` modules entry for a module without fixtures).
pub fn layout(
    files: &BTreeMap<String, File>,
    docs: &BTreeMap<String, Document>,
) -> Vec<Diagnostic> {
    let at = |file: &str| Location {
        file: file.to_string(),
        line: 1,
        col: 1,
        section: None,
        key: None,
    };
    let mut out = Vec::new();
    let has_class = |class: FileClass| files.values().any(|f| f.class == class);
    for (dir, class, what) in [
        (
            "lib/",
            FileClass::Lib,
            "the kernel-side implementation lib/<module>.py",
        ),
        (
            "contracts/",
            FileClass::Contract,
            "the rule contracts contracts/<module>.kvx",
        ),
        (
            "fixtures/",
            FileClass::Fixture,
            "the contract-test fixtures fixtures/<module>/*.kvx",
        ),
        (
            "manual/",
            FileClass::Manual,
            "the hand-written prose manual/*.md",
        ),
    ] {
        if !has_class(class) {
            out.push(Diagnostic::new(
                Stage::Parse,
                None,
                "layout",
                at(dir),
                format!("required directory {dir} is missing or empty: a World Source consists of world.kvx, CHANGELOG.kvx, lib/, contracts/, adapters/, migrations/, fixtures/ and manual/, and {dir} holds {what} (world-source.md section 2)"),
            ));
        }
    }
    if has_class(FileClass::Manual) {
        for name in MANUAL_PROSE {
            let path = format!("manual/{name}");
            if !files.contains_key(&path) {
                out.push(Diagnostic::new(
                    Stage::Parse,
                    None,
                    "layout",
                    at(&path),
                    format!("required prose file {path} is missing: manual/ must hold intro.md, conventions.md, types.md and examples.md (world-source.md section 10)"),
                ));
            }
        }
    }
    // Every declared module needs fixtures/<module>/ (world-source.md section 9).
    if has_class(FileClass::Fixture) {
        if let Some(entry) = docs
            .get("world.kvx")
            .and_then(|d| d.section("world"))
            .and_then(|s| s.get("modules"))
        {
            let with_fixture: BTreeSet<&str> = files
                .values()
                .filter(|f| f.class == FileClass::Fixture)
                .filter_map(|f| f.module.as_deref())
                .collect();
            if let Json::Array(items) = &entry.value.value {
                for m in items.iter().filter_map(|v| match &v.value {
                    Json::Str(s) => Some(s.as_str()),
                    _ => None,
                }) {
                    if !with_fixture.contains(m) {
                        out.push(Diagnostic::new(
                            Stage::Parse,
                            None,
                            "layout",
                            Location {
                                file: "world.kvx".into(),
                                line: entry.line,
                                col: entry.value.col,
                                section: Some("world".into()),
                                key: Some("modules".into()),
                            },
                            format!("module {m} has no fixtures: fixtures/{m}/ must hold the initial states that reach every declared failure of the module (world-source.md section 9)"),
                        ));
                    }
                }
            }
        }
    }
    out
}

/// Runs the parse stage.
pub fn parse(root: &Path, allow_example: bool) -> Result<Parsed, Vec<Diagnostic>> {
    let tree = match SourceTree::read_dir(root) {
        Ok(t) => t,
        Err(errors) => {
            let none = BTreeMap::new();
            return Err(errors.0.iter().map(|e| from_reader(e, &none)).collect());
        }
    };
    let mut files = BTreeMap::new();
    let mut docs = BTreeMap::new();
    for (path, bytes) in &tree.files {
        let (Some(bytes), Some((class, module))) = (bytes, classify(path)) else {
            continue;
        };
        let Ok(text) = String::from_utf8(bytes.clone()) else {
            continue;
        };
        if class.is_kvx() {
            if let Ok(doc) = kvx::parse(path, &text) {
                docs.insert(path.clone(), doc);
            }
        }
        files.insert(
            path.clone(),
            File {
                class,
                module,
                text,
            },
        );
    }
    let (model, reader_errors) = match WorldSource::load_tree(&tree) {
        Ok(m) => (Some(m), Vec::new()),
        Err(errors) => (None, errors.0),
    };
    let mut now = Vec::new();
    let mut deferred = Vec::new();
    for e in &reader_errors {
        let d = from_reader(e, &docs);
        if d.stage == Stage::Parse {
            now.push(d);
        } else {
            deferred.push(d);
        }
    }
    // meta.example = true is refused without --allow-example (world-source.md section 3).
    if let Some(entry) = docs
        .get("world.kvx")
        .and_then(|d| d.section("meta"))
        .and_then(|s| s.get("example"))
    {
        if entry.value.value == Json::Bool(true) && !allow_example {
            let loc = Location {
                file: "world.kvx".into(),
                line: entry.line,
                col: entry.value.col,
                section: Some("meta".into()),
                key: Some("example".into()),
            };
            now.push(Diagnostic::new(
                Stage::Parse,
                None,
                "example",
                loc,
                "world.kvx marks this source as an illustrative example (meta.example = true); pass --allow-example to compile it",
            ));
        }
    }
    now.extend(layout(&files, &docs));
    if !now.is_empty() {
        return Err(now);
    }
    Ok(Parsed {
        files,
        docs,
        model,
        deferred,
    })
}
