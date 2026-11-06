// Copyright Sidiora Labs Inc. CWR® 2026-2027
// SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0

//! Closure of World Source Python (clause 3.5, lint CWR-L001) and the public-function
//! inventory of `lib/` used by CWR-L002.
//!
//! A small Python 3.12 lexer splits a file into logical lines while skipping comments and
//! string literals (so `"import os"` inside a docstring is not an import), then:
//!
//! * [`scan`] lists every `import a.b`, `from a.b import c` and relative import, wherever it
//!   appears (top level, inside `if TYPE_CHECKING:`, inside functions), plus every use of
//!   the `__import__` builtin, which is a dynamic import;
//! * [`top_level_defs`] lists every top-level `def` / `async def` with its parameter list.
//!
//! The permitted modules are a closed list (world-source.md section 8; compiler.md CWR-L001):
//!
//! | File class | Permitted top-level modules |
//! |---|---|
//! | `lib/<module>.py` | `cwr_state`, the generated SDK `cwr`, `typing`, `dataclasses`, `json`, `re`, `math`, `collections`, `itertools` |
//! | `adapters/<module>/live.py` | the same as `lib/` |
//! | `migrations/<name>.py` | the same without `cwr_state` (no clock, RNG, adapters or ledger) |
//!
//! Everything else is CWR-L001, including the explicitly forbidden host facilities (`os`,
//! `sys`, `socket`, `http`, `urllib`, `time`, `datetime`, `random`, `secrets`, `subprocess`,
//! `pathlib`, `shutil`, `tempfile`, `threading`, `multiprocessing`, `ctypes`, `importlib`),
//! any other standard-library module, any third-party package and any relative import.

/// The standard-library modules every World Source Python file may import.
pub const STDLIB_ALLOWED: [&str; 7] = [
    "typing",
    "dataclasses",
    "json",
    "re",
    "math",
    "collections",
    "itertools",
];

/// The kernel state API module (lib/ and live.py only, never migrations).
pub const STATE_API: &str = "cwr_state";

/// The generated SDK package.
pub const SDK: &str = "cwr";

/// Host facilities named explicitly by world-source.md section 8. They are also outside the
/// allowlist; the list exists so the diagnostic can say which host facility was reached for.
pub const FORBIDDEN_HOST: [&str; 17] = [
    "os",
    "sys",
    "socket",
    "http",
    "urllib",
    "time",
    "datetime",
    "random",
    "secrets",
    "subprocess",
    "pathlib",
    "shutil",
    "tempfile",
    "threading",
    "multiprocessing",
    "ctypes",
    "importlib",
];

/// Which import policy applies to a file.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Policy {
    /// `lib/` modules: the closed import list, including `cwr_state`.
    Lib,
    /// Live adapter implementations: the same list as `lib/`.
    AdapterLive,
    /// Migrations: the closed list without `cwr_state`.
    Migration,
}

/// One import found in a file.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Import {
    /// The module as written: `a.b.c`, `.`, `..pkg`, or `__import__` for the builtin.
    pub module: String,
    /// One-based line of the module name (or of `__import__`).
    pub line: usize,
    /// One-based column of the module name (or of `__import__`).
    pub col: usize,
}

impl Import {
    /// The top-level package (`a` for `a.b.c`); empty for a relative import.
    pub fn top(&self) -> &str {
        if self.module.starts_with('.') {
            ""
        } else {
            self.module.split('.').next().unwrap_or("")
        }
    }
}

/// A top-level function definition.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Def {
    /// The function name.
    pub name: String,
    /// Plain parameter names in order. `None` when the list uses `*`, `**`, `/` or cannot be read.
    pub params: Option<Vec<String>>,
    /// The full parameter text as written, for messages.
    pub signature: String,
    /// One-based line of the `def` keyword.
    pub line: usize,
    /// One-based column of the `def` keyword.
    pub col: usize,
}

/// Returns why an import is refused under a policy, or `None` when it is permitted.
pub fn refusal(policy: Policy, import: &Import) -> Option<String> {
    if import.module == "__import__" {
        return Some(
            "dynamic import through __import__ is outside the closed import list".to_string(),
        );
    }
    if import.module.starts_with('.') {
        return Some(format!(
            "relative import {:?} is outside the closed import list",
            import.module
        ));
    }
    let top = import.top();
    if STDLIB_ALLOWED.contains(&top) || top == SDK {
        return None;
    }
    if top == STATE_API {
        return match policy {
            Policy::Migration => Some(
                "a migration may not import cwr_state: it is a pure function with no clock, RNG, adapters or ledger".to_string(),
            ),
            _ => None,
        };
    }
    if FORBIDDEN_HOST.contains(&top) {
        return Some(format!("import of host facility {top} is forbidden; use the kernel state API (cwr_state) instead"));
    }
    Some(format!(
        "import of {top} is outside the closed import list (cwr_state, cwr, {})",
        STDLIB_ALLOWED.join(", ")
    ))
}

