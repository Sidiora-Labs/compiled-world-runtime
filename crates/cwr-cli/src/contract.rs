// Copyright Sidiora Labs Inc. CWR® 2026-2027
// SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0

//! `cwr contract`: the contract crate's reader exposed for drivers and authors.
//!
//! ```text
//! cwr contract check <world_dir> [--json]     parse and type a World Source; 0 ok, 2 rejected
//! cwr contract dump <world_dir>               typed model as canonical JSON; 0 ok, 2 rejected
//! cwr contract dump --from-json <file|->      JSON -> typed model -> JSON; 0 ok, 2 rejected
//! cwr contract emit <world_dir> <out_dir>     write canonical KVX of the model plus the
//!                                             byte-identical non-KVX files into a new out_dir
//! cwr contract codes                          the eleven envelope codes, one per line
//! cwr contract envelope <json|->              parse one failure envelope strictly; 0 ok, 2 rejected
//! cwr contract expr <expression>              parse one precondition expression; 0 ok, 2 rejected
//! ```
//!
//! Errors are printed one per line as `file:line:col: [CODE ]kind: message` on stderr, or
//! as a JSON array on stdout with `--json`. Usage errors exit 1.

use std::fs;
use std::io::Read;
use std::path::Path;

use cwr_contract::{Code, Envelope, Expression, SourceTree, WorldSource};

use crate::cli::{self, Spec};

const CHECK_SPEC: Spec = Spec {
    values: &[],
    repeatable: &[],
    flags: &["--json"],
};

/// One-line help text for `cwr contract`, shown by `cwr help`.
pub const SUMMARY: &str =
    "check, dump or emit a World Source; list codes; parse envelopes and expressions";

const USAGE: &str = "usage:
  cwr contract check <world_dir> [--json]
  cwr contract dump <world_dir>
  cwr contract dump --from-json <file|->
  cwr contract emit <world_dir> <out_dir>
  cwr contract codes
  cwr contract envelope <json|->
  cwr contract expr <expression>";

fn usage_error(message: &str) -> i32 {
    eprintln!("cwr contract: {message}");
    eprintln!("{USAGE}");
    1
}

/// Prints the canonical JSON model; exit 0, or 1 when the model cannot be serialised.
fn print_model(src: &WorldSource) -> i32 {
    match src.to_json() {
        Ok(text) => cli::emit(&text),
        Err(e) => {
            eprintln!("cwr contract: cannot serialise the JSON model: {e}");
            1
        }
    }
}

fn read_input(arg: &str) -> Result<String, String> {
    if arg == "-" {
        let mut s = String::new();
        std::io::stdin()
            .read_to_string(&mut s)
            .map_err(|e| format!("cannot read stdin: {e}"))?;
        Ok(s)
    } else {
        fs::read_to_string(arg).map_err(|e| format!("cannot read {arg}: {e}"))
    }
}

fn report(errors: &cwr_contract::Errors, json: bool) -> i32 {
    if json {
        match serde_json::to_string_pretty(&errors.0) {
            Ok(text) => {
                let written = cli::emit_line(&text);
                if written != 0 {
                    return written;
                }
            }
            Err(e) => {
                eprintln!("cwr contract: cannot serialise the error list: {e}");
                return 1;
            }
        }
    } else {
        for e in &errors.0 {
            eprintln!("{e}");
        }
        eprintln!(
            "cwr contract: {} error(s); World Source rejected",
            errors.0.len()
        );
    }
    2
}

