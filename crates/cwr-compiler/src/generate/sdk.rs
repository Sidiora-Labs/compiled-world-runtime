// Copyright Sidiora Labs Inc. CWR® 2026-2027
// SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0

//! Python SDK generator (kernel.md sections 3 and 5; clause 4.6; task 1.3).
//!
//! The SDK is a pure function of `contracts.json`. It is rendered from templates shipped inside
//! the compiler (`sdk/python/cwr/templates/`, embedded at compile time) and contains, under the
//! Build's `sdk/`:
//!
//! | Path | Content |
//! |---|---|
//! | `cwr/__init__.py` | Package identity (world, version, contracts sha256, modules) and the exception classes |
//! | `cwr/_codec.py` | The value codec and the per-call and turn result envelopes, byte-copied from `sdk/python/cwr/_codec.py` |
//! | `cwr/_rpc.py` | The unix-socket RPC client, byte-copied from `sdk/python/cwr/_rpc.py` |
//! | `cwr/errors.py` | `WorldError` and one subclass per envelope code, from `contracts.json` `envelope` |
//! | `<module>.py` | One typed function per primitive and adapter of the module, each performing exactly one RPC |
//!
//! A primitive or parameter whose name is a Python keyword is bound with a trailing underscore
//! (`kv.del` is `kv.del_`); the RPC still carries the contract name. `world.seed` is omitted when
//! `rng.seed_visible` is false (kernel.md section 6).

use std::collections::BTreeMap;

use serde_json::{json, Value};

use super::manual::visible;

const T_INIT: &str = include_str!("../../../../sdk/python/cwr/templates/__init__.py.tmpl");
const T_ERRORS: &str = include_str!("../../../../sdk/python/cwr/templates/errors.py.tmpl");
const T_ERROR_CLASS: &str =
    include_str!("../../../../sdk/python/cwr/templates/error_class.py.tmpl");
const T_MODULE: &str = include_str!("../../../../sdk/python/cwr/templates/module.py.tmpl");
const T_FUNCTION: &str = include_str!("../../../../sdk/python/cwr/templates/function.py.tmpl");
const R_CODEC: &str = include_str!("../../../../sdk/python/cwr/_codec.py");
const R_RPC: &str = include_str!("../../../../sdk/python/cwr/_rpc.py");

/// Every embedded template and runtime file, by repository-relative name (for the compiler
/// revision digest).
pub fn embedded_files() -> [(&'static str, &'static str); 7] {
    [
        ("sdk/python/cwr/_codec.py", R_CODEC),
        ("sdk/python/cwr/_rpc.py", R_RPC),
        ("sdk/python/cwr/templates/__init__.py.tmpl", T_INIT),
        (
            "sdk/python/cwr/templates/error_class.py.tmpl",
            T_ERROR_CLASS,
        ),
        ("sdk/python/cwr/templates/errors.py.tmpl", T_ERRORS),
        ("sdk/python/cwr/templates/function.py.tmpl", T_FUNCTION),
        ("sdk/python/cwr/templates/module.py.tmpl", T_MODULE),
    ]
}

const KEYWORDS: [&str; 35] = [
    "False", "None", "True", "and", "as", "assert", "async", "await", "break", "class", "continue",
    "def", "del", "elif", "else", "except", "finally", "for", "from", "global", "if", "import",
    "in", "is", "lambda", "nonlocal", "not", "or", "pass", "raise", "return", "try", "while",
    "with", "yield",
];

/// The Python identifier for a contract name: keywords get a trailing underscore.
pub fn python_name(name: &str) -> String {
    if KEYWORDS.contains(&name) {
        format!("{name}_")
    } else {
        name.to_string()
    }
}

/// `NOT_FOUND` to `NotFound`.
pub fn class_name(code: &str) -> String {
    code.split('_')
        .map(|w| {
            let mut c = w.chars();
            match c.next() {
                Some(f) => f.to_ascii_uppercase().to_string() + &c.as_str().to_ascii_lowercase(),
                None => String::new(),
            }
        })
        .collect()
}

