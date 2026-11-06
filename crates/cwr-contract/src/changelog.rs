// Copyright Sidiora Labs Inc. CWR® 2026-2027
// SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0

//! `CHANGELOG.kvx` and migrations (world-source.md sections 11 and 12, clause 3.6).
//!
//! Rules: one `[release.<version>]` per version (a repeated version is a duplicate
//! section); `version` equals the section suffix; `date` is an ISO date; `summary` is
//! non-empty; every listed change exists and every change belongs to exactly one release;
//! `kind` is one of the eleven change kinds; a `state_shape` change names a migration
//! under `migrations/` that exists and defines `migrate(state)` (CWR-M001); a non-empty
//! migration on any other kind must also exist. `world.version` equals the newest release.

use std::collections::BTreeMap;

use serde::{Deserialize, Serialize};

use crate::error::ErrorKind;
use crate::kvx::Document;
use crate::read::{self, Ctx, Keys};
use crate::types::Version;

/// Required `meta.format` value of `CHANGELOG.kvx`.
pub const CHANGELOG_FORMAT: &str = "cwr.changelog";

/// Change kinds and their derived minimum bump (world-source.md section 11).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ChangeKind {
    /// `primitive_added`: a primitive or adapter exists that did not; minimum bump minor.
    PrimitiveAdded,
    /// `primitive_removed`: a primitive or adapter no longer exists; minimum bump major.
    PrimitiveRemoved,
    /// `precondition_added`: a primitive refuses something it used to accept; major.
    PreconditionAdded,
    /// `precondition_removed`: a primitive accepts something it used to refuse; minor.
    PreconditionRemoved,
    /// `precondition_changed`: an expression or its failure code changed; major.
    PreconditionChanged,
    /// `effect_changed`: a declared postcondition changed; major.
    EffectChanged,
    /// `failure_changed`: an adapter's declared failure set changed; major.
    FailureChanged,
    /// `limit_changed`: a value in `limits` or `clock` changed; major when stricter, minor when looser.
    LimitChanged,
    /// `state_shape`: a module's state shape changed and a migration is required; major.
    StateShape,
    /// `adapter_changed`: an adapter's idempotency, timeout, endpoint, undeclared status or
    /// retry ratio changed; major.
    AdapterChanged,
    /// `doc_only`: only `doc` strings or `manual/` prose changed; patch.
    DocOnly,
}

/// A version bump class.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Bump {
    /// No version change is required.
    None,
    /// The patch component must increase.
    Patch,
    /// At least the minor component must increase.
    Minor,
    /// The major component must increase (minor suffices before 1.0, see compiler rules).
    Major,
}

impl ChangeKind {
    /// Every change kind, in the order of world-source.md section 11.
    pub const ALL: [ChangeKind; 11] = [
        ChangeKind::PrimitiveAdded,
        ChangeKind::PrimitiveRemoved,
        ChangeKind::PreconditionAdded,
        ChangeKind::PreconditionRemoved,
        ChangeKind::PreconditionChanged,
        ChangeKind::EffectChanged,
        ChangeKind::FailureChanged,
        ChangeKind::LimitChanged,
        ChangeKind::StateShape,
        ChangeKind::AdapterChanged,
        ChangeKind::DocOnly,
    ];

    /// The snake_case KVX spelling, e.g. `"primitive_added"`.
    pub fn as_str(self) -> &'static str {
        match self {
            ChangeKind::PrimitiveAdded => "primitive_added",
            ChangeKind::PrimitiveRemoved => "primitive_removed",
            ChangeKind::PreconditionAdded => "precondition_added",
            ChangeKind::PreconditionRemoved => "precondition_removed",
            ChangeKind::PreconditionChanged => "precondition_changed",
            ChangeKind::EffectChanged => "effect_changed",
            ChangeKind::FailureChanged => "failure_changed",
            ChangeKind::LimitChanged => "limit_changed",
            ChangeKind::StateShape => "state_shape",
            ChangeKind::AdapterChanged => "adapter_changed",
            ChangeKind::DocOnly => "doc_only",
        }
    }

    /// Parses the exact KVX spelling; `None` for anything else.
    pub fn parse(s: &str) -> Option<Self> {
        ChangeKind::ALL.into_iter().find(|k| k.as_str() == s)
    }

    /// Derived minimum bump. `limit_changed` is major when stricter and minor when looser;
    /// this returns the stricter bound, the diff refines it from the values.
    pub fn min_bump(self) -> Bump {
        match self {
            ChangeKind::PrimitiveAdded | ChangeKind::PreconditionRemoved => Bump::Minor,
            ChangeKind::DocOnly => Bump::Patch,
            _ => Bump::Major,
        }
    }

    /// Kinds a drift schedule may inject into a running enforcement table (compiler.md section 10).
    pub fn drift_applicable(self) -> bool {
        !matches!(
            self,
            ChangeKind::PrimitiveAdded
                | ChangeKind::PrimitiveRemoved
                | ChangeKind::StateShape
                | ChangeKind::DocOnly
        )
    }
}

