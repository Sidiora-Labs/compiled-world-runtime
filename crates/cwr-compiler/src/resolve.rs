// Copyright Sidiora Labs Inc. CWR® 2026-2027
// SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0

//! Resolve stage (compiler.md section 2): typed sections plus `lib/`, `adapters/*/live.py`
//! and `migrations/` Python to a symbol table of every primitive, adapter, module, Python
//! import and manual prose file.
//!
//! Stops on (all reported together, sorted):
//!
//! * CWR-C003: a `contracts/`, `adapters/`, `lib/` or `fixtures/` path for a module absent
//!   from `world.modules`;
//! * CWR-C002: a primitive or adapter name declared more than once, located at the later
//!   declaration and naming the first;
//! * CWR-L002: a contract without a `lib/` function, a public `lib/` function without a
//!   contract, or a parameter list that differs (the first `lib/` parameter is the kernel
//!   state handle; a contract name that is a Python keyword is implemented with a trailing
//!   underscore, `kv.del` as `del_`);
//! * CWR-V002: a precondition (of a primitive, an adapter or a drift entry) outside the
//!   expression language, naming something that is not a parameter, or calling something
//!   that is not a primitive or adapter of this Build (including one this release removes).
//!
//! The symbol table is built from the raw KVX documents, so it is complete even when the
//! contract reader rejected a section for a lint-stage reason.

use std::collections::{BTreeMap, BTreeSet};

use cwr_contract::kvx::{Document, Json, Node, Section};
use cwr_contract::{FileClass, Version};

use crate::parse::Parsed;
use crate::pyimports::{self, Def, Import, Policy};
use crate::{expr, Diagnostic, Location, Stage};

/// Whether a symbol is a primitive contract or an adapter contract.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SymbolKind {
    /// Declared in a `contracts/<module>.kvx` file.
    Primitive,
    /// Declared in an `adapters/<module>.kvx` file.
    Adapter,
}

/// One declared primitive or adapter (its first declaration).
#[derive(Debug, Clone)]
pub struct Symbol {
    /// The primitive or adapter name (its KVX section name).
    pub name: String,
    /// The module whose contract file declares it.
    pub module: String,
    /// Primitive or adapter.
    pub kind: SymbolKind,
    /// The declared `side_effect` string.
    pub side_effect: String,
    /// Declared parameter names, in order.
    pub params: Vec<String>,
    /// Source-relative path of the declaring file.
    pub file: String,
    /// The declaring KVX section.
    pub section: String,
    /// One-based line of the section header.
    pub line: usize,
}

/// One Python file and its imports.
#[derive(Debug, Clone)]
pub struct PyFile {
    /// Source-relative path.
    pub path: String,
    /// The import policy its class falls under.
    pub policy: Policy,
    /// Every import it makes, in source order.
    pub imports: Vec<Import>,
}

/// One precondition expression as written, with its location.
#[derive(Debug, Clone)]
pub struct PreRef {
    /// Primitive or adapter the expression belongs to.
    pub owner: String,
    /// Parameter names of the owner, the only bare names the expression may use.
    pub params: Vec<String>,
    /// Source-relative path of the declaring file.
    pub file: String,
    /// The owner's KVX section.
    pub section: String,
    /// The KVX key holding the expression (`preconditions` or the drift key).
    pub key: String,
    /// One-based line of the entry.
    pub line: usize,
    /// One-based column of the expression literal.
    pub col: usize,
    /// One-based precondition index (the drift entry number for drift expressions).
    pub index: usize,
    /// The expression text.
    pub expr: String,
    /// The failure code as written and its column, when present and a string.
    pub code: Option<(String, usize)>,
}

/// The resolve stage's output.
#[derive(Debug, Clone)]
pub struct SymbolTable {
    /// Modules `world.kvx` declares, in declaration order.
    pub modules: Vec<String>,
    /// First declaration of every name.
    pub symbols: BTreeMap<String, Symbol>,
    /// Every precondition and drift expression.
    pub preconditions: Vec<PreRef>,
    /// Every Python file subject to an import policy, in path order.
    pub python: Vec<PyFile>,
    /// Public top-level functions of each `lib/<module>.py`.
    pub lib: BTreeMap<String, Vec<Def>>,
    /// Primitive names the newest release removes.
    pub removed: BTreeSet<String>,
}

impl SymbolTable {
    /// The declared side effect of `name`, or `None` when no primitive or adapter has it.
    pub fn side_effect_of(&self, name: &str) -> Option<String> {
        self.symbols.get(name).map(|s| s.side_effect.clone())
    }
}

