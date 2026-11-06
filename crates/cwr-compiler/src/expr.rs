// Copyright Sidiora Labs Inc. CWR® 2026-2027
// SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0

//! Name resolution for the precondition expression language (world-source.md section 5,
//! clauses 2.1 and 2.6).
//!
//! The grammar itself lives in `cwr_contract::precondition`; this module answers the
//! symbol-table questions the grammar leaves open:
//!
//! * every bare name (a term or a call argument) must be one of the primitive's own
//!   parameters;
//! * every call must target a primitive or adapter declared by this Build;
//! * every call target must be `read_only`.
//!
//! The first two are CWR-V002 (an undefined reference, reported at resolve); the third is
//! CWR-V001 (reported at lint). An expression outside the grammar (attribute access,
//! indexing, host calls, floats and so on) is also CWR-V002.

use cwr_contract::precondition;

/// One problem found in one expression. Offsets are zero-based characters into the
/// expression text.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Issue {
    /// The text is outside the precondition grammar (CWR-V002).
    Grammar {
        /// Zero-based character offset of the first unreadable token.
        offset: usize,
        /// What the grammar expected there.
        message: String,
    },
    /// A bare name that is not a parameter of the primitive (CWR-V002).
    UnknownName {
        /// The name as written.
        name: String,
    },
    /// A call to a name that is not a primitive or adapter of this Build (CWR-V002).
    UnknownPrimitive {
        /// The called name.
        target: String,
        /// True when the newest release announces the target's removal.
        removed: bool,
    },
    /// A call to a declared primitive or adapter whose side effect is not read_only (CWR-V001).
    NotReadOnly {
        /// The called primitive or adapter.
        target: String,
        /// Its declared side-effect class.
        side_effect: String,
    },
}

impl Issue {
    /// The lint code this issue is reported under.
    pub fn code(&self) -> &'static str {
        match self {
            Issue::NotReadOnly { .. } => "CWR-V001",
            _ => "CWR-V002",
        }
    }

    /// Whether the issue belongs to the resolve stage (undefined reference) rather than lint.
    pub fn is_resolve(&self) -> bool {
        !matches!(self, Issue::NotReadOnly { .. })
    }

    /// The diagnostic text for this issue in expression `expr`, introduced by `what`
    /// (for example `precondition 2`).
    pub fn message(&self, what: &str, expr: &str) -> String {
        match self {
            Issue::Grammar { offset, message } => {
                format!("{what} {expr:?} is outside the precondition language at offset {offset}: {message}")
            }
            Issue::UnknownName { name } => format!(
                "{what} {expr:?} references {name:?}, which is not a parameter of this primitive; preconditions may name only parameters and read_only primitives"
            ),
            Issue::UnknownPrimitive { target, removed: true } => format!(
                "{what} {expr:?} calls {target}, which this release removes; a removed primitive cannot be referenced"
            ),
            Issue::UnknownPrimitive { target, removed: false } => format!(
                "{what} {expr:?} calls {target}, which is not a primitive or adapter of this Build"
            ),
            Issue::NotReadOnly { target, side_effect } => format!(
                "{what} {expr:?} calls {target}, whose side_effect is {side_effect}; a rule that can only be checked by acting is not visible"
            ),
        }
    }
}

/// Checks one expression.
///
/// * `params`: the primitive's declared parameter names.
/// * `side_effect_of`: the declared `side_effect` of a primitive or adapter of this Build,
///   or `None` when the name is not declared.
/// * `removed`: whether the newest release removes the named primitive.
///
/// Issues are returned in source order: grammar first (a grammar error yields no other
/// issue), then names and calls as they occur.
pub fn check(
    expr: &str,
    params: &[String],
    side_effect_of: &dyn Fn(&str) -> Option<String>,
    removed: &dyn Fn(&str) -> bool,
) -> Vec<Issue> {
    let ast = match precondition::parse(expr) {
        Ok(ast) => ast,
        Err(e) => {
            return vec![Issue::Grammar {
                offset: e.offset,
                message: e.message,
            }]
        }
    };
    let mut out = Vec::new();
    for name in ast.names() {
        if !params.iter().any(|p| p == name) {
            let issue = Issue::UnknownName {
                name: name.to_string(),
            };
            if !out.contains(&issue) {
                out.push(issue);
            }
        }
    }
    for target in ast.calls() {
        let issue = match side_effect_of(target) {
            None => Issue::UnknownPrimitive {
                target: target.to_string(),
                removed: removed(target),
            },
            Some(se) if se == "read_only" => continue,
            Some(se) => Issue::NotReadOnly {
                target: target.to_string(),
                side_effect: se,
            },
        };
        if !out.contains(&issue) {
            out.push(issue);
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    fn table(name: &str) -> Option<String> {
        match name {
            "fs.exists" | "fs.locked" | "fs.size" => Some("read_only".into()),
            "fs.write" => Some("mutating".into()),
            "search.query" => Some("external".into()),
            _ => None,
        }
    }

    fn run(expr: &str) -> Vec<Issue> {
        let params = vec!["path".to_string(), "content".to_string()];
        check(expr, &params, &table, &|t| t == "fs.gone")
    }

    #[test]
    fn clean_expressions_pass() {
        for e in [
            "fs.exists(path)",
            "not fs.locked(path) and fs.size(path) < 4096",
            "len(content) <= 1048576",
            "fs.exists(\"/x\")",
        ] {
            assert_eq!(run(e), vec![], "{e}");
        }
    }

    #[test]
    fn mutating_and_external_calls_are_v001() {
        let issues = run("fs.write(path, content) and search.query(path, 1)");
        assert_eq!(issues.len(), 2);
        assert!(issues
            .iter()
            .all(|i| i.code() == "CWR-V001" && !i.is_resolve()));
    }

    #[test]
    fn hidden_state_is_v002() {
        assert_eq!(
            run("hidden < 3"),
            vec![Issue::UnknownName {
                name: "hidden".into()
            }]
        );
        assert_eq!(
            run("time.time() > 0"),
            vec![Issue::UnknownPrimitive {
                target: "time.time".into(),
                removed: false
            }]
        );
        assert_eq!(
            run("fs.gone(path)"),
            vec![Issue::UnknownPrimitive {
                target: "fs.gone".into(),
                removed: true
            }]
        );
        assert!(matches!(run("os.environ")[0], Issue::Grammar { .. }));
        assert!(run("fs.exists(other)")
            .iter()
            .all(|i| i.code() == "CWR-V002" && i.is_resolve()));
    }
}
