// Copyright Sidiora Labs Inc. CWR® 2026-2027
// SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0

//! Lint stage (compiler.md section 3). Every code fails the build; there is no warning level.
//!
//! | Code | Check here |
//! |---|---|
//! | CWR-V001 | A precondition (primitive, adapter or drift entry) calls a primitive or adapter whose `side_effect` is not `read_only` |
//! | CWR-F001 | A precondition code, drift `failure` or adapter `failures` entry outside the eleven-code envelope |
//! | CWR-F002 | An adapter with an empty `failures` list, without `UNAVAILABLE`, without `TIMEOUT`, a `key` adapter without `DUPLICATE_KEY` (codes the gateway produces itself, kernel.md section 7), or without `content_guaranteed = false` |
//! | CWR-D001 | An empty `doc` string, or a `manual/` prose file naming a primitive (`<module>.<name>`), failure code (`UPPER_CASE` with an underscore, outside fenced code) or limit (`limits.<name>`) that the Build does not define |
//! | CWR-L001 | A forbidden import in `lib/`, `adapters/*/live.py` or `migrations/` (closed list in [`crate::pyimports`]) |
//! | CWR-M001 | A `state_shape` change in CHANGELOG.kvx whose `migration` is missing, empty or names a file that does not exist (the source-level half; the ContractDiff half belongs to the changelog gate) |
//!
//! The ContractDiff-dependent half of CWR-M001 (a `state_shape` diff entry with no matching
//! change) needs the diff stage and is not checked by the front end.

use std::str::FromStr;

use cwr_contract::kvx::Json;
use cwr_contract::{Code, FileClass, Limits};

use crate::parse::Parsed;
use crate::pyimports;
use crate::resolve::{self, member_name, str_of, SymbolTable};
use crate::{expr, Diagnostic, Location, Stage};

fn loc(file: &str, line: usize, col: usize, section: Option<&str>, key: Option<&str>) -> Location {
    Location {
        file: file.to_string(),
        line,
        col,
        section: section.map(str::to_string),
        key: key.map(str::to_string),
    }
}

fn in_envelope(code: &str) -> bool {
    Code::from_str(code).is_ok()
}