const PY_KEYWORDS: [&str; 35] = [
    "False", "None", "True", "and", "as", "assert", "async", "await", "break", "class", "continue",
    "def", "del", "elif", "else", "except", "finally", "for", "from", "global", "if", "import",
    "in", "is", "lambda", "nonlocal", "not", "or", "pass", "raise", "return", "try", "while",
    "with", "yield",
];

/// The `lib/` function name that implements a contract member name.
pub fn python_name(member: &str) -> String {
    if PY_KEYWORDS.contains(&member) {
        format!("{member}_")
    } else {
        member.to_string()
    }
}

/// The contract member name a `lib/` function name implements.
pub fn member_name(function: &str) -> String {
    match function.strip_suffix('_') {
        Some(stem) if PY_KEYWORDS.contains(&stem) => stem.to_string(),
        _ => function.to_string(),
    }
}

pub(crate) fn str_of(node: &Node) -> Option<&str> {
    match &node.value {
        Json::Str(s) => Some(s),
        _ => None,
    }
}

fn params_of(section: &Section) -> Vec<String> {
    match section.get("params").map(|e| &e.value.value) {
        Some(Json::Object(members)) => members.iter().map(|(k, _)| k.clone()).collect(),
        _ => Vec::new(),
    }
}

fn loc(file: &str, line: usize, col: usize, section: Option<&str>, key: Option<&str>) -> Location {
    Location {
        file: file.to_string(),
        line,
        col,
        section: section.map(str::to_string),
        key: key.map(str::to_string),
    }
}

fn declared_modules(parsed: &Parsed) -> Vec<String> {
    let entry = parsed
        .docs
        .get("world.kvx")
        .and_then(|d| d.section("world"))
        .and_then(|s| s.get("modules"));
    match entry.map(|e| &e.value.value) {
        Some(Json::Array(items)) => items
            .iter()
            .filter_map(|n| str_of(n).map(str::to_string))
            .collect(),
        _ => Vec::new(),
    }
}

fn removed_by_newest(doc: Option<&Document>) -> BTreeSet<String> {
    let mut out = BTreeSet::new();
    let Some(doc) = doc else { return out };
    let newest = doc
        .sections
        .iter()
        .filter_map(|s| {
            s.name
                .strip_prefix("release.")
                .and_then(|v| v.parse::<Version>().ok())
                .map(|v| (v, s))
        })
        .max_by_key(|(v, _)| *v);
    let Some((_, release)) = newest else {
        return out;
    };
    let ids: Vec<&str> = match release.get("changes").map(|e| &e.value.value) {
        Some(Json::Array(items)) => items.iter().filter_map(str_of).collect(),
        _ => Vec::new(),
    };
    for id in ids {
        if let Some(change) = doc.section(&format!("change.{id}")) {
            let kind = change.get("kind").and_then(|e| str_of(&e.value));
            if kind == Some("primitive_removed") {
                if let Some(target) = change.get("target").and_then(|e| str_of(&e.value)) {
                    out.insert(target.to_string());
                }
            }
        }
    }
    out
}