/// A Python literal for a JSON value (str, list of str, bool, int, null); JSON string escapes
/// are valid Python string escapes for the characters serde_json emits.
fn py(v: &Value) -> String {
    match v {
        Value::Null => "None".into(),
        Value::Bool(true) => "True".into(),
        Value::Bool(false) => "False".into(),
        Value::Array(a) => format!("[{}]", a.iter().map(py).collect::<Vec<_>>().join(", ")),
        Value::Object(m) => {
            let mut keys: Vec<&String> = m.keys().collect();
            keys.sort();
            format!(
                "{{{}}}",
                keys.iter()
                    .map(|k| format!("{}: {}", py(&json!(k)), py(&m[*k])))
                    .collect::<Vec<_>>()
                    .join(", ")
            )
        }
        other => other.to_string(),
    }
}

/// Text safe inside a `"""` docstring.
fn doc_text(s: &str) -> String {
    s.replace('\\', "\\\\").replace("\"\"\"", "\\\"\\\"\\\"")
}

/// The length in bytes of the source header (the CLAUDE.md copyright and SPDX lines) a template
/// starts with. Recognised by line shape, not by exact text, so a later rewrite of the licence
/// identifier in the templates cannot make fragments keep their headers.
fn header_len(template: &str) -> usize {
    let mut n = 0;
    for line in template.split_inclusive('\n') {
        if line.starts_with("# Copyright ") || line.starts_with("# SPDX-License-Identifier:") {
            n += line.len();
        } else {
            break;
        }
    }
    n
}

/// A fragment template (one function, one exception class) without its source header: fragments
/// are spliced into a file whose own template already carries the header, so a generated file
/// carries it exactly once.
fn fragment(template: &str) -> &str {
    &template[header_len(template)..]
}

fn fill(template: &str, vars: &[(&str, String)]) -> String {
    let mut out = template.to_string();
    for (k, v) in vars {
        out = out.replace(&format!("{{{{{k}}}}}"), v);
    }
    out
}

fn s<'a>(v: &'a Value, k: &str) -> &'a str {
    v.get(k).and_then(Value::as_str).unwrap_or("")
}

fn arr<'a>(v: &'a Value, k: &str) -> &'a [Value] {
    v.get(k)
        .and_then(Value::as_array)
        .map(Vec::as_slice)
        .unwrap_or(&[])
}

/// One typed function.
fn function(p: &Value) -> String {
    let name = s(p, "name");
    let short = name.split_once('.').map(|x| x.1).unwrap_or(name);
    let params: Vec<(String, String)> = arr(p, "params")
        .iter()
        .map(|x| {
            (
                x[0].as_str().unwrap_or("").to_string(),
                x[1].as_str().unwrap_or("").to_string(),
            )
        })
        .collect();
    let signature = params
        .iter()
        .map(|(n, t)| format!("{}: {}", python_name(n), py(&json!(t))))
        .collect::<Vec<_>>()
        .join(", ");
    let dict = params
        .iter()
        .map(|(n, _)| format!("{}: {}", py(&json!(n)), python_name(n)))
        .collect::<Vec<_>>()
        .join(", ");
    let mut doc = vec![doc_text(s(p, "doc")), String::new()];
    let kind = super::manual::tag(p);
    doc.push(format!("{} {kind}", super::manual::signature(p)));
    let pre = arr(p, "preconditions");
    if pre.is_empty() {
        doc.push("Requires nothing beyond the parameter types.".into());
    }
    for (i, x) in pre.iter().enumerate() {
        doc.push(format!(
            "Requires {}. {}  else {}",
            i + 1,
            doc_text(x[0].as_str().unwrap_or("")),
            x[1].as_str().unwrap_or("")
        ));
    }
    for e in arr(p, "effects") {
        doc.push(format!("Ensures {}", doc_text(e.as_str().unwrap_or(""))));
    }
    if s(p, "kind") == "adapter" {
        let f: Vec<&str> = arr(p, "failures")
            .iter()
            .filter_map(Value::as_str)
            .collect();
        doc.push(format!("May fail {}", f.join(", ")));
        if p["content_guaranteed"] != Value::Bool(true) {
            doc.push(format!("Content is not guaranteed: an ok result guarantees only the {} shape and the side-effect class.", s(p, "returns")));
        }
    }
    let docstring = doc
        .iter()
        .map(|l| {
            if l.is_empty() {
                String::new()
            } else {
                format!("    {l}")
            }
        })
        .collect::<Vec<_>>()
        .join("\n");
    fill(
        fragment(T_FUNCTION),
        &[
            ("PYNAME", python_name(short)),
            ("SIGNATURE", signature),
            ("RETURNS", py(&json!(s(p, "returns")))),
            ("DOCSTRING", docstring.trim_start().to_string()),
            ("NAME_LITERAL", py(&json!(name))),
            ("PARAMS_DICT", dict),
            (
                "LABEL_ARG",
                if s(p, "kind") == "adapter" && p["content_guaranteed"] != Value::Bool(true) {
                    ", False".to_string()
                } else {
                    String::new()
                },
            ),
        ],
    )
}

