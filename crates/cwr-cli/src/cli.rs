// Copyright Sidiora Labs Inc. CWR® 2026-2027
// SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0

//! What every subcommand shares: one argument rule and one way to write to stdout.
//!
//! Arguments: an option is declared as a value option (takes the next argument, at most once),
//! a repeatable value option (takes the next argument, any number of times) or a flag (no value,
//! at most once). An undeclared `--option`, a missing value and a repeat of a non-repeatable
//! option are usage errors, which every subcommand reports with exit 1. Every other argument is
//! positional; the subcommand checks how many it takes.
//!
//! Output: [`emit`] writes to a locked stdout and flushes. A reader that closed the pipe ends the
//! command quietly with exit 1; any other write failure (a full disk, `/dev/full`) prints one
//! line on stderr and exits 1. No subcommand panics on a closed or full stdout, and none reports
//! success for output it could not deliver.

use std::io::{ErrorKind, Write};
use std::path::PathBuf;

/// Exit code of a command whose stdout could not be written (closed pipe or write failure).
pub const EXIT_OUTPUT: i32 = 1;

/// Writes `text` to stdout and flushes. Returns 0, or [`EXIT_OUTPUT`] when the text could not be
/// delivered (quietly for a closed pipe, with a message on stderr otherwise).
pub fn emit(text: &str) -> i32 {
    emit_bytes(text.as_bytes())
}

/// [`emit`] of `text` followed by one newline.
pub fn emit_line(text: &str) -> i32 {
    let mut s = String::with_capacity(text.len() + 1);
    s.push_str(text);
    s.push('\n');
    emit(&s)
}

/// [`emit`] for raw bytes (a manual or a file printed byte for byte).
pub fn emit_bytes(bytes: &[u8]) -> i32 {
    let mut out = std::io::stdout().lock();
    match out.write_all(bytes).and_then(|()| out.flush()) {
        Ok(()) => 0,
        Err(e) if e.kind() == ErrorKind::BrokenPipe => EXIT_OUTPUT,
        Err(e) => {
            eprintln!("cwr: cannot write to stdout: {e}");
            EXIT_OUTPUT
        }
    }
}

/// The options one subcommand accepts.
#[derive(Debug, Clone, Copy, Default)]
pub struct Spec {
    /// Options that take one value and may be given at most once.
    pub values: &'static [&'static str],
    /// Options that take one value and may be repeated (each occurrence is kept, in order).
    pub repeatable: &'static [&'static str],
    /// Options without a value, given at most once.
    pub flags: &'static [&'static str],
}

/// Parsed arguments: options in the order given and the positional arguments.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Parsed {
    options: Vec<(String, Option<String>)>,
    /// Every argument that is not an option or an option's value, in order.
    pub positional: Vec<String>,
}

/// Whether `a` is spelled as an option (`--name`). A lone `-` (stdin) is positional.
fn is_option(a: &str) -> bool {
    a.starts_with("--") && a.len() > 2
}

/// Parses `args` against `spec` with the rule of this module; the error is the usage message.
pub fn parse(args: &[String], spec: &Spec) -> Result<Parsed, String> {
    let mut p = Parsed::default();
    let mut it = args.iter();
    while let Some(a) = it.next() {
        let name = a.as_str();
        if !is_option(name) {
            p.positional.push(a.clone());
            continue;
        }
        let once = spec.values.contains(&name) || spec.flags.contains(&name);
        if once && p.given(name) {
            return Err(format!("{name} given twice"));
        }
        if spec.values.contains(&name) || spec.repeatable.contains(&name) {
            let v = it.next().ok_or_else(|| format!("{name} needs a value"))?;
            p.options.push((name.to_string(), Some(v.clone())));
        } else if spec.flags.contains(&name) {
            p.options.push((name.to_string(), None));
        } else {
            return Err(format!("unknown option {name}"));
        }
    }
    Ok(p)
}

impl Parsed {
    /// Whether option `name` was given.
    pub fn given(&self, name: &str) -> bool {
        self.options.iter().any(|(n, _)| n == name)
    }

    /// The value of a value option, when given.
    pub fn value(&self, name: &str) -> Option<&str> {
        self.options
            .iter()
            .find(|(n, _)| n == name)
            .and_then(|(_, v)| v.as_deref())
    }

    /// Every value of a repeatable option, in the order given.
    pub fn values(&self, name: &str) -> Vec<&str> {
        self.options
            .iter()
            .filter(|(n, _)| n == name)
            .filter_map(|(_, v)| v.as_deref())
            .collect()
    }

    /// Whether flag `name` was given.
    pub fn flag(&self, name: &str) -> bool {
        self.given(name)
    }

    /// The first given option outside `allowed`, as a usage message.
    pub fn only(&self, allowed: &[&str]) -> Result<(), String> {
        match self
            .options
            .iter()
            .find(|(n, _)| !allowed.contains(&n.as_str()))
        {
            Some((n, _)) => Err(format!("option {n} does not apply here")),
            None => Ok(()),
        }
    }

    /// The Instance store: `--home`, else `$CWR_HOME`, else `.cwr`.
    pub fn home(&self) -> PathBuf {
        match self.value("--home") {
            Some(h) => PathBuf::from(h),
            None => std::env::var_os("CWR_HOME")
                .map(PathBuf::from)
                .unwrap_or_else(|| PathBuf::from(".cwr")),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const SPEC: Spec = Spec {
        values: &["--home", "--seed"],
        repeatable: &["--stub"],
        flags: &["--json"],
    };

    fn args(v: &[&str]) -> Vec<String> {
        v.iter().map(|s| s.to_string()).collect()
    }

    #[test]
    fn values_flags_and_positionals_parse_in_order() {
        let p = parse(
            &args(&[
                "id", "--home", "h", "--stub", "a=1", "--json", "--stub", "b=2", "-",
            ]),
            &SPEC,
        )
        .unwrap();
        assert_eq!(p.positional, args(&["id", "-"]));
        assert_eq!(p.value("--home"), Some("h"));
        assert_eq!(p.values("--stub"), vec!["a=1", "b=2"]);
        assert!(p.flag("--json"));
        assert!(!p.flag("--seed"));
        assert_eq!(p.home(), PathBuf::from("h"));
    }

    #[test]
    fn a_repeated_value_option_or_flag_is_a_usage_error() {
        assert_eq!(
            parse(&args(&["--home", "a", "--home", "b"]), &SPEC),
            Err("--home given twice".into())
        );
        assert_eq!(
            parse(&args(&["--json", "--json"]), &SPEC),
            Err("--json given twice".into())
        );
    }

    #[test]
    fn missing_values_and_unknown_options_are_usage_errors() {
        assert_eq!(
            parse(&args(&["--seed"]), &SPEC),
            Err("--seed needs a value".into())
        );
        assert_eq!(
            parse(&args(&["--nope"]), &SPEC),
            Err("unknown option --nope".into())
        );
    }

    #[test]
    fn only_names_the_first_option_outside_the_allowed_set() {
        let p = parse(&args(&["--json", "--seed", "1"]), &SPEC).unwrap();
        assert!(p.only(&["--json", "--seed"]).is_ok());
        assert_eq!(
            p.only(&["--json"]),
            Err("option --seed does not apply here".into())
        );
    }

    #[test]
    fn emit_writes_to_stdout() {
        assert_eq!(emit(""), 0);
    }
}