/// Runs the resolve stage.
pub fn resolve(parsed: &Parsed) -> Result<SymbolTable, Vec<Diagnostic>> {
    let mut diags: Vec<Diagnostic> = Vec::new();
    let modules = declared_modules(parsed);
    let declared = |m: &str| modules.iter().any(|x| x == m);

    // Contract and adapter files: modules, then symbols in file-class then path order.
    let mut symbols: BTreeMap<String, Symbol> = BTreeMap::new();
    let mut sections: Vec<(String, String, SymbolKind, &Section)> = Vec::new();
    for (class, prefix, kind) in [
        (FileClass::Contract, "primitive.", SymbolKind::Primitive),
        (FileClass::Adapter, "adapter.", SymbolKind::Adapter),
    ] {
        for (path, file) in parsed.of_class(class) {
            let module = file.module.clone().unwrap_or_default();
            let Some(doc) = parsed.docs.get(path) else {
                continue;
            };
            if !declared(&module) {
                let (line, col) = doc
                    .section("meta")
                    .and_then(|s| s.get("module"))
                    .map(|e| (e.line, e.value.col))
                    .unwrap_or((1, 1));
                let what = if class == FileClass::Contract {
                    "contracts/"
                } else {
                    "adapters/"
                };
                diags.push(Diagnostic::new(
                    Stage::Resolve,
                    Some("CWR-C003"),
                    "undeclared_module",
                    parsed.locate(path, line, col),
                    format!("{path} is a {what} file for module {module}, which world.modules does not declare"),
                ));
                continue;
            }
            for section in &doc.sections {
                let Some(name) = section.name.strip_prefix(prefix) else {
                    continue;
                };
                sections.push((path.clone(), module.clone(), kind, section));
                if let Some(first) = symbols.get(name) {
                    diags.push(
                        Diagnostic::new(
                            Stage::Resolve,
                            Some("CWR-C002"),
                            "duplicate_primitive",
                            loc(path, section.line, 1, Some(&section.name), None),
                            format!(
                                "{name} is declared more than once: here and at {}:{} [{}]",
                                first.file, first.line, first.section
                            ),
                        )
                        .with_primitive(name)
                        .with_related(loc(
                            &first.file,
                            first.line,
                            1,
                            Some(&first.section),
                            None,
                        )),
                    );
                    continue;
                }
                let side_effect = section
                    .get("side_effect")
                    .and_then(|e| str_of(&e.value))
                    .unwrap_or("")
                    .to_string();
                symbols.insert(
                    name.to_string(),
                    Symbol {
                        name: name.to_string(),
                        module: module.clone(),
                        kind,
                        side_effect,
                        params: params_of(section),
                        file: path.clone(),
                        section: section.name.clone(),
                        line: section.line,
                    },
                );
            }
        }
    }

    // lib/ and fixtures/ paths must name declared modules.
    for (path, file) in &parsed.files {
        if matches!(file.class, FileClass::Lib | FileClass::Fixture) {
            let m = file.module.clone().unwrap_or_default();
            if !declared(&m) {
                diags.push(Diagnostic::new(
                    Stage::Resolve,
                    Some("CWR-C003"),
                    "undeclared_module",
                    loc(path, 1, 1, None, None),
                    format!("{path} belongs to module {m}, which world.modules does not declare"),
                ));
            }
        }
    }

    // Python: imports for every file, public functions for lib/.
    let mut python = Vec::new();
    let mut lib: BTreeMap<String, Vec<Def>> = BTreeMap::new();
    for (path, file) in &parsed.files {
        let policy = match file.class {
            FileClass::Lib => Policy::Lib,
            FileClass::AdapterLive => Policy::AdapterLive,
            FileClass::Migration => Policy::Migration,
            _ => continue,
        };
        python.push(PyFile {
            path: path.clone(),
            policy,
            imports: pyimports::scan(&file.text),
        });
        if policy == Policy::Lib {
            let m = file.module.clone().unwrap_or_default();
            let mut defs: Vec<Def> = Vec::new();
            for d in pyimports::top_level_defs(&file.text) {
                if d.name.starts_with('_') {
                    continue;
                }
                // A later definition of the same name replaces the earlier one, as in Python.
                defs.retain(|x| x.name != d.name);
                defs.push(d);
            }
            lib.insert(m, defs);
        }
    }

    // CWR-L002: contracts against lib/ functions, both directions.
    for (path, module, kind, section) in &sections {
        if *kind != SymbolKind::Primitive {
            continue;
        }
        let name = section.name.strip_prefix("primitive.").unwrap_or_default();
        let Some(member) = name.strip_prefix(&format!("{module}.")) else {
            continue;
        };
        let lib_path = format!("lib/{module}.py");
        let fname = python_name(member);
        let contract_loc = loc(path, section.line, 1, Some(&section.name), None);
        let Some(def) = lib
            .get(module)
            .and_then(|defs| defs.iter().find(|d| d.name == fname))
        else {
            let why = if parsed.files.contains_key(&lib_path) {
                format!("{lib_path} defines no public function {fname}")
            } else {
                format!("{lib_path} does not exist")
            };
            diags.push(
                Diagnostic::new(
                    Stage::Resolve,
                    Some("CWR-L002"),
                    "implementation",
                    contract_loc,
                    format!("contract {name} has no lib/ implementation: {why}"),
                )
                .with_primitive(name),
            );
            continue;
        };
        let want = params_of(section);
        let def_loc = loc(&lib_path, def.line, def.col, None, None);
        let mismatch = match &def.params {
            None => Some(format!(
                "{lib_path}:{} def {}({}) uses a parameter form other than plain names",
                def.line, def.name, def.signature
            )),
            Some(ps) if ps.is_empty() => Some(format!(
                "{lib_path}:{} def {}() does not take the kernel state handle",
                def.line, def.name
            )),
            Some(ps) if ps[1..] != want[..] => Some(format!(
                "parameters differ: contract ({}) but {lib_path}:{} def {}(<state>, {})",
                want.join(", "),
                def.line,
                def.name,
                ps[1..].join(", ")
            )),
            _ => None,
        };
        if let Some(m) = mismatch {
            diags.push(
                Diagnostic::new(
                    Stage::Resolve,
                    Some("CWR-L002"),
                    "implementation",
                    contract_loc,
                    format!("contract {name}: {m}"),
                )
                .with_primitive(name)
                .with_related(def_loc),
            );
        }
    }
    for (module, defs) in &lib {
        if !declared(module) {
            // Already CWR-C003; its functions are not judged against contracts.
            continue;
        }
        let lib_path = format!("lib/{module}.py");
        for d in defs {
            let name = format!("{module}.{}", member_name(&d.name));
            let has_contract = symbols
                .get(&name)
                .is_some_and(|s| s.kind == SymbolKind::Primitive && s.module == *module);
            if !has_contract {
                diags.push(
                    Diagnostic::new(
                        Stage::Resolve,
                        Some("CWR-L002"),
                        "implementation",
                        loc(&lib_path, d.line, d.col, None, None),
                        format!("public function {} in {lib_path} has no contract {name} in contracts/{module}.kvx; make it private with a leading underscore or declare the contract", d.name),
                    )
                    .with_primitive(&name),
                );
            }
        }
    }

    // Preconditions of every primitive and adapter section, and drift expressions.
    let mut preconditions = Vec::new();
    for (path, _, _, section) in &sections {
        let owner = section
            .name
            .split_once('.')
            .map(|x| x.1)
            .unwrap_or_default()
            .to_string();
        let params = params_of(section);
        let Some(entry) = section.get("preconditions") else {
            continue;
        };
        let Json::Array(items) = &entry.value.value else {
            continue;
        };
        for (i, node) in items.iter().enumerate() {
            let Json::Array(pair) = &node.value else {
                continue;
            };
            let Some(expr_node) = pair.first() else {
                continue;
            };
            let Some(text) = str_of(expr_node) else {
                continue;
            };
            let code = pair
                .get(1)
                .and_then(|n| str_of(n).map(|s| (s.to_string(), n.col)));
            preconditions.push(PreRef {
                owner: owner.clone(),
                params: params.clone(),
                file: path.clone(),
                section: section.name.clone(),
                key: "preconditions".into(),
                line: entry.line,
                col: expr_node.col,
                index: i + 1,
                expr: text.to_string(),
                code,
            });
        }
    }
    for (path, _) in parsed.of_class(FileClass::Drift) {
        let Some(doc) = parsed.docs.get(path) else {
            continue;
        };
        for section in &doc.sections {
            let Some(n) = section.name.strip_prefix("drift.") else {
                continue;
            };
            let Some(target) = section.get("primitive").and_then(|e| str_of(&e.value)) else {
                continue;
            };
            let Some(entry) = section.get("expr") else {
                continue;
            };
            let Some(text) = str_of(&entry.value) else {
                continue;
            };
            let Some(sym) = symbols.get(target) else {
                continue;
            };
            let code = section
                .get("failure")
                .and_then(|e| str_of(&e.value).map(|s| (s.to_string(), e.value.col)));
            preconditions.push(PreRef {
                owner: target.to_string(),
                params: sym.params.clone(),
                file: path.clone(),
                section: section.name.clone(),
                key: "expr".into(),
                line: entry.line,
                col: entry.value.col,
                index: n.parse().unwrap_or(0),
                expr: text.to_string(),
                code,
            });
        }
    }

    let removed = removed_by_newest(parsed.docs.get("CHANGELOG.kvx"));
    let table = SymbolTable {
        modules: modules.clone(),
        symbols,
        preconditions,
        python,
        lib,
        removed,
    };

    // CWR-V002: undefined references and expressions outside the language.
    for p in &table.preconditions {
        let issues = expr::check(&p.expr, &p.params, &|t| table.side_effect_of(t), &|t| {
            table.removed.contains(t)
        });
        for issue in issues.into_iter().filter(expr::Issue::is_resolve) {
            let col = match &issue {
                expr::Issue::Grammar { offset, .. } => p.col + 1 + offset,
                _ => p.col,
            };
            let what = if p.key == "expr" {
                format!("drift {} expression", p.index)
            } else {
                format!("precondition {}", p.index)
            };
            diags.push(
                Diagnostic::new(
                    Stage::Resolve,
                    Some(issue.code()),
                    "expression",
                    loc(&p.file, p.line, col, Some(&p.section), Some(&p.key)),
                    issue.message(&format!("{}: {what}", p.owner), &p.expr),
                )
                .with_index(p.index)
                .with_primitive(&p.owner),
            );
        }
    }

    merge_deferred(&mut diags, parsed, Stage::Resolve);
    if diags.is_empty() {
        Ok(table)
    } else {
        Err(diags)
    }
}

/// Adds the reader's deferred errors of `stage` that the compiler's own checks did not
/// already report at the same file, line and code.
pub(crate) fn merge_deferred(diags: &mut Vec<Diagnostic>, parsed: &Parsed, stage: Stage) {
    for d in parsed.deferred.iter().filter(|d| d.stage == stage) {
        let dup = diags
            .iter()
            .any(|x| x.file == d.file && x.line == d.line && x.code == d.code);
        if !dup {
            diags.push(d.clone());
        }
    }
}
