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
//!   the `__import__` builtin, which is a dynamic import, plus every reflective or dynamic
//!   route around the import list (see "Dynamic and reflective access" below);
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
//!
//! ## Dynamic and reflective access
//!
//! An import list alone can be walked around: `exec("import os")`, `getattr(__builtins__,
//! "__imp" + "ort__")("socket")`, `open("/etc/passwd")`, `typing.sys.modules["os"]` or
//! `json.codecs.open(...)`. The scanner therefore also refuses, as CWR-L001, three closed
//! lists (all checked on code tokens, including the `{...}` expressions of f-strings):
//!
//! * [`BANNED_BUILTINS`]: builtins that run code, touch the host or reflect on names
//!   (`exec`, `eval`, `compile`, `open`, `input`, `breakpoint`, `help`, `exit`, `quit`,
//!   `globals`, `locals`, `vars`, `getattr`, `setattr`, `delattr`), used as a bare name;
//! * every dunder name outside [`DUNDER_ALLOWED`] (the special-method names a world's own
//!   classes use), bare or as an attribute: `__builtins__`, `__globals__`, `__class__`,
//!   `__subclasses__`, `__dict__`, `__self__`, `__code__`, `__loader__`, ...;
//! * [`BANNED_ATTRS`]: attribute names (and `from X import <name>` names) through which a
//!   permitted module re-exports a module outside the list (`typing.sys`, `json.codecs`,
//!   `dataclasses.inspect`, `re.enum.bltns`, ...), host-facility names, CPython frame
//!   attributes and the generated SDK's private transport modules.
//!
//! The same closed-list discipline covers the further routes:
//!
//! * [`EVAL_NAMES`]: names through which a permitted module evaluates a string as code or
//!   builds a class from an arbitrary namespace (`typing.get_type_hints` and
//!   `typing.ForwardRef` evaluate string annotations; `ForwardRef._evaluate`,
//!   `typing._eval_type` and `dataclasses._create_fn` reach `eval`/`exec`; `ABCMeta`,
//!   `NamedTupleMeta`, the `metaclass=` keyword and the builtin `type` build classes whose
//!   `__annotations__` keys `@dataclass` would paste into `exec`'d source), refused bare, as
//!   an attribute or as an imported name. `type` stays usable as the soft keyword of a
//!   `type X = ...` statement;
//! * every private attribute (`x._name`, `from m import _name`): the internals of a
//!   permitted module are not part of its closed surface. Imports of a private submodule
//!   (`re._parser`, `cwr._rpc`) are refused as well;
//! * `typing.TypedDict` anywhere except an import (without `as`) or a class base: its
//!   functional form takes arbitrary string keys that `@dataclass` would `exec`;
//! * `def` of a special method outside [`DUNDER_ALLOWED`] (`__prepare__`, `__new__`,
//!   `__init_subclass__`, `__reduce_ex__`, `__getattr__`, ...), and `from m import *`;
//! * any character outside ASCII in code (outside string literals and comments): Python
//!   NFKC-normalises identifiers, so the fullwidth `ｅｘｅｃ` *is* `exec` and `ｏｓ` *is*
//!   `os`. Code is ASCII-only, so every spelling the lists compare is the one Python runs.
//!
//! The lists were derived by walking every module and class reachable from the permitted
//! modules under Python 3.12 without passing a banned name, a private name or a non-allowed
//! dunder, and by reading every `eval`/`exec` call site in those modules: what remains
//! reachable is only `typing`, `dataclasses`, `json` (and its decoder, encoder and scanner),
//! `re`, `math`, `collections` (and `abc`), `itertools`, `abc`, `copy` and `cwr`, and the
//! remaining `eval`/`exec` sites (`collections.namedtuple`, `@dataclass`) receive only
//! validated identifiers.

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

/// Builtins refused as bare names: they run code, reach the host, look up names
/// dynamically (and so can reach `__import__` or any re-exported host module), or (`type`)
/// build a class from an arbitrary namespace. `type` as the soft keyword of a `type X = ...`
/// statement is not the builtin and is permitted.
pub const BANNED_BUILTINS: [&str; 16] = [
    "exec",
    "eval",
    "compile",
    "open",
    "input",
    "breakpoint",
    "help",
    "exit",
    "quit",
    "globals",
    "locals",
    "vars",
    "getattr",
    "setattr",
    "delattr",
    "type",
];