/// One `[release.<version>]` section of `CHANGELOG.kvx`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Release {
    /// Release version; equal to the section suffix.
    pub version: Version,
    /// ISO date `YYYY-MM-DD`.
    pub date: String,
    /// One human sentence, rendered first in the recompile notice.
    pub summary: String,
    /// Change ids, in declared order.
    pub changes: Vec<String>,
}

/// One `[change.<id>]` section of `CHANGELOG.kvx`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Change {
    /// Change id from the section suffix: ASCII letters, digits, `_` and `-`.
    pub id: String,
    /// What kind of contract change this is.
    pub kind: ChangeKind,
    /// The primitive, adapter or module the change concerns.
    pub target: String,
    /// Human description of the old rule; empty for additions.
    pub before: String,
    /// Human description of the new rule.
    pub after: String,
    /// Path under `migrations/`, or empty.
    pub migration: String,
}

/// Typed `CHANGELOG.kvx`; releases and changes in declared order.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Changelog {
    /// `meta.example`: the file is illustrative.
    pub example: bool,
    /// Releases in declared order.
    pub releases: Vec<Release>,
    /// Changes in declared order.
    pub changes: Vec<Change>,
}

impl Changelog {
    /// The numerically newest release.
    pub fn newest(&self) -> Option<&Release> {
        self.releases.iter().max_by_key(|r| r.version)
    }

    /// The change with id `id`, if declared.
    pub fn change(&self, id: &str) -> Option<&Change> {
        self.changes.iter().find(|c| c.id == id)
    }
}

/// Why a path named as a migration is not one: the reasons the changelog and migration
/// fixtures report, located at the naming value.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum MigrationRefError {
    /// The path is not under `migrations/`.
    NotUnderMigrations(String),
    /// The path is under `migrations/` but is not `migrations/<name>.py`.
    NotAMigrationPath(String),
    /// No such file exists in the source.
    Missing(String),
    /// The file does not define `migrate(state)` at top level.
    NoMigrate(String),
}

impl std::fmt::Display for MigrationRefError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            MigrationRefError::NotUnderMigrations(m) => {
                write!(f, "migration {m:?} must be a path under migrations/")
            }
            MigrationRefError::NotAMigrationPath(m) => {
                write!(f, "migration {m:?} must be migrations/<name>.py")
            }
            MigrationRefError::Missing(m) => write!(f, "migration file {m} does not exist"),
            MigrationRefError::NoMigrate(m) => {
                write!(f, "migration file {m} does not define migrate(state)")
            }
        }
    }
}

impl std::error::Error for MigrationRefError {}

/// True exactly when `s` is a valid calendar date `YYYY-MM-DD` (leap years included).
pub fn is_iso_date(s: &str) -> bool {
    let b = s.as_bytes();
    if b.len() != 10 || b.get(4) != Some(&b'-') || b.get(7) != Some(&b'-') {
        return false;
    }
    // Decimal value of an all-digit byte range; `None` when any byte is not an ASCII digit.
    let number = |r: std::ops::Range<usize>| -> Option<u32> {
        b.get(r)?.iter().try_fold(0u32, |acc, &c| {
            c.is_ascii_digit().then(|| acc * 10 + u32::from(c - b'0'))
        })
    };
    let (Some(year), Some(month), Some(day)) = (number(0..4), number(5..7), number(8..10)) else {
        return false;
    };
    let leap = (year.is_multiple_of(4) && !year.is_multiple_of(100)) || year.is_multiple_of(400);
    let days = match month {
        1 | 3 | 5 | 7 | 8 | 10 | 12 => 31,
        4 | 6 | 9 | 11 => 30,
        2 if leap => 29,
        2 => 28,
        _ => return false,
    };
    day >= 1 && day <= days
}