#[derive(Debug, Clone, PartialEq)]
enum Kind {
    Name,
    Op,
    Str,
    Num,
}

#[derive(Debug, Clone)]
struct Tok {
    kind: Kind,
    text: String,
    line: usize,
    col: usize,
}

/// A logical line: its tokens and the column of its first token (1 = top level).
#[derive(Debug, Clone)]
struct Logical {
    indent: usize,
    toks: Vec<Tok>,
}

fn is_string_prefix(word: &str) -> bool {
    matches!(
        word.to_ascii_lowercase().as_str(),
        "r" | "u" | "b" | "f" | "br" | "rb" | "fr" | "rf"
    )
}

/// Splits Python source into logical lines. The lexer is deliberately forgiving: an
/// unterminated string runs to the end of the file, which can only hide imports from a
/// file that Python itself would refuse to load.
fn logical_lines(src: &str) -> Vec<Logical> {
    let chars: Vec<char> = src.chars().collect();
    let mut out = Vec::new();
    let mut cur: Vec<Tok> = Vec::new();
    let mut depth = 0usize;
    let (mut line, mut col) = (1usize, 1usize);
    let mut i = 0usize;
    let mut fstring_imports: Vec<Tok> = Vec::new();
    macro_rules! advance {
        ($n:expr) => {{
            for _ in 0..$n {
                if i < chars.len() {
                    if chars[i] == '\n' {
                        line += 1;
                        col = 1;
                    } else {
                        col += 1;
                    }
                    i += 1;
                }
            }
        }};
    }
    while i < chars.len() {
        let c = chars[i];
        if c == '\n' {
            if depth == 0 && !cur.is_empty() {
                let indent = cur[0].col;
                out.push(Logical {
                    indent,
                    toks: std::mem::take(&mut cur),
                });
            }
            advance!(1);
            continue;
        }
        if c == '#' {
            while i < chars.len() && chars[i] != '\n' {
                advance!(1);
            }
            continue;
        }
        if c == '\\' && chars.get(i + 1) == Some(&'\n') {
            advance!(2);
            continue;
        }
        if c.is_whitespace() {
            advance!(1);
            continue;
        }
        let (tl, tc) = (line, col);
        if c.is_alphabetic() || c == '_' {
            let mut j = i;
            while j < chars.len() && (chars[j].is_alphanumeric() || chars[j] == '_') {
                j += 1;
            }
            let word: String = chars[i..j].iter().collect();
            if j < chars.len() && (chars[j] == '"' || chars[j] == '\'') && is_string_prefix(&word) {
                // A prefixed string literal: lex it as a string from the quote.
                let is_f = word.to_ascii_lowercase().contains('f');
                advance!(j - i);
                let body = lex_string(&chars, &mut i, &mut line, &mut col);
                if is_f && body.contains("__import__") {
                    fstring_imports.push(Tok {
                        kind: Kind::Name,
                        text: "__import__".into(),
                        line: tl,
                        col: tc,
                    });
                }
                cur.push(Tok {
                    kind: Kind::Str,
                    text: String::new(),
                    line: tl,
                    col: tc,
                });
                continue;
            }
            advance!(j - i);
            cur.push(Tok {
                kind: Kind::Name,
                text: word,
                line: tl,
                col: tc,
            });
            continue;
        }
        if c == '"' || c == '\'' {
            lex_string(&chars, &mut i, &mut line, &mut col);
            cur.push(Tok {
                kind: Kind::Str,
                text: String::new(),
                line: tl,
                col: tc,
            });
            continue;
        }
        if c.is_ascii_digit() {
            let mut j = i;
            while j < chars.len()
                && (chars[j].is_alphanumeric() || chars[j] == '_' || chars[j] == '.')
            {
                j += 1;
            }
            let text: String = chars[i..j].iter().collect();
            advance!(j - i);
            cur.push(Tok {
                kind: Kind::Num,
                text,
                line: tl,
                col: tc,
            });
            continue;
        }
        match c {
            '(' | '[' | '{' => depth += 1,
            ')' | ']' | '}' => depth = depth.saturating_sub(1),
            _ => {}
        }
        // Multi-character operators that matter here: `**` and `->`; everything else single.
        let two: String = chars[i..(i + 2).min(chars.len())].iter().collect();
        let text = if two == "**" || two == "->" {
            two
        } else {
            c.to_string()
        };
        let n = text.chars().count();
        advance!(n);
        cur.push(Tok {
            kind: Kind::Op,
            text,
            line: tl,
            col: tc,
        });
    }
    if !cur.is_empty() {
        let indent = cur[0].col;
        out.push(Logical { indent, toks: cur });
    }
    if !fstring_imports.is_empty() {
        out.push(Logical {
            indent: 0,
            toks: fstring_imports,
        });
    }
    out
}