/// Renders every SDK file, by path relative to the Build's `sdk/`.
pub fn render(contracts: &Value, contracts_sha256: &str) -> BTreeMap<String, String> {
    let mut out = BTreeMap::new();
    let world = &contracts["world"];
    let name = s(world, "name");
    let version = s(world, "version");
    let modules: Vec<&str> = arr(world, "modules")
        .iter()
        .filter_map(Value::as_str)
        .collect();
    let base = [
        ("WORLD", name.to_string()),
        ("VERSION", version.to_string()),
    ];

    out.insert("cwr/_codec.py".to_string(), R_CODEC.to_string());
    out.insert("cwr/_rpc.py".to_string(), R_RPC.to_string());

    let codes: Vec<&Value> = arr(contracts, "envelope").iter().collect();
    let mut classes = String::new();
    let mut by_code = serde_json::Map::new();
    let mut all = vec![json!("WorldError"), json!("CODES"), json!("from_failure")];
    for c in &codes {
        let code = s(c, "code");
        let class = class_name(code);
        by_code.insert(code.to_string(), json!(class));
        all.push(json!(class));
        classes.push_str(&fill(
            fragment(T_ERROR_CLASS),
            &[
                ("CLASS", class),
                ("CODE", code.to_string()),
                ("MEANING", doc_text(s(c, "meaning"))),
                ("GUIDANCE", doc_text(s(c, "guidance"))),
                ("CODE_LITERAL", py(&json!(code))),
            ],
        ));
    }
    let code_list: Vec<Value> = codes.iter().map(|c| json!(s(c, "code"))).collect();
    let mut vars = base.to_vec();
    vars.extend([
        ("ALL_LITERAL", py(&Value::Array(all))),
        (
            "CODES_LITERAL",
            format!(
                "({},)",
                code_list.iter().map(py).collect::<Vec<_>>().join(", ")
            ),
        ),
        ("CLASSES", classes.trim_end().to_string()),
        ("BY_CODE_LITERAL", py(&Value::Object(by_code))),
    ]);
    out.insert("cwr/errors.py".to_string(), fill(T_ERRORS, &vars));

    let mut vars = base.to_vec();
    vars.extend([
        ("IMPORT_LINE", format!("`import {}`", modules.join(", "))),
        ("WORLD_LITERAL", py(&json!(name))),
        ("VERSION_LITERAL", py(&json!(version))),
        (
            "CONTRACTS_SHA256_LITERAL",
            py(&json!(format!("sha256:{contracts_sha256}"))),
        ),
        (
            "MODULES_LITERAL",
            format!(
                "({},)",
                modules
                    .iter()
                    .map(|m| py(&json!(m)))
                    .collect::<Vec<_>>()
                    .join(", ")
            ),
        ),
    ]);
    out.insert("cwr/__init__.py".to_string(), fill(T_INIT, &vars));

    for m in &modules {
        let members: Vec<&Value> = arr(contracts, "primitives")
            .iter()
            .chain(arr(contracts, "adapters"))
            .filter(|p| s(p, "module") == *m && visible(contracts, s(p, "name")))
            .collect();
        let names: Vec<Value> = members
            .iter()
            .map(|p| {
                json!(python_name(
                    s(p, "name").split_once('.').map(|x| x.1).unwrap_or("")
                ))
            })
            .collect();
        let functions: String = members.iter().map(|p| function(p)).collect();
        let n_pr = members.iter().filter(|p| s(p, "kind") != "adapter").count();
        let n_ad = members.len() - n_pr;
        let mut summary = format!("{n_pr} primitive{}", if n_pr == 1 { "" } else { "s" });
        if n_ad > 0 {
            summary.push_str(&format!(
                " and {n_ad} external adapter{}; adapter results are content not guaranteed",
                if n_ad == 1 { "" } else { "s" }
            ));
        }
        summary.push('.');
        let mut vars = base.to_vec();
        vars.extend([
            ("MODULE", m.to_string()),
            ("SUMMARY", summary),
            ("ALL_LITERAL", py(&Value::Array(names))),
            ("FUNCTIONS", functions.trim_end().to_string()),
        ]);
        let mut text = fill(T_MODULE, &vars);
        if !text.ends_with('\n') {
            text.push('\n');
        }
        out.insert(format!("{m}.py"), text);
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn names() {
        assert_eq!(python_name("del"), "del_");
        assert_eq!(python_name("delete"), "delete");
        assert_eq!(class_name("NOT_FOUND"), "NotFound");
        assert_eq!(class_name("INVARIANT_VIOLATION"), "InvariantViolation");
    }

    #[test]
    fn one_rpc_per_function() {
        let p = json!({"name": "kv.del", "kind": "primitive", "side_effect": "mutating", "params": [["ns", "str"], ["key", "str"]], "returns": "None",
            "preconditions": [["kv.has(ns, key)", "NOT_FOUND"]], "effects": ["not kv.has(ns, key)"], "doc": "Delete."});
        let f = function(&p);
        assert!(f.contains("def del_(ns: \"str\", key: \"str\") -> \"None\":"));
        assert_eq!(f.matches("_call(").count(), 1);
        assert!(f.contains("return _call(\"kv.del\", {\"ns\": ns, \"key\": key})"));
    }

    #[test]
    fn source_header_once_per_generated_file() {
        // Every template carries the same two-line header (copyright, SPDX).
        let header = &T_MODULE[..header_len(T_MODULE)];
        assert_eq!(header.lines().count(), 2, "{header:?}");
        for t in [T_INIT, T_ERRORS, T_ERROR_CLASS, T_MODULE, T_FUNCTION] {
            assert_eq!(&t[..header_len(t)], header);
            assert!(!fragment(t).contains("SPDX-License-Identifier"));
            assert!(!fragment(t).contains("# Copyright "));
        }
        let contracts = json!({
            "world": {"name": "w", "version": "1.0.0", "modules": ["kv"]},
            "envelope": [{"code": "NOT_FOUND", "meaning": "m.", "guidance": "g."},
                         {"code": "LOCKED", "meaning": "m.", "guidance": "g."}],
            "primitives": [{"name": "kv.get", "module": "kv", "kind": "primitive", "side_effect": "pure",
                            "params": [["key", "str"]], "returns": "str", "preconditions": [], "effects": [], "doc": "Get."},
                           {"name": "kv.has", "module": "kv", "kind": "primitive", "side_effect": "pure",
                            "params": [["key", "str"]], "returns": "bool", "preconditions": [], "effects": [], "doc": "Has."}],
            "adapters": []
        });
        let files = render(&contracts, "00");
        for (path, text) in &files {
            assert!(text.starts_with(header), "{path}");
            assert_eq!(text.matches("SPDX-License-Identifier").count(), 1, "{path}");
            assert_eq!(text.matches("# Copyright ").count(), 1, "{path}");
        }
    }
}
