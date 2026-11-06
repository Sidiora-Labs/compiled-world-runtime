// Copyright Sidiora Labs Inc. CWR® 2026-2027
// SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0

//! Changelog gate (compiler.md section 6, clauses 3.6, 4.4 and 5.5).
//!
//! Algorithm, applied to the ContractDiff of the diff stage and the newest
//! `[release.<version>]` of `CHANGELOG.kvx`:
//!
//! 1. The newest release's version equals `world.version`.
//! 2. With a previous Build: `world.version` is numerically greater than the previous
//!    version and the declared bump satisfies the derived bump, using the pre-1.0 rule P15
//!    ([`crate::version`]). On the first-Build path (no previous Build) the diff is empty, the
//!    derived bump is none, any version is accepted and steps 3 to 5 have nothing to compare.
//! 3. Every rule entry is covered by a change of the newest release with the same kind whose
//!    target is the entry's target or the entry's module (`fs` covers `fs.write`; `limits`
//!    covers `limits.cpu_ms`). An entry covered only by an older release is uncovered.
//!    * A `params` entry (kind null; world-source.md section 11 has no change kind for a
//!      parameter list) is covered by the module's `state_shape` change, or by any non-doc
//!      change of the newest release on the same target or module. This adopts the coverage
//!      rule recorded in benchmark/upgrades.kvx `[meta].coverage_rule_params` and extends it so
//!      a parameter change without a state-shape change can still be declared.
//!    * A `doc` entry is covered by a `doc_only` change on its target or module, by any change
//!      on exactly its target (a rule change carries its own documentation), or by its module's
//!      `state_shape` change. This is `[meta].coverage_rule_doc`, with module-level `doc_only`
//!      changes added so one change can declare several reworded docs of a module.
//!    * A `manual/` prose entry (`prose_entries`; world-source.md section 11 counts `manual/`
//!      prose under `doc_only`) is covered by a `doc_only` change of the newest release that
//!      names the prose (see step 5), or by any rule change (any kind but `doc_only`) of the
//!      newest release: a release that changes rules rewrites the prose describing them. A
//!      `doc_only` change on an unrelated target does not cover prose. That a rule change
//!      covers prose whatever its target is the recorded deviation
//!      `finding.task_1_2_rule_change_covers_all_prose` of `spec/cwr/qualification.kvx`.
//! 4. Every `state_shape` entry needs a `state_shape` change for the module naming an
//!    existing file under `migrations/`; otherwise CWR-M001.
//! 5. A change of the newest release that matches no diff entry is refused: the changelog may
//!    not announce a change that did not happen. A `doc_only` change matches a doc entry on its
//!    target or module, or a prose entry when its target is `manual`, exactly that prose region,
//!    or a primitive, adapter or module of this source that the region's text (before or after
//!    the change) names; a
//!    `state_shape` change matches the module's state-shape entry; any other change matches an
//!    entry of the same kind, or a `params` entry, on its target or within its module.
//!
//! Every refusal is reported in one run (uncovered entries, unmatched changes, version and
//! migration problems together), each located at the `CHANGELOG.kvx` or `world.kvx` line it
//! concerns, so one edit can fix the whole set.

use cwr_contract::{Change, ChangeKind, FileClass};

use crate::diff::{ContractDiff, Entry};
use crate::parse::Parsed;
use crate::version::bump_str;
use crate::{Context, Diagnostic, Location, Stage, StageRun};

fn target_matches(change_target: &str, entry: &Entry) -> bool {
    change_target == entry.target || change_target == entry.module()
}

/// Whether a `doc_only` change on `target` names the prose of entry `entry`: `target` is
/// `manual`, exactly the entry's region, or a primitive, adapter or module of this source that
/// the region names before or after the change (`fs` is named by `fs.write`).
fn prose_target(target: &str, entry: &Entry, parsed: &Parsed) -> bool {
    if target_matches(target, entry) {
        return true;
    }
    let Some(model) = parsed.model.as_ref() else {
        return false;
    };
    let known = model.world.world.modules.iter().any(|m| m == target)
        || model
            .contracts
            .iter()
            .any(|c| c.primitives.iter().any(|p| p.name == target))
        || model
            .adapters
            .iter()
            .any(|a| a.adapters.iter().any(|x| x.name == target));
    let names = |v: &Option<serde_json::Value>| {
        v.as_ref().and_then(|v| v.as_str()).is_some_and(|text| {
            text.split(|c: char| !(c.is_ascii_alphanumeric() || c == '_' || c == '.'))
                .map(|w| w.trim_end_matches('.'))
                .any(|w| w == target || w.strip_prefix(target).is_some_and(|r| r.starts_with('.')))
        })
    };
    known && (names(&entry.before) || names(&entry.after))
}