/// Lexes one string literal starting at the opening quote; returns its body.
fn lex_string(chars: &[char], i: &mut usize, line: &mut usize, col: &mut usize) -> String {
    let q = chars[*i];
    let triple = chars.get(*i + 1) == Some(&q) && chars.get(*i + 2) == Some(&q);
    let step = |i: &mut usize, line: &mut usize, col: &mut usize| {
        if chars[*i] == '\n' {
            *line += 1;
            *col = 1;
        } else {
            *col += 1;
        }
        *i += 1;
    };
    let open = if triple { 3 } else { 1 };
    for _ in 0..open {
        step(i, line, col);
    }
    let mut body = String::new();
    while *i < chars.len() {
        let c = chars[*i];
        if c == '\\' && *i + 1 < chars.len() {
            body.push(c);
            body.push(chars[*i + 1]);
            step(i, line, col);
            step(i, line, col);
            continue;
        }
        if triple {
            if c == q && chars.get(*i + 1) == Some(&q) && chars.get(*i + 2) == Some(&q) {
                for _ in 0..3 {
                    step(i, line, col);
                }
                return body;
            }
        } else if c == q {
            step(i, line, col);
            return body;
        } else if c == '\n' {
            // Unterminated single-quoted string: stop at the line end.
            return body;
        }
        body.push(c);
        step(i, line, col);
    }
    body
}

fn is_name(t: &Tok, text: &str) -> bool {
    t.kind == Kind::Name && t.text == text
}

fn is_op(t: &Tok, text: &str) -> bool {
    t.kind == Kind::Op && t.text == text
}

/// Every import in a Python source text, in source order.
pub fn scan(src: &str) -> Vec<Import> {
    let mut out = Vec::new();
    for logical in logical_lines(src) {
        let toks = &logical.toks;
        for (k, t) in toks.iter().enumerate() {
            if is_name(t, "__import__") {
                out.push(Import {
                    module: "__import__".into(),
                    line: t.line,
                    col: t.col,
                });
                continue;
            }
            if !is_name(t, "import") {
                continue;
            }
            // `from <dotted or dots> import ...`: walk back over names and dots to `from`.
            let mut b = k;
            let mut from_at = None;
            while b > 0 {
                b -= 1;
                let p = &toks[b];
                if is_name(p, "from") {
                    from_at = Some(b);
                    break;
                }
                if p.kind == Kind::Name || is_op(p, ".") {
                    continue;
                }
                break;
            }
            if let Some(f) = from_at {
                if f + 1 < k {
                    let module: String = toks[f + 1..k].iter().map(|t| t.text.as_str()).collect();
                    out.push(Import {
                        module,
                        line: toks[f + 1].line,
                        col: toks[f + 1].col,
                    });
                } else {
                    out.push(Import {
                        module: ".".into(),
                        line: t.line,
                        col: t.col,
                    });
                }
                continue;
            }
            // `import a.b [as c], d ...` up to `;` or the end of the logical line.
            let mut j = k + 1;
            while let Some(first) = toks.get(j) {
                if first.kind != Kind::Name {
                    break;
                }
                let (line, col) = (first.line, first.col);
                let mut module = first.text.clone();
                j += 1;
                while j + 1 < toks.len() && is_op(&toks[j], ".") && toks[j + 1].kind == Kind::Name {
                    module.push('.');
                    module.push_str(&toks[j + 1].text);
                    j += 2;
                }
                out.push(Import { module, line, col });
                if toks.get(j).is_some_and(|t| is_name(t, "as")) {
                    j += 2;
                }
                if toks.get(j).is_some_and(|t| is_op(t, ",")) {
                    j += 1;
                    continue;
                }
                break;
            }
        }
    }
    out.sort_by_key(|a| (a.line, a.col));
    out.dedup();
    out
}