/// Runs `cwr contract <command>`; `args` follow `contract`. Returns 0 on success, 2 when the
/// input is rejected (errors printed) and 1 on a usage or I/O error.
pub fn run(args: &[String]) -> i32 {
    let rest: Vec<&str> = args.iter().skip(1).map(String::as_str).collect();
    match args.first().map(String::as_str) {
        Some("check") => {
            let p = match cli::parse(&args[1..], &CHECK_SPEC) {
                Ok(p) => p,
                Err(m) => return usage_error(&m),
            };
            let json = p.flag("--json");
            let [dir] = p.positional.as_slice() else {
                return usage_error("check takes exactly one <world_dir>");
            };
            match WorldSource::load_dir(Path::new(dir)) {
                Ok(src) => {
                    let summary = serde_json::json!({
                        "ok": true,
                        "name": src.world.world.name,
                        "version": src.world.world.version.to_string(),
                        "example": src.is_example(),
                        "primitives": src.primitives().count(),
                        "adapters": src.adapter_contracts().count(),
                        "releases": src.changelog.releases.len(),
                        "changes": src.changelog.changes.len(),
                        "migrations": src.migrations.len(),
                        "fixtures": src.fixtures.len(),
                        "drift": src.drift.len(),
                        "files": src.files.len(),
                    });
                    if json {
                        cli::emit_line(&format!("{summary:#}"))
                    } else {
                        cli::emit_line(&format!(
                            "ok {} {}{}: {} primitives, {} adapters, {} releases, {} changes, {} migrations, {} fixture files, {} drift files, {} files",
                            src.world.world.name,
                            src.world.world.version,
                            if src.is_example() { " (example)" } else { "" },
                            summary["primitives"],
                            summary["adapters"],
                            summary["releases"],
                            summary["changes"],
                            summary["migrations"],
                            summary["fixtures"],
                            summary["drift"],
                            summary["files"],
                        ))
                    }
                }
                Err(errors) => report(&errors, json),
            }
        }
        Some("dump") => match rest.as_slice() {
            ["--from-json", input] => {
                let text = match read_input(input) {
                    Ok(t) => t,
                    Err(m) => return usage_error(&m),
                };
                match WorldSource::from_json(&text) {
                    Ok(model) => print_model(&model),
                    Err(message) => {
                        eprintln!("{message}");
                        eprintln!("cwr contract: JSON model rejected");
                        2
                    }
                }
            }
            [dir] if !dir.starts_with('-') => match WorldSource::load_dir(Path::new(dir)) {
                Ok(src) => print_model(&src),
                Err(errors) => report(&errors, false),
            },
            _ => usage_error("dump takes <world_dir> or --from-json <file|->"),
        },
        Some("emit") => {
            let [dir, out] = rest.as_slice() else {
                return usage_error("emit takes <world_dir> <out_dir>");
            };
            let out = Path::new(out);
            if out.exists()
                && fs::read_dir(out)
                    .map(|mut d| d.next().is_some())
                    .unwrap_or(true)
            {
                return usage_error("out_dir must not exist or must be empty");
            }
            let tree = match SourceTree::read_dir(Path::new(dir)) {
                Ok(t) => t,
                Err(errors) => return report(&errors, false),
            };
            let src = match WorldSource::load_tree(&tree) {
                Ok(s) => s,
                Err(errors) => return report(&errors, false),
            };
            let kvx = src.to_kvx_files();
            for f in &src.files {
                let target = out.join(&f.path);
                if let Some(parent) = target.parent() {
                    if let Err(e) = fs::create_dir_all(parent) {
                        eprintln!("cwr contract: cannot create {}: {e}", parent.display());
                        return 1;
                    }
                }
                let bytes: Vec<u8> = match kvx.get(&f.path) {
                    Some(text) => text.clone().into_bytes(),
                    None => match tree.files.get(&f.path) {
                        Some(bytes) => bytes.clone().unwrap_or_default(),
                        None => {
                            eprintln!("cwr contract: {} is missing from the source tree", f.path);
                            return 1;
                        }
                    },
                };
                if let Err(e) = fs::write(&target, bytes) {
                    eprintln!("cwr contract: cannot write {}: {e}", target.display());
                    return 1;
                }
            }
            cli::emit_line(&format!(
                "emitted {} files ({} canonical KVX) into {}",
                src.files.len(),
                kvx.len(),
                out.display()
            ))
        }
        Some("codes") => {
            if !rest.is_empty() {
                return usage_error("codes takes no arguments");
            }
            let text: String = Code::ALL.iter().map(|c| format!("{c}\n")).collect();
            cli::emit(&text)
        }
        Some("envelope") => {
            let [input] = rest.as_slice() else {
                return usage_error("envelope takes one <json|->");
            };
            let text = if *input == "-" {
                match read_input("-") {
                    Ok(t) => t,
                    Err(m) => return usage_error(&m),
                }
            } else {
                input.to_string()
            };
            match serde_json::from_str::<Envelope>(&text) {
                Ok(env) => match serde_json::to_string(&env) {
                    Ok(text) => cli::emit_line(&text),
                    Err(e) => {
                        eprintln!("cwr contract: cannot serialise the envelope: {e}");
                        1
                    }
                },
                Err(e) => {
                    eprintln!("cwr contract: envelope rejected: {e}");
                    2
                }
            }
        }
        Some("expr") => {
            let [source] = rest.as_slice() else {
                return usage_error("expr takes one <expression>");
            };
            match source.parse::<Expression>() {
                Ok(expr) => {
                    let out = serde_json::json!({
                        "expr": expr.source(),
                        "calls": expr.ast().calls(),
                        "names": expr.ast().names(),
                    });
                    cli::emit_line(&out.to_string())
                }
                Err(e) => {
                    eprintln!(
                        "<expr>:1:{}: CWR-V002 expression: {}",
                        e.offset + 1,
                        e.message
                    );
                    2
                }
            }
        }
        Some(other) => usage_error(&format!("unknown contract command {other:?}")),
        None => usage_error("missing contract command"),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn args(v: &[&str]) -> Vec<String> {
        v.iter().map(|s| s.to_string()).collect()
    }

    #[test]
    fn usage_errors_exit_one() {
        assert_eq!(run(&args(&[])), 1);
        assert_eq!(run(&args(&["nope"])), 1);
        assert_eq!(run(&args(&["check", "w", "--json", "--json"])), 1);
        assert_eq!(run(&args(&["check", "a", "b"])), 1);
        assert_eq!(run(&args(&["check", "w", "--nope"])), 1);
        assert_eq!(run(&args(&["codes", "x"])), 1);
        assert_eq!(run(&args(&["dump"])), 1);
        assert_eq!(run(&args(&["emit", "only-one"])), 1);
        assert_eq!(run(&args(&["expr"])), 1);
    }

    #[test]
    fn rejected_input_exits_two() {
        assert_eq!(run(&args(&["expr", "((("])), 2);
        assert_eq!(run(&args(&["envelope", "{\"code\": \"NOPE\"}"])), 2);
        assert_eq!(run(&args(&["check", "/nonexistent/world"])), 2);
    }

    #[test]
    fn codes_and_expressions_succeed() {
        assert_eq!(run(&args(&["codes"])), 0);
        assert_eq!(run(&args(&["expr", "fs.exists(path)"])), 0);
    }
}