fn is_change_id(s: &str) -> bool {
    !s.is_empty()
        && s.chars()
            .all(|c| c.is_ascii_alphanumeric() || c == '_' || c == '-')
}

/// Reads `CHANGELOG.kvx`. `migration_ok` refuses a named migration path that does not exist
/// or is malformed.
pub(crate) fn read_changelog(
    ctx: &mut Ctx,
    doc: &Document,
    migration_ok: &dyn Fn(&str) -> Result<(), MigrationRefError>,
) -> Option<Changelog> {
    let (example, _) = read::meta(ctx, doc, CHANGELOG_FORMAT, &[])?;
    let mut ok = true;
    let mut releases: Vec<Release> = Vec::new();
    // change id -> (release version, line of the reference)
    let mut owners: BTreeMap<String, (Version, usize)> = BTreeMap::new();
    let mut references: Vec<(String, usize, usize, Version)> = Vec::new();
    let mut changes: Vec<Change> = Vec::new();
    let mut change_lines: BTreeMap<String, usize> = BTreeMap::new();
    for section in &doc.sections {
        if section.name == "meta" {
            continue;
        }
        if let Some(suffix) = section.name.strip_prefix("release.") {
            let Ok(version) = suffix.parse::<Version>() else {
                ctx.push(
                    section.line,
                    1,
                    ErrorKind::UnknownSection,
                    format!(
                        "section [{}] must be [release.<MAJOR.MINOR.PATCH>]",
                        section.name
                    ),
                );
                ok = false;
                continue;
            };
            let mut keys = Keys::new(section);
            let v = keys.req(ctx, "version").and_then(|e| {
                let s = read::string(ctx, e)?;
                if s != suffix {
                    ctx.at(
                        e,
                        &e.value,
                        ErrorKind::Changelog,
                        format!("release version {s:?} must equal the section suffix {suffix:?}"),
                    );
                    return None;
                }
                Some(())
            });
            let date = keys.req(ctx, "date").and_then(|e| {
                let s = read::string(ctx, e)?;
                if !is_iso_date(&s) {
                    ctx.bad(
                        e,
                        &e.value,
                        format!("date {s:?} must be an ISO date YYYY-MM-DD"),
                    );
                    return None;
                }
                Some(s)
            });
            let summary = keys
                .req(ctx, "summary")
                .and_then(|e| read::nonempty_string(ctx, e));
            let ids = keys.req(ctx, "changes").and_then(|e| {
                let list = read::string_list(ctx, e)?;
                let mut out: Vec<String> = Vec::new();
                let mut good = true;
                for (id, node) in list {
                    if out.contains(&id) {
                        ctx.at(
                            e,
                            node,
                            ErrorKind::Changelog,
                            format!("change {id} is listed twice in release {suffix}"),
                        );
                        good = false;
                        continue;
                    }
                    references.push((id.clone(), e.line, node.col, version));
                    out.push(id);
                }
                good.then_some(out)
            });
            keys.finish(ctx, "a release section");
            match (v, date, summary, ids) {
                (Some(()), Some(date), Some(summary), Some(changes)) => releases.push(Release {
                    version,
                    date,
                    summary,
                    changes,
                }),
                _ => ok = false,
            }
        } else if let Some(id) = section.name.strip_prefix("change.") {
            if !is_change_id(id) {
                ctx.push(
                    section.line,
                    1,
                    ErrorKind::UnknownSection,
                    format!(
                        "section [{}] must be [change.<id>] with id in [A-Za-z0-9_-]",
                        section.name
                    ),
                );
                ok = false;
                continue;
            }
            change_lines.insert(id.to_string(), section.line);
            let mut keys = Keys::new(section);
            let kind = keys.req(ctx, "kind").and_then(|e| {
                let s = read::string(ctx, e)?;
                let k = ChangeKind::parse(&s);
                if k.is_none() {
                    ctx.bad(e, &e.value, format!("change kind {s:?} is not one of the eleven kinds in world-source.md section 11"));
                }
                k
            });
            let target = keys
                .req(ctx, "target")
                .and_then(|e| read::nonempty_string(ctx, e));
            let before = keys.req(ctx, "before").and_then(|e| read::string(ctx, e));
            let after = keys
                .req(ctx, "after")
                .and_then(|e| read::nonempty_string(ctx, e));
            let migration_entry = keys.opt("migration");
            let migration = match migration_entry {
                None => {
                    let kind_label = if kind == Some(ChangeKind::StateShape) { ErrorKind::Migration } else { ErrorKind::MissingKey };
                    ctx.push(section.line, 1, kind_label, format!("change {id} is missing required key migration"));
                    None
                }
                Some(e) => read::string(ctx, e).and_then(|m| {
                    if m.is_empty() {
                        if kind == Some(ChangeKind::StateShape) {
                            ctx.at(e, &e.value, ErrorKind::Migration, format!("state_shape change {id} must name a migration under migrations/"));
                            return None;
                        }
                        return Some(m);
                    }
                    if let Err(message) = migration_ok(&m) {
                        ctx.at(e, &e.value, ErrorKind::Migration, format!("change {id}: {message}"));
                        return None;
                    }
                    Some(m)
                }),
            };
            keys.finish(ctx, "a change section");
            match (kind, target, before, after, migration) {
                (Some(kind), Some(target), Some(before), Some(after), Some(migration)) => changes
                    .push(Change {
                        id: id.to_string(),
                        kind,
                        target,
                        before,
                        after,
                        migration,
                    }),
                _ => ok = false,
            }
        } else {
            ctx.push(section.line, 1, ErrorKind::UnknownSection, format!("unknown section [{}]; CHANGELOG.kvx defines [meta], [release.<version>] and [change.<id>]", section.name));
            ok = false;
        }
    }
    for (id, line, col, version) in &references {
        if !change_lines.contains_key(id) {
            ctx.push(
                *line,
                *col,
                ErrorKind::Changelog,
                format!("release {version} lists change {id}, which has no [change.{id}] section"),
            );
            ok = false;
        }
        if let Some((first, _)) = owners.get(id) {
            ctx.push(*line, *col, ErrorKind::Changelog, format!("change {id} already belongs to release {first}; every change belongs to exactly one release"));
            ok = false;
        } else {
            owners.insert(id.clone(), (*version, *line));
        }
    }
    for (id, line) in &change_lines {
        if !references.iter().any(|(r, ..)| r == id) {
            ctx.push(
                *line,
                1,
                ErrorKind::Changelog,
                format!("change {id} belongs to no release"),
            );
            ok = false;
        }
    }
    if releases.is_empty() && ok {
        ctx.push(
            1,
            1,
            ErrorKind::Changelog,
            "CHANGELOG.kvx must declare at least one [release.<version>]",
        );
        ok = false;
    }
    ok.then_some(Changelog {
        example,
        releases,
        changes,
    })
}