/// Runs the lint stage; an empty result means the stage passed.
pub fn lint(parsed: &Parsed, table: &SymbolTable) -> Vec<Diagnostic> {
    let mut diags = Vec::new();

    // CWR-V001 and CWR-F001 over every precondition and drift expression.
    for p in &table.preconditions {
        let what = if p.key == "expr" {
            format!("drift {} expression", p.index)
        } else {
            format!("precondition {}", p.index)
        };
        let issues = expr::check(&p.expr, &p.params, &|t| table.side_effect_of(t), &|t| {
            table.removed.contains(t)
        });
        for issue in issues.into_iter().filter(|i| !i.is_resolve()) {
            diags.push(
                Diagnostic::new(
                    Stage::Lint,
                    Some(issue.code()),
                    "visibility",
                    loc(&p.file, p.line, p.col, Some(&p.section), Some(&p.key)),
                    issue.message(&format!("{}: {what}", p.owner), &p.expr),
                )
                .with_index(p.index)
                .with_primitive(&p.owner),
            );
        }
        if let Some((code, col)) = &p.code {
            if !in_envelope(code) {
                diags.push(
                    Diagnostic::new(
                        Stage::Lint,
                        Some("CWR-F001"),
                        "envelope",
                        loc(&p.file, p.line, *col, Some(&p.section), Some(if p.key == "expr" { "failure" } else { &p.key })),
                        format!("{}: {what} names failure code {code:?}, which is outside the closed eleven-code envelope", p.owner),
                    )
                    .with_index(p.index)
                    .with_primitive(&p.owner),
                );
            }
        }
    }

    // Contract and adapter sections: doc strings (CWR-D001), adapter failures and content (F001, F002).
    for (path, file) in &parsed.files {
        if !matches!(file.class, FileClass::Contract | FileClass::Adapter) {
            continue;
        }
        let module = file.module.clone().unwrap_or_default();
        if !table.modules.contains(&module) {
            continue;
        }
        let Some(doc) = parsed.docs.get(path) else {
            continue;
        };
        for section in &doc.sections {
            let Some((prefix, name)) = section.name.split_once('.') else {
                continue;
            };
            if prefix != "primitive" && prefix != "adapter" {
                continue;
            }
            if let Some(e) = section.get("doc") {
                if str_of(&e.value).is_some_and(|s| s.trim().is_empty()) {
                    diags.push(
                        Diagnostic::new(
                            Stage::Lint,
                            Some("CWR-D001"),
                            "doc",
                            loc(path, e.line, e.value.col, Some(&section.name), Some("doc")),
                            format!(
                                "{name}: doc is empty; every rule must be documented in the manual"
                            ),
                        )
                        .with_primitive(name),
                    );
                }
            }
            if prefix != "adapter" {
                continue;
            }
            let section_loc = |key: Option<&str>, line: usize, col: usize| {
                loc(path, line, col, Some(&section.name), key)
            };
            if let Some(e) = section.get("failures") {
                if let Json::Array(items) = &e.value.value {
                    let codes: Vec<&str> = items.iter().filter_map(str_of).collect();
                    for (i, node) in items.iter().enumerate() {
                        if let Some(c) = str_of(node) {
                            if !in_envelope(c) {
                                diags.push(
                                    Diagnostic::new(Stage::Lint, Some("CWR-F001"), "envelope", section_loc(Some("failures"), e.line, node.col), format!("{name}: failures entry {c:?} is outside the closed eleven-code envelope"))
                                        .with_index(i + 1)
                                        .with_primitive(name),
                                );
                            }
                        }
                    }
                    if items.is_empty() {
                        diags.push(
                            Diagnostic::new(Stage::Lint, Some("CWR-F002"), "adapter", section_loc(Some("failures"), e.line, e.value.col), format!("{name}: failures is empty; an adapter must declare every code its gateway can map to"))
                                .with_primitive(name),
                        );
                    } else if !codes.contains(&"UNAVAILABLE") {
                        diags.push(
                            Diagnostic::new(Stage::Lint, Some("CWR-F002"), "adapter", section_loc(Some("failures"), e.line, e.value.col), format!("{name}: failures lacks UNAVAILABLE, the mapping target for undeclared transport outcomes"))
                                .with_primitive(name),
                        );
                    }
                    // kernel.md section 7: codes the gateway produces whatever the service says
                    // must be declared; they are refused here, never patched at runtime.
                    let key_adapter =
                        section.get("idempotency").and_then(|i| str_of(&i.value)) == Some("key");
                    for (needed, why) in [("TIMEOUT", "every adapter has a timeout_ms and an expired one is TIMEOUT"), ("DUPLICATE_KEY", "a key adapter's store answers a reused key with a different payload with DUPLICATE_KEY")] {
                        if !items.is_empty() && !codes.contains(&needed) && (needed == "TIMEOUT" || key_adapter) {
                            diags.push(
                                Diagnostic::new(Stage::Lint, Some("CWR-F002"), "adapter", section_loc(Some("failures"), e.line, e.value.col), format!("{name}: failures lacks {needed}, which the gateway would need: {why}"))
                                    .with_primitive(name),
                            );
                        }
                    }
                }
            }
            match section.get("content_guaranteed") {
                Some(e) if e.value.value == Json::Bool(false) => {}
                Some(e) => diags.push(
                    Diagnostic::new(Stage::Lint, Some("CWR-F002"), "adapter", section_loc(Some("content_guaranteed"), e.line, e.value.col), format!("{name}: content_guaranteed must be false; adapter content is never guaranteed"))
                        .with_primitive(name),
                ),
                None => diags.push(
                    Diagnostic::new(Stage::Lint, Some("CWR-F002"), "adapter", section_loc(Some("content_guaranteed"), section.line, 1), format!("{name}: content_guaranteed = false is not declared"))
                        .with_primitive(name),
                ),
            }
        }
    }

    // CWR-D001 over manual prose.
    for (path, file) in parsed.of_class(FileClass::Manual) {
        diags.extend(prose(path, &file.text, table));
    }

    // CWR-L001 over Python imports.
    for py in &table.python {
        for imp in &py.imports {
            if let Some(why) = pyimports::refusal(py.policy, imp) {
                diags.push(Diagnostic::new(
                    Stage::Lint,
                    Some("CWR-L001"),
                    "import",
                    loc(&py.path, imp.line, imp.col, None, None),
                    format!("{}: {why}", imp.module),
                ));
            }
        }
    }

    // CWR-M001 over CHANGELOG.kvx state_shape changes.
    if let Some(doc) = parsed.docs.get("CHANGELOG.kvx") {
        for section in &doc.sections {
            let Some(id) = section.name.strip_prefix("change.") else {
                continue;
            };
            if section.get("kind").and_then(|e| str_of(&e.value)) != Some("state_shape") {
                continue;
            }
            let problem = match section.get("migration") {
                None => Some((section.line, 1, "declares no migration".to_string())),
                Some(e) => match str_of(&e.value) {
                    Some(m) if m.trim().is_empty() => {
                        Some((e.line, e.value.col, "has an empty migration".to_string()))
                    }
                    Some(m)
                        if !parsed
                            .files
                            .get(m)
                            .is_some_and(|f| f.class == FileClass::Migration) =>
                    {
                        Some((
                            e.line,
                            e.value.col,
                            format!("names migration {m}, which does not exist under migrations/"),
                        ))
                    }
                    _ => None,
                },
            };
            if let Some((line, col, why)) = problem {
                diags.push(Diagnostic::new(
                    Stage::Lint,
                    Some("CWR-M001"),
                    "migration",
                    loc("CHANGELOG.kvx", line, col, Some(&section.name), Some("migration")),
                    format!("state_shape change {id} {why}; every state-shape change needs a pure migration"),
                ));
            }
        }
    }

    resolve::merge_deferred(&mut diags, parsed, Stage::Lint);
    diags
}