/// Names refused in every position (bare, attribute, imported name, keyword argument):
/// they evaluate a string as code, reach `eval`/`exec` inside a permitted module, or build a
/// class from an arbitrary namespace (whose annotation keys `@dataclass` pastes into
/// `exec`'d source).
pub const EVAL_NAMES: [&str; 11] = [
    "get_type_hints",
    "ForwardRef",
    "evaluate_forward_ref",
    "get_annotations",
    "_eval_type",
    "_evaluate",
    "_create_fn",
    "_FuncBuilder",
    "ABCMeta",
    "NamedTupleMeta",
    "metaclass",
];

/// The class factory whose functional form takes arbitrary string keys; usable only in an
/// import (without `as`) or as a class base.
pub const TYPED_DICT: &str = "TypedDict";

/// The dunder names World Source Python may use: special methods of its own classes and
/// module metadata. Every other dunder (`__builtins__`, `__globals__`, `__class__`, ...) is
/// interpreter introspection and is refused.
pub const DUNDER_ALLOWED: [&str; 27] = [
    "__init__",
    "__name__",
    "__doc__",
    "__all__",
    "__slots__",
    "__post_init__",
    "__eq__",
    "__ne__",
    "__lt__",
    "__le__",
    "__gt__",
    "__ge__",
    "__hash__",
    "__repr__",
    "__str__",
    "__len__",
    "__iter__",
    "__next__",
    "__contains__",
    "__getitem__",
    "__setitem__",
    "__delitem__",
    "__bool__",
    "__enter__",
    "__exit__",
    "__call__",
    "__future__",
];

/// Attribute names (and `from X import` names) refused anywhere: modules outside the list
/// that a permitted module re-exports (`typing.sys`, `json.codecs`, `dataclasses.inspect`,
/// `re.enum.bltns`, `collections._sys`, `typing.operator.attrgetter`, ...), the host
/// facilities by name, CPython frame and generator attributes, and the generated SDK's
/// private transport modules (`cwr._rpc`, `cwr._runner`).
pub const BANNED_ATTRS: [&str; 59] = [
    // host facilities and interpreter modules
    "sys",
    "_sys",
    "os",
    "_os",
    "io",
    "_io",
    "codecs",
    "_codecs",
    "builtins",
    "bltns",
    "inspect",
    "types",
    "_thread",
    "threading",
    "subprocess",
    "socket",
    "importlib",
    "_imp",
    "ctypes",
    "shutil",
    "tempfile",
    "pathlib",
    "multiprocessing",
    "posix",
    "nt",
    "signal",
    "_signal",
    "marshal",
    "gc",
    // modules re-exported by permitted modules that lead onward
    "operator",
    "warnings",
    "contextlib",
    "copyreg",
    "enum",
    "functools",
    "keyword",
    "_sre",
    "_parser",
    "_compiler",
    "_constants",
    "_casefix",
    "_collections_abc",
    "attrgetter",
    "methodcaller",
    // CPython frame, generator and traceback attributes
    "gi_frame",
    "gi_code",
    "cr_frame",
    "cr_code",
    "ag_frame",
    "ag_code",
    "tb_frame",
    "tb_next",
    "f_globals",
    "f_builtins",
    "f_locals",
    "f_back",
    "f_code",
    // the generated SDK's private transport
    "_rpc",
    "_runner",
];

/// Marker prefix of an [`Import`] that records a banned builtin used as a bare name.
pub const BUILTIN_MARK: &str = "builtin:";

/// Marker prefix of an [`Import`] that records a banned attribute, imported name or dunder.
pub const ATTR_MARK: &str = "attr:";

/// Marker prefix of an [`Import`] that records an [`EVAL_NAMES`] name or a misuse of
/// [`TYPED_DICT`].
pub const EVAL_MARK: &str = "eval:";

/// Marker prefix of an [`Import`] that records a private attribute or imported name.
pub const PRIVATE_MARK: &str = "private:";

/// Marker prefix of an [`Import`] that records a `def` of a special method outside
/// [`DUNDER_ALLOWED`].
pub const SPECIAL_MARK: &str = "special:";

/// Marker prefix of an [`Import`] that records a non-ASCII token in code.
pub const NONASCII_MARK: &str = "nonascii:";

/// The [`Import`] module recorded for `from m import *`.
pub const STAR_IMPORT: &str = "*";