fn locate(parsed: &Parsed, file: &str, section: &str, key: Option<&str>) -> Location {
    let mut l = Location {
        file: file.to_string(),
        line: 1,
        col: 1,
        section: Some(section.to_string()),
        key: None,
    };
    if let Some(s) = parsed.docs.get(file).and_then(|d| d.section(section)) {
        l.line = s.line;
        if let Some(k) = key {
            if let Some(e) = s.get(k) {
                l.line = e.line;
                l.col = e.value.col;
                l.key = Some(k.to_string());
            }
        }
    }
    l
}

/// Runs the gate and returns every refusal (empty on success).
pub fn check(diff: &ContractDiff, parsed: &Parsed) -> Vec<Diagnostic> {
    let mut out = Vec::new();
    let Some(model) = parsed.model.as_ref() else {
        out.push(Diagnostic::new(
            Stage::ChangelogGate,
            None,
            "changelog",
            locate(parsed, "CHANGELOG.kvx", "meta", None),
            "the source has no typed changelog",
        ));
        return out;
    };
    let log = &model.changelog;
    let Some(release) = log.newest() else {
        out.push(Diagnostic::new(
            Stage::ChangelogGate,
            None,
            "changelog",
            locate(parsed, "CHANGELOG.kvx", "meta", None),
            "CHANGELOG.kvx declares no release",
        ));
        return out;
    };
    let rel_section = format!("release.{}", release.version);
    let rel_loc = locate(parsed, "CHANGELOG.kvx", &rel_section, None);
    let world_version = model.world.world.version;
    if release.version != world_version {
        out.push(
            Diagnostic::new(
                Stage::ChangelogGate,
                None,
                "release_version",
                locate(parsed, "world.kvx", "world", Some("version")),
                format!("world.version {world_version} differs from the newest release {} in CHANGELOG.kvx", release.version),
            )
            .with_related(rel_loc.clone()),
        );
    }
    let Some(verdict) = &diff.verdict else {
        // First-Build path: no previous Build to compare with.
        return out;
    };
    let version_loc = locate(parsed, "world.kvx", "world", Some("version"));
    if !verdict.greater {
        out.push(
            Diagnostic::new(
                Stage::ChangelogGate,
                None,
                "version_not_greater",
                version_loc.clone(),
                verdict.statement(),
            )
            .with_related(rel_loc.clone()),
        );
    } else if !verdict.satisfied {
        let drivers: Vec<String> = diff
            .entries
            .iter()
            .chain(diff.doc_entries.iter())
            .chain(diff.prose_entries.iter())
            .filter(|e| e.bump == diff.derived_bump)
            .map(|e| e.label())
            .collect();
        out.push(
            Diagnostic::new(
                Stage::ChangelogGate,
                None,
                "version_below_derived_bump",
                version_loc.clone(),
                format!(
                    "{}; the derived bump {} comes from: {}",
                    verdict.statement(),
                    bump_str(diff.derived_bump),
                    drivers.join(", ")
                ),
            )
            .with_related(rel_loc.clone()),
        );
    }
    let changes: Vec<&Change> = release
        .changes
        .iter()
        .filter_map(|id| log.change(id))
        .collect();
    let change_loc = |c: &Change, key: Option<&str>| {
        locate(parsed, "CHANGELOG.kvx", &format!("change.{}", c.id), key)
    };
    let migration_exists = |m: &str| {
        parsed
            .files
            .get(m)
            .map(|f| f.class == FileClass::Migration)
            .unwrap_or(false)
    };

    for e in &diff.entries {
        match e.kind {
            Some(ChangeKind::StateShape) => {
                let found = changes
                    .iter()
                    .find(|c| c.kind == ChangeKind::StateShape && c.target == e.target);
                match found {
                    None => out.push(
                        Diagnostic::new(
                            Stage::ChangelogGate,
                            Some("CWR-M001"),
                            "uncovered_entry",
                            rel_loc.clone(),
                            format!(
                                "ContractDiff entry {} is not covered: release {} has no state_shape change for module {} naming a migration under migrations/",
                                e.label(),
                                release.version,
                                e.target
                            ),
                        )
                        .with_primitive(&e.target),
                    ),
                    Some(c) if c.migration.is_empty() || !migration_exists(&c.migration) => out.push(
                        Diagnostic::new(
                            Stage::ChangelogGate,
                            Some("CWR-M001"),
                            "migration",
                            change_loc(c, Some("migration")),
                            if c.migration.is_empty() {
                                format!("state_shape change {} for module {} names no migration", c.id, c.target)
                            } else {
                                format!("state_shape change {} names migration {}, which does not exist", c.id, c.migration)
                            },
                        )
                        .with_primitive(&e.target),
                    ),
                    Some(_) => {}
                }
            }
            Some(k) => {
                if !changes
                    .iter()
                    .any(|c| c.kind == k && target_matches(&c.target, e))
                {
                    out.push(
                        Diagnostic::new(
                            Stage::ChangelogGate,
                            None,
                            "uncovered_entry",
                            rel_loc.clone(),
                            format!(
                                "ContractDiff entry {} (bump {}) is not covered by any change in release {}: add a change with kind {} and target {}",
                                e.label(),
                                bump_str(e.bump),
                                release.version,
                                k.as_str(),
                                e.target
                            ),
                        )
                        .with_primitive(&e.target),
                    );
                }
            }
            None => {
                let covered = changes.iter().any(|c| {
                    (c.kind == ChangeKind::StateShape && c.target == e.module())
                        || (c.kind != ChangeKind::DocOnly && target_matches(&c.target, e))
                });
                if !covered {
                    out.push(
                        Diagnostic::new(
                            Stage::ChangelogGate,
                            None,
                            "uncovered_entry",
                            rel_loc.clone(),
                            format!(
                                "ContractDiff entry {} (bump major) is not covered by any change in release {}: declare the parameter change with a rule change on {} or a state_shape change for module {}",
                                e.label(),
                                release.version,
                                e.target,
                                e.module()
                            ),
                        )
                        .with_primitive(&e.target),
                    );
                }
            }
        }
    }
    for e in &diff.doc_entries {
        let covered = changes.iter().any(|c| {
            (c.kind == ChangeKind::DocOnly && target_matches(&c.target, e))
                || c.target == e.target
                || (c.kind == ChangeKind::StateShape && c.target == e.module())
        });
        if !covered {
            out.push(
                Diagnostic::new(
                    Stage::ChangelogGate,
                    None,
                    "uncovered_entry",
                    rel_loc.clone(),
                    format!("ContractDiff doc entry {} (bump patch) is not covered by any change in release {}: add a doc_only change with target {}", e.label(), release.version, e.target),
                )
                .with_primitive(&e.target),
            );
        }
    }
    for e in &diff.prose_entries {
        let covered = changes
            .iter()
            .any(|c| c.kind != ChangeKind::DocOnly || prose_target(&c.target, e, parsed));
        if !covered {
            out.push(
                Diagnostic::new(
                    Stage::ChangelogGate,
                    None,
                    "uncovered_entry",
                    rel_loc.clone(),
                    format!("ContractDiff prose entry {} (bump patch) is not covered by any change in release {}: add a doc_only change with target manual, {} or a primitive, adapter or module the prose names", e.label(), release.version, e.target),
                )
                .with_primitive(&e.target),
            );
        }
    }
    for c in &changes {
        let matched = match c.kind {
            ChangeKind::DocOnly => {
                diff.doc_entries
                    .iter()
                    .any(|e| target_matches(&c.target, e))
                    || diff
                        .prose_entries
                        .iter()
                        .any(|e| prose_target(&c.target, e, parsed))
            }
            ChangeKind::StateShape => diff
                .entries
                .iter()
                .any(|e| e.kind == Some(ChangeKind::StateShape) && e.target == c.target),
            k => diff
                .entries
                .iter()
                .any(|e| (e.kind == Some(k) || e.kind.is_none()) && target_matches(&c.target, e)),
        };
        if !matched {
            out.push(
                Diagnostic::new(
                    Stage::ChangelogGate,
                    None,
                    "unmatched_change",
                    change_loc(c, Some("kind")),
                    format!(
                        "change {} ({} {}) in release {} matches no ContractDiff entry: the changelog may not announce a change that did not happen",
                        c.id,
                        c.kind.as_str(),
                        c.target,
                        release.version
                    ),
                )
                .with_primitive(&c.target),
            );
        }
    }
    out
}

/// The changelog-gate stage.
pub fn run_stage(cx: &mut Context) -> StageRun {
    let parsed = match crate::prerequisite(&cx.parsed, Stage::ChangelogGate, Stage::Parse) {
        Ok(p) => p,
        Err(d) => return StageRun::Failed(d),
    };
    let Some(diff) = cx.diff.as_ref() else {
        return StageRun::Failed(vec![Diagnostic::new(
            Stage::ChangelogGate,
            None,
            "diff",
            Location {
                file: "CHANGELOG.kvx".into(),
                line: 1,
                col: 1,
                section: None,
                key: None,
            },
            "no ContractDiff: the diff stage did not run",
        )]);
    };
    let d = check(diff, parsed);
    if d.is_empty() {
        StageRun::Passed
    } else {
        StageRun::Failed(d)
    }
}