pub(crate) fn emit(log: &Changelog) -> String {
    let q = crate::kvx::quote;
    let mut out = format!(
        "[meta]\nformat = {}\nschema_version = 1\n",
        q(CHANGELOG_FORMAT)
    );
    if log.example {
        out.push_str("example = true\n");
    }
    for r in &log.releases {
        out.push_str(&format!("\n[release.{}]\n", r.version));
        out.push_str(&format!("version = {}\n", q(&r.version.to_string())));
        out.push_str(&format!("date = {}\n", q(&r.date)));
        out.push_str(&format!("summary = {}\n", q(&r.summary)));
        out.push_str(&format!(
            "changes = {}\n",
            crate::kvx::quote_list(&r.changes)
        ));
    }
    for c in &log.changes {
        out.push_str(&format!("\n[change.{}]\n", c.id));
        out.push_str(&format!("kind = {}\n", q(c.kind.as_str())));
        out.push_str(&format!("target = {}\n", q(&c.target)));
        out.push_str(&format!("before = {}\n", q(&c.before)));
        out.push_str(&format!("after = {}\n", q(&c.after)));
        out.push_str(&format!("migration = {}\n", q(&c.migration)));
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn iso_dates() {
        assert!(is_iso_date("2026-11-06"));
        assert!(is_iso_date("2024-02-29"));
        assert!(!is_iso_date("2026-02-29"));
        assert!(!is_iso_date("2026-13-01"));
        assert!(!is_iso_date("2026-1-01"));
    }
}