fn is_dunder(name: &str) -> bool {
    name.len() > 4 && name.starts_with("__") && name.ends_with("__")
}

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
    /// The module as written: `a.b.c`, `.`, `..pkg`, `__import__` for the builtin,
    /// `builtin:<name>` for a [`BANNED_BUILTINS`] name, `attr:<name>` for a
    /// [`BANNED_ATTRS`] name or a dunder outside [`DUNDER_ALLOWED`], `eval:<name>` for an
    /// [`EVAL_NAMES`] name or a misused `TypedDict`, `private:<name>` for a private attribute,
    /// `special:<name>` for a refused special-method `def`, `nonascii:<token>` for code
    /// outside ASCII, and `*` for a star import.
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
    if let Some(name) = import.module.strip_prefix(BUILTIN_MARK) {
        return Some(format!(
            "the builtin {name} runs code, reaches the host or looks names up dynamically; it is outside the closed import list"
        ));
    }
    if import.module == STAR_IMPORT {
        return Some("from ... import * binds names the closed import list cannot see".to_string());
    }
    if let Some(tok) = import.module.strip_prefix(NONASCII_MARK) {
        return Some(format!(
            "{tok:?} is not ASCII: Python NFKC-normalises identifiers (a fullwidth exec is exec), so code outside string literals and comments must be ASCII"
        ));
    }
    if let Some(name) = import.module.strip_prefix(EVAL_MARK) {
        return Some(if name == TYPED_DICT {
            "TypedDict outside an import or a class base: its functional form takes arbitrary keys that @dataclass pastes into exec'd source".to_string()
        } else {
            format!("{name} evaluates strings as code or builds classes from arbitrary namespaces; it is outside the closed import list")
        });
    }
    if let Some(name) = import.module.strip_prefix(PRIVATE_MARK) {
        return Some(format!(
            "the private name {name} reaches the internals of a module; only public names of the closed import list are permitted"
        ));
    }
    if let Some(name) = import.module.strip_prefix(SPECIAL_MARK) {
        return Some(format!(
            "defining the special method {name} hooks interpreter machinery (class creation, pickling, attribute lookup); it is outside the closed import list"
        ));
    }
    if let Some(name) = import.module.strip_prefix(ATTR_MARK) {
        return Some(if is_dunder(name) {
            format!("interpreter introspection through {name} is outside the closed import list")
        } else {
            format!("{name} reaches a module or interpreter frame outside the closed import list")
        });
    }
    if import.module.starts_with('.') {
        return Some(format!(
            "relative import {:?} is outside the closed import list",
            import.module
        ));
    }
    let top = import.top();
    if let Some(private) = import
        .module
        .split('.')
        .skip(1)
        .find(|p| p.starts_with('_'))
    {
        return Some(format!(
            "import of the private submodule {private} of {top} is outside the closed import list"
        ));
    }
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
    let mut fstring_code: Vec<Logical> = Vec::new();
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
                if is_f {
                    // The `{...}` expressions of an f-string are code: lex them as such, every
                    // token located at the string's start.
                    for mut l in logical_lines(&fstring_exprs(&body)) {
                        for t in &mut l.toks {
                            t.line = tl;
                            t.col = tc;
                        }
                        l.indent = 0;
                        fstring_code.push(l);
                    }
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
    out.extend(fstring_code);
    out
}