/// Every top-level `def` / `async def`, in source order.
pub fn top_level_defs(src: &str) -> Vec<Def> {
    let mut out = Vec::new();
    for logical in logical_lines(src) {
        if logical.indent != 1 {
            continue;
        }
        let toks = &logical.toks;
        let start = if toks.first().is_some_and(|t| is_name(t, "async")) {
            1
        } else {
            0
        };
        let (Some(d), Some(name), Some(open)) =
            (toks.get(start), toks.get(start + 1), toks.get(start + 2))
        else {
            continue;
        };
        if !is_name(d, "def") || name.kind != Kind::Name || !is_op(open, "(") {
            continue;
        }
        // Collect the parameter list between the matching parentheses.
        let mut depth = 0usize;
        let mut groups: Vec<Vec<&Tok>> = vec![Vec::new()];
        let mut closed = false;
        for t in &toks[start + 3..] {
            if t.kind == Kind::Op && matches!(t.text.as_str(), "(" | "[" | "{") {
                depth += 1;
            } else if t.kind == Kind::Op && matches!(t.text.as_str(), ")" | "]" | "}") {
                if depth == 0 {
                    closed = true;
                    break;
                }
                depth -= 1;
            } else if depth == 0 && is_op(t, ",") {
                groups.push(Vec::new());
                continue;
            }
            if let Some(g) = groups.last_mut() {
                g.push(t);
            }
        }
        if groups.last().is_some_and(|g| g.is_empty()) {
            groups.pop();
        }
        let signature = groups
            .iter()
            .map(|g| {
                g.iter()
                    .map(|t| {
                        if t.kind == Kind::Str {
                            "\"…\""
                        } else {
                            t.text.as_str()
                        }
                    })
                    .collect::<Vec<_>>()
                    .join("")
            })
            .collect::<Vec<_>>()
            .join(", ");
        let mut params = Some(Vec::new());
        if !closed {
            params = None;
        }
        for g in &groups {
            match g.first() {
                Some(t) if t.kind == Kind::Name => {
                    if let Some(p) = params.as_mut() {
                        p.push(t.text.clone());
                    }
                }
                _ => params = None,
            }
        }
        out.push(Def {
            name: name.text.clone(),
            params,
            signature,
            line: d.line,
            col: d.col,
        });
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn imports_outside_strings_and_comments() {
        let src = "\"\"\"Docstring mentioning import os.\"\"\"\n# import sys\nfrom typing import (Any,\n    TypedDict)\nif TYPE_CHECKING:\n    import cwr_state  # ok\nx = 'import time'; import json, os.path as p\ndef f():\n    from . import sibling\n    return __import__(\"socket\")\nraise E from err\ny = f\"{__import__('os')}\"\n";
        let mods: Vec<(String, usize)> =
            scan(src).into_iter().map(|i| (i.module, i.line)).collect();
        assert_eq!(
            mods,
            vec![
                ("typing".into(), 3),
                ("cwr_state".into(), 6),
                ("json".into(), 7),
                ("os.path".into(), 7),
                (".".into(), 9),
                ("__import__".into(), 10),
                ("__import__".into(), 12),
            ]
        );
    }

    #[test]
    fn policies() {
        let imp = |m: &str| Import {
            module: m.into(),
            line: 1,
            col: 1,
        };
        assert_eq!(refusal(Policy::Lib, &imp("typing")), None);
        assert_eq!(refusal(Policy::Lib, &imp("collections.abc")), None);
        assert_eq!(refusal(Policy::Lib, &imp("cwr_state")), None);
        assert_eq!(refusal(Policy::Lib, &imp("cwr.fs")), None);
        assert!(refusal(Policy::Migration, &imp("cwr_state")).is_some());
        for m in FORBIDDEN_HOST
            .iter()
            .chain(["requests", "functools", "__future__", "..x", "__import__"].iter())
        {
            assert!(refusal(Policy::Lib, &imp(m)).is_some(), "{m}");
            assert!(refusal(Policy::AdapterLive, &imp(m)).is_some(), "{m}");
        }
    }

    #[test]
    fn defs() {
        let src = "def _private(state):\n    pass\n\ndef write(state: Any, path: str,\n          content: bytes) -> None:\n    def inner(x): pass\nasync def go(s, *args): pass\nclass C:\n    def method(self): pass\ndef del_(state, ns: dict[str, int] = {}, key=(1, 2)): pass\n";
        let defs = top_level_defs(src);
        let got: Vec<(String, Option<Vec<String>>, usize)> = defs
            .into_iter()
            .map(|d| (d.name, d.params, d.line))
            .collect();
        let v = |xs: &[&str]| Some(xs.iter().map(|s| s.to_string()).collect::<Vec<_>>());
        assert_eq!(
            got,
            vec![
                ("_private".into(), v(&["state"]), 1),
                ("write".into(), v(&["state", "path", "content"]), 4),
                ("go".into(), None, 7),
                ("del_".into(), v(&["state", "ns", "key"]), 10),
            ]
        );
    }
}