fn is_ident_char(c: char) -> bool {
    c.is_ascii_alphanumeric() || c == '_'
}

/// File extensions that make `<module>.<ext>` a file name rather than a primitive reference.
const FILE_EXTENSIONS: [&str; 5] = ["kvx", "md", "py", "json", "txt"];

/// CWR-D001 over one manual prose file.
fn prose(path: &str, text: &str, table: &SymbolTable) -> Vec<Diagnostic> {
    let mut out = Vec::new();
    let mut fenced = false;
    for (n, line) in text.lines().enumerate() {
        let line_no = n + 1;
        if line.trim_start().starts_with("```") {
            fenced = !fenced;
            continue;
        }
        let chars: Vec<char> = line.chars().collect();
        let mut i = 0;
        while i < chars.len() {
            if !(chars[i].is_ascii_alphabetic() || chars[i] == '_')
                || (i > 0 && (is_ident_char(chars[i - 1]) || chars[i - 1] == '.'))
            {
                i += 1;
                continue;
            }
            let start = i;
            while i < chars.len() && is_ident_char(chars[i]) {
                i += 1;
            }
            let word: String = chars[start..i].iter().collect();
            let col = start + 1;
            // `<word>.<member>`
            let member = if i + 1 < chars.len()
                && chars[i] == '.'
                && (chars[i + 1].is_ascii_alphabetic() || chars[i + 1] == '_')
            {
                let mut j = i + 1;
                while j < chars.len() && is_ident_char(chars[j]) {
                    j += 1;
                }
                Some(chars[i + 1..j].iter().collect::<String>())
            } else {
                None
            };
            if let Some(member) = &member {
                if table.modules.contains(&word) && !FILE_EXTENSIONS.contains(&member.as_str()) {
                    let name = format!("{word}.{}", member_name(member));
                    if !table.symbols.contains_key(&name) {
                        out.push(Diagnostic::new(
                            Stage::Lint,
                            Some("CWR-D001"),
                            "prose",
                            loc(path, line_no, col, None, None),
                            format!("manual prose names {word}.{member}, which is not a primitive or adapter of this Build"),
                        ));
                    }
                } else if word == "limits" && !Limits::NAMES.contains(&member.as_str()) {
                    out.push(Diagnostic::new(
                        Stage::Lint,
                        Some("CWR-D001"),
                        "prose",
                        loc(path, line_no, col, None, None),
                        format!("manual prose names limits.{member}, which is not a limit of this Build ({})", Limits::NAMES.join(", ")),
                    ));
                }
            }
            let looks_like_code = !fenced
                && word.contains('_')
                && word.chars().next().is_some_and(|c| c.is_ascii_uppercase())
                && word
                    .chars()
                    .all(|c| c.is_ascii_uppercase() || c.is_ascii_digit() || c == '_')
                && !word.starts_with('_')
                && !word.ends_with('_');
            if looks_like_code && member.is_none() && !in_envelope(&word) {
                out.push(Diagnostic::new(
                    Stage::Lint,
                    Some("CWR-D001"),
                    "prose",
                    loc(path, line_no, col, None, None),
                    format!("manual prose names failure code {word}, which is outside the closed eleven-code envelope"),
                ));
            }
        }
    }
    out
}