/// The `{...}` replacement-field expressions of an f-string body (`{{` and `}}` are literal
/// braces), one per line; a format spec after a top-level `:` or `!` is kept, since it can
/// itself hold nested `{...}` expressions and is otherwise harmless text.
fn fstring_exprs(body: &str) -> String {
    let chars: Vec<char> = body.chars().collect();
    let mut out = String::new();
    let mut i = 0;
    while i < chars.len() {
        if chars[i] == '{' {
            if chars.get(i + 1) == Some(&'{') {
                i += 2;
                continue;
            }
            let mut depth = 1usize;
            let mut j = i + 1;
            while j < chars.len() && depth > 0 {
                match chars[j] {
                    '{' => depth += 1,
                    '}' => depth -= 1,
                    _ => {}
                }
                j += 1;
            }
            let end = if depth == 0 { j - 1 } else { j };
            out.extend(&chars[i + 1..end]);
            out.push('\n');
            i = j;
            continue;
        }
        i += 1;
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

/// The marker module for a banned dynamic or reflective use at token `k`, if any.
fn reflective(toks: &[Tok], k: usize) -> Option<String> {
    let t = &toks[k];
    if t.kind != Kind::Str && !t.text.is_ascii() {
        return Some(format!("{NONASCII_MARK}{}", t.text));
    }
    if t.kind != Kind::Name {
        return None;
    }
    let prev = k.checked_sub(1).map(|p| &toks[p]);
    let after_dot = prev.is_some_and(|p| is_op(p, "."));
    let after_def = prev.is_some_and(|p| is_name(p, "def") || is_name(p, "class"));
    let name = t.text.as_str();
    if is_dunder(name) {
        if name == "__import__" || DUNDER_ALLOWED.contains(&name) {
            return None;
        }
        if after_def {
            return Some(format!("{SPECIAL_MARK}{name}"));
        }
        return Some(format!("{ATTR_MARK}{name}"));
    }
    // The statement this token belongs to (back to the previous `;`).
    let stmt_start = toks[..k]
        .iter()
        .rposition(|p| is_op(p, ";"))
        .map_or(0, |p| p + 1);
    // An attribute, or a name imported by `from X import name`: the import statement's own
    // module path (`from a.b import`, `import a.b`) is judged by `refusal`, not here. Only a
    // `from`/`import` heading a statement (or a compound clause after `:`) makes one;
    // `raise E from err` and `yield from it` are not imports.
    let head = import_head(toks, stmt_start, k);
    let in_import = head.is_some();
    // The local name after `as` is a binding, not a name taken from the module.
    let after_as = prev.is_some_and(|p| is_name(p, "as"));
    let imported_name = !after_as
        && head.is_some_and(|h| {
            is_name(&toks[h], "from") && toks[h..k].iter().any(|p| is_name(p, "import"))
        });
    if EVAL_NAMES.contains(&name) && !after_def {
        return Some(format!("{EVAL_MARK}{name}"));
    }
    if name.starts_with('_') && !after_def && ((after_dot && !in_import) || imported_name) {
        return Some(format!("{PRIVATE_MARK}{name}"));
    }
    if name == TYPED_DICT && !typed_dict_ok(toks, stmt_start, head, k) {
        return Some(format!("{EVAL_MARK}{name}"));
    }
    if BANNED_ATTRS.contains(&name) && ((after_dot && !in_import) || imported_name) {
        return Some(format!("{ATTR_MARK}{name}"));
    }
    if BANNED_BUILTINS.contains(&name) && !after_dot && !after_def && !in_import {
        // `name = ...` and keyword arguments `f(name=...)` bind or label, they do not use.
        let binds = toks.get(k + 1).is_some_and(|n| is_op(n, "="))
            && !toks.get(k + 2).is_some_and(|n| is_op(n, "="));
        // `type X = ...` / `type X[T] = ...` is the type-alias statement, not the builtin.
        let alias_stmt = name == "type"
            && k == stmt_start
            && toks.get(k + 1).is_some_and(|n| n.kind == Kind::Name);
        if !binds && !alias_stmt {
            return Some(format!("{BUILTIN_MARK}{name}"));
        }
    }
    None
}

/// The index of the `from`/`import` keyword heading the import statement that token `k` is
/// part of, if any: the keyword must open the statement or follow a compound clause's `:`.
fn import_head(toks: &[Tok], stmt_start: usize, k: usize) -> Option<usize> {
    (stmt_start..k).rev().find(|&i| {
        (is_name(&toks[i], "from") || is_name(&toks[i], "import"))
            && (i == stmt_start || is_op(&toks[i - 1], ":"))
    })
}

/// Whether the `TypedDict` token at `k` is an imported name without `as`, or a whole class
/// base (`class X(TypedDict, ...)`, `class X(typing.TypedDict)`).
fn typed_dict_ok(toks: &[Tok], stmt_start: usize, head: Option<usize>, k: usize) -> bool {
    let next = toks.get(k + 1);
    if head.is_some() {
        return !next.is_some_and(|n| is_name(n, "as"));
    }
    if !is_name(&toks[stmt_start], "class") {
        return false;
    }
    // Walk back over a dotted prefix (`typing.`) to the token before the base expression.
    let mut b = k;
    while b >= stmt_start + 2 && is_op(&toks[b - 1], ".") && toks[b - 2].kind == Kind::Name {
        b -= 2;
    }
    let before_ok = b > stmt_start && (is_op(&toks[b - 1], "(") || is_op(&toks[b - 1], ","));
    let after_ok = next.is_some_and(|n| is_op(n, ",") || is_op(n, ")"));
    before_ok && after_ok
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
            if let Some(module) = reflective(toks, k) {
                out.push(Import {
                    module,
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
                if toks.get(k + 1).is_some_and(|n| is_op(n, "*")) {
                    out.push(Import {
                        module: STAR_IMPORT.into(),
                        line: toks[k + 1].line,
                        col: toks[k + 1].col,
                    });
                }
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
    fn dynamic_and_reflective_access() {
        let src = concat!(
            "_x = exec(\"import os\")\n",                                       // 1
            "_y = getattr(__builtins__, \"__imp\" + \"ort__\")(\"socket\")\n",  // 2
            "_z = open(\"/etc/passwd\").read\n",                                // 3
            "import typing\nm = typing.sys.modules\n",                          // 4, 5
            "from json import codecs\n",                                        // 6
            "f = ().__class__.__bases__[0].__subclasses__()\n",                 // 7
            "g = f\"{eval('1')}\"\n",                                           // 8
            "import cwr._rpc\n",                                                // 9
            "p = re.compile('x'); q = self.open; r = dict(open=1); eval = 2\n", // 10
            "class C:\n    def __init__(self):\n        self.n = __name__\n",   // 11-13
            "s = f\"cannot open {path}\"\n",                                    // 14
        );
        let got: Vec<(String, usize)> = scan(src).into_iter().map(|i| (i.module, i.line)).collect();
        let want: Vec<(String, usize)> = [
            ("builtin:exec", 1),
            ("builtin:getattr", 2),
            ("attr:__builtins__", 2),
            ("builtin:open", 3),
            ("typing", 4),
            ("attr:sys", 5),
            ("json", 6),
            ("attr:codecs", 6),
            ("attr:__class__", 7),
            ("attr:__bases__", 7),
            ("attr:__subclasses__", 7),
            ("builtin:eval", 8),
            ("cwr._rpc", 9),
        ]
        .iter()
        .map(|(m, l)| (m.to_string(), *l))
        .collect();
        assert_eq!(got, want);
        let lib = |m: &str| {
            refusal(
                Policy::Lib,
                &Import {
                    module: m.into(),
                    line: 1,
                    col: 1,
                },
            )
        };
        for (m, _) in &want {
            if !matches!(m.as_str(), "typing" | "json") {
                assert!(lib(m).is_some(), "{m}");
            }
        }
    }

    #[test]
    fn normalisation_string_evaluation_and_private_internals() {
        let src = concat!(
            "_x = \u{ff45}\u{ff58}\u{ff45}\u{ff43}(\"import os\")\n", // 1
            "import \u{ff4f}\u{ff53}\n",                              // 2
            "h = typing.get_type_hints(f); from typing import ForwardRef\n", // 3
            "c = dataclasses._create_fn; from dataclasses import _MISSING_TYPE as _g; d = self._k\n", // 4
            "r = typing.get_args(a)[0]._evaluate(None, None, frozenset())\n", // 5
            "T = TypedDict(\"T\", {\"a=1\": int}); X = type(\"X\", (), {})\n", // 6
            "class M(collections.abc.ABCMeta): pass\n",                       // 7
            "class K(metaclass=M):\n    def __prepare__(m, n, b): pass\n",    // 8, 9
            "from typing import *\n",                                         // 10
            "raise E from exec(\"x\")\n",                                     // 11
            "class D(typing.TypedDict, total=False):\n    def __init__(self): pass\n", // 12, 13
            "type Alias = int; from typing import TypedDict\n",               // 14
            "s = \"\u{ff45}x\"  # \u{ff45}\n",                                // 15
        );
        let got: Vec<(String, usize)> = scan(src).into_iter().map(|i| (i.module, i.line)).collect();
        let want: Vec<(String, usize)> = [
            ("nonascii:\u{ff45}\u{ff58}\u{ff45}\u{ff43}", 1),
            ("\u{ff4f}\u{ff53}", 2),
            ("nonascii:\u{ff4f}\u{ff53}", 2),
            ("eval:get_type_hints", 3),
            ("typing", 3),
            ("eval:ForwardRef", 3),
            ("eval:_create_fn", 4),
            ("dataclasses", 4),
            ("private:_MISSING_TYPE", 4),
            ("private:_k", 4),
            ("eval:_evaluate", 5),
            ("eval:TypedDict", 6),
            ("builtin:type", 6),
            ("eval:ABCMeta", 7),
            ("eval:metaclass", 8),
            ("special:__prepare__", 9),
            ("typing", 10),
            ("*", 10),
            ("builtin:exec", 11),
            ("typing", 14),
        ]
        .iter()
        .map(|(m, l)| (m.to_string(), *l))
        .collect();
        assert_eq!(got, want);
        for (m, _) in &want {
            let imp = Import {
                module: m.clone(),
                line: 1,
                col: 1,
            };
            assert_eq!(
                refusal(Policy::Lib, &imp).is_none(),
                matches!(m.as_str(), "typing" | "dataclasses"),
                "{m}"
            );
        }
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
