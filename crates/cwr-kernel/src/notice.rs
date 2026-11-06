// Copyright Sidiora Labs Inc. CWR® 2026-2027
// SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0

//! The recompile notice (kernel.md section 10, model-surface.md section 5; clause 5.3;
//! decisions.md D09).
//!
//! [`derive`] builds the notice an upgrade stores from the target Build's compiled changelog
//! (`contracts.json` `changelog`, compiled from `CHANGELOG.kvx`): the old and new versions and
//! Build hashes, the summary of every release between the two versions and every change whose
//! target this Instance has used, where a primitive is used when the ledger holds a `primitive`
//! or `adapter` record naming it and a module target counts as used when any of its primitives
//! was used. The stored notice is the same for every Build; [`render`] produces context
//! component 2, and only an experimental `--x-version-only` or `--x-silent-change` Build alters
//! that rendering.
//!
//! Rendering (model-surface.md section 5, with the lines it leaves open pinned as
//! `benchmark/upgrades.kvx` `[meta].notice_template` records them):
//!
//! ```text
//! WORLD RECOMPILED  <old> -> <new>   (<old build> -> <new build>)
//! Summary: <summary>                      one line per release, `Summary <version>:` when several
//!
//! Changes affecting primitives this instance has used:
//!   <verb>  <target><label>               removed | added | changed; " (state shape)", " (documentation)"
//!     before  <before>                    verbatim from the changelog; "(none)" when empty
//!     after   <after>
//!                                          "  (none)" when nothing is listed
//! [State was migrated. ]Your ledger is intact. The manual above describes <new>.
//! ```
//!
//! Each notice is stored as `notices/<seq>.json` under the Instance, where `seq` is the seq of
//! its `upgrade` ledger record, and the record carries the notice's hash. A notice is pending,
//! and so rendered into the next context, exactly while no `turn` record follows its upgrade
//! record; [`pending`] ignores any file that its upgrade record does not certify.

use std::collections::BTreeSet;
use std::fs;
use std::path::{Path, PathBuf};

use serde_json::{json, Value};

use crate::migrate::{releases_between, Migration};
use crate::state::{Build, Instance};
use crate::{canonical_json, io_err, sha256_hex, write_atomic, KernelError, Result};

/// Directory of stored notices inside an Instance directory.
pub const NOTICE_DIR: &str = "notices";

/// One listed change, verbatim from the compiled changelog.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct NoticeChange {
    /// Change id.
    pub id: String,
    /// Change kind (world-source.md section 11).
    pub kind: String,
    /// Primitive, adapter or module the change targets.
    pub target: String,
    /// The old rule in words.
    pub before: String,
    /// The new rule in words.
    pub after: String,
}

/// The stored recompile notice of one upgrade.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Notice {
    /// Version the Instance ran before the upgrade.
    pub old_version: String,
    /// Version of the target Build.
    pub new_version: String,
    /// Build hash before the upgrade.
    pub old_build: String,
    /// Build hash after the upgrade.
    pub new_build: String,
    /// `(version, summary)` of every release between the versions, oldest first.
    pub summaries: Vec<(String, String)>,
    /// Primitives this Instance had used at the upgrade, sorted.
    pub used: Vec<String>,
    /// Changes whose target was used, in changelog order.
    pub changes: Vec<NoticeChange>,
    /// Migration paths applied, in order.
    pub migrations: Vec<String>,
    /// Seq of the `upgrade` record that commits this notice.
    pub upgrade_seq: u64,
}

/// How component 2 renders a stored notice under the bound Build.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Rendering {
    /// Control Builds: header, summary, every affecting change, migration statement.
    Full,
    /// `--x-version-only`: the header line alone.
    VersionOnly,
    /// `--x-silent-change`: nothing.
    Silent,
}

impl Rendering {
    /// The rendering a Build selects: only an experimental Build whose flag is
    /// `--x-version-only` or `--x-silent-change` departs from [`Rendering::Full`].
    pub fn for_build(build: &Build) -> Rendering {
        let flag = build
            .experimental
            .as_deref()
            .map(|f| f.trim_start_matches('-').replace('_', "-"))
            .unwrap_or_default();
        match flag.trim_start_matches("x-") {
            "version-only" => Rendering::VersionOnly,
            "silent-change" => Rendering::Silent,
            _ => Rendering::Full,
        }
    }
}

/// The primitives an Instance has used: every `primitive` and `adapter` record's name.
pub fn used_primitives(records: &[Value]) -> BTreeSet<String> {
    records
        .iter()
        .filter(|r| matches!(r["kind"].as_str(), Some("primitive") | Some("adapter")))
        .filter_map(|r| r["primitive"].as_str().map(str::to_string))
        .collect()
}

/// Whether a change target counts as used: the primitive itself, or a module any of whose
/// primitives was used.
pub fn target_used(target: &str, used: &BTreeSet<String>) -> bool {
    used.contains(target)
        || (!target.contains('.')
            && used
                .iter()
                .any(|p| p.split_once('.').map(|(m, _)| m) == Some(target)))
}

/// Derives the notice of an upgrade from `from` to `to` for an Instance with ledger `records`,
/// committed by the `upgrade` record at `upgrade_seq`.
pub fn derive(
    from: &Build,
    to: &Build,
    records: &[Value],
    migrations: &[Migration],
    upgrade_seq: u64,
) -> Result<Notice> {
    let used = used_primitives(records);
    let releases = releases_between(to, &from.version)?;
    let mut changes = Vec::new();
    for r in &releases {
        for c in &r.changes {
            let target = c["target"].as_str().unwrap_or_default();
            if target_used(target, &used) {
                let s = |k: &str| c[k].as_str().unwrap_or_default().to_string();
                changes.push(NoticeChange {
                    id: s("id"),
                    kind: s("kind"),
                    target: target.to_string(),
                    before: s("before"),
                    after: s("after"),
                });
            }
        }
    }
    Ok(Notice {
        old_version: from.version.clone(),
        new_version: to.version.clone(),
        old_build: from.hash.clone(),
        new_build: to.hash.clone(),
        summaries: releases
            .iter()
            .map(|r| (r.version.clone(), r.summary.clone()))
            .collect(),
        used: used.into_iter().collect(),
        changes,
        migrations: migrations.iter().map(|m| m.path.clone()).collect(),
        upgrade_seq,
    })
}

impl Notice {
    /// The stored form.
    pub fn to_json(&self) -> Value {
        json!({
            "format": "cwr.recompile-notice",
            "schema_version": 1,
            "old_version": self.old_version,
            "new_version": self.new_version,
            "old_build": self.old_build,
            "new_build": self.new_build,
            "summaries": self.summaries.iter().map(|(v, s)| json!({"version": v, "summary": s})).collect::<Vec<_>>(),
            "used": self.used,
            "changes": self.changes.iter().map(|c| json!({"id": c.id, "kind": c.kind, "target": c.target, "before": c.before, "after": c.after})).collect::<Vec<_>>(),
            "migrations": self.migrations,
            "upgrade_seq": self.upgrade_seq,
        })
    }

    /// Reads the stored form.
    pub fn from_json(v: &Value) -> Result<Notice> {
        let bad = |m: &str| KernelError::Corrupt(format!("recompile notice: {m}"));
        if v["format"] != "cwr.recompile-notice" || v["schema_version"] != 1 {
            return Err(bad("unknown format or schema_version"));
        }
        let s = |x: &Value, k: &str| {
            x[k].as_str()
                .map(str::to_string)
                .ok_or_else(|| bad(&format!("{k} missing")))
        };
        let strings = |k: &str| -> Result<Vec<String>> {
            v[k].as_array()
                .ok_or_else(|| bad(&format!("{k} missing")))?
                .iter()
                .map(|x| x.as_str().map(str::to_string).ok_or_else(|| bad(k)))
                .collect()
        };
        let list = |k: &str| {
            v[k].as_array()
                .cloned()
                .ok_or_else(|| bad(&format!("{k} missing")))
        };
        Ok(Notice {
            old_version: s(v, "old_version")?,
            new_version: s(v, "new_version")?,
            old_build: s(v, "old_build")?,
            new_build: s(v, "new_build")?,
            summaries: list("summaries")?
                .iter()
                .map(|x| Ok((s(x, "version")?, s(x, "summary")?)))
                .collect::<Result<_>>()?,
            used: strings("used")?,
            changes: list("changes")?
                .iter()
                .map(|c| {
                    Ok(NoticeChange {
                        id: s(c, "id")?,
                        kind: s(c, "kind")?,
                        target: s(c, "target")?,
                        before: s(c, "before")?,
                        after: s(c, "after")?,
                    })
                })
                .collect::<Result<_>>()?,
            migrations: strings("migrations")?,
            upgrade_seq: v["upgrade_seq"]
                .as_u64()
                .ok_or_else(|| bad("upgrade_seq missing"))?,
        })
    }

    /// `sha256:<hex>` of the canonical stored form; the `upgrade` record carries it.
    pub fn hash(&self) -> String {
        format!(
            "sha256:{}",
            sha256_hex(canonical_json(&self.to_json()).as_bytes())
        )
    }
}

/// The header line of a notice.
pub fn header(n: &Notice) -> String {
    format!(
        "WORLD RECOMPILED  {} -> {}   ({} -> {})",
        n.old_version, n.new_version, n.old_build, n.new_build
    )
}

fn verb(kind: &str) -> &'static str {
    match kind {
        "primitive_removed" => "removed",
        "primitive_added" => "added",
        _ => "changed",
    }
}

fn label(kind: &str) -> &'static str {
    match kind {
        "state_shape" => " (state shape)",
        "doc_only" => " (documentation)",
        _ => "",
    }
}

fn or_none(s: &str) -> &str {
    if s.is_empty() {
        "(none)"
    } else {
        s
    }
}

/// Component 2 of the context for a stored notice: a pure function of the notice and the
/// rendering, with no trailing newline (empty for [`Rendering::Silent`]).
pub fn render(n: &Notice, rendering: Rendering) -> String {
    match rendering {
        Rendering::Silent => return String::new(),
        Rendering::VersionOnly => return header(n),
        Rendering::Full => {}
    }
    let mut lines = vec![header(n)];
    match n.summaries.as_slice() {
        [] => lines.push(format!(
            "Summary: no release between {} and {} is recorded in the changelog.",
            n.old_version, n.new_version
        )),
        [(_, s)] => lines.push(format!("Summary: {s}")),
        many => lines.extend(many.iter().map(|(v, s)| format!("Summary {v}: {s}"))),
    }
    lines.push(String::new());
    lines.push("Changes affecting primitives this instance has used:".into());
    if n.changes.is_empty() {
        lines.push("  (none)".into());
    }
    for c in &n.changes {
        lines.push(format!(
            "  {}  {}{}",
            verb(&c.kind),
            c.target,
            label(&c.kind)
        ));
        lines.push(format!("    before  {}", or_none(&c.before)));
        lines.push(format!("    after   {}", or_none(&c.after)));
    }
    lines.push(String::new());
    lines.push(format!(
        "{}Your ledger is intact. The manual above describes {}.",
        if n.migrations.is_empty() {
            ""
        } else {
            "State was migrated. "
        },
        n.new_version
    ));
    lines.join("\n")
}

fn notice_path(instance_dir: &Path, seq: u64) -> PathBuf {
    instance_dir.join(NOTICE_DIR).join(format!("{seq}.json"))
}

/// Stores `n` durably as `notices/<upgrade_seq>.json` under the Instance directory.
pub fn store(instance_dir: &Path, n: &Notice) -> Result<()> {
    let dir = instance_dir.join(NOTICE_DIR);
    fs::create_dir_all(&dir).map_err(|e| io_err("create", &dir, e))?;
    write_atomic(
        &notice_path(instance_dir, n.upgrade_seq),
        format!("{}\n", canonical_json(&n.to_json())).as_bytes(),
    )
}

/// Removes the notice stored for `seq`, if any (used when an upgrade rolls back).
pub fn discard(instance_dir: &Path, seq: u64) -> Result<()> {
    let p = notice_path(instance_dir, seq);
    match fs::remove_file(&p) {
        Ok(()) => Ok(()),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(e) => Err(io_err("remove", &p, e)),
    }
}

/// The notices to render into the next context: one per `upgrade` record after the last `turn`
/// record, oldest first, each certified by the record's `notice_hash`.
pub fn pending(instance_dir: &Path, records: &[Value]) -> Result<Vec<Notice>> {
    let last_turn = records
        .iter()
        .rposition(|r| r["kind"] == "turn")
        .map(|i| i as i64)
        .unwrap_or(-1);
    let mut out = Vec::new();
    for (i, r) in records.iter().enumerate() {
        if (i as i64) <= last_turn || r["kind"] != "upgrade" {
            continue;
        }
        let seq = r["seq"].as_u64().unwrap_or(i as u64);
        let p = notice_path(instance_dir, seq);
        let bytes = fs::read(&p).map_err(|e| io_err("read notice", &p, e))?;
        let v: Value = serde_json::from_slice(&bytes)
            .map_err(|e| KernelError::Corrupt(format!("{}: {e}", p.display())))?;
        let n = Notice::from_json(&v)?;
        if r["notice_hash"].as_str() != Some(n.hash().as_str()) || n.upgrade_seq != seq {
            return Err(KernelError::Corrupt(format!(
                "{} is not the notice its upgrade record (seq {seq}) certifies",
                p.display()
            )));
        }
        out.push(n);
    }
    Ok(out)
}

/// Context component 2 for the next turn of `inst`: every pending notice rendered under the
/// bound Build, separated by a blank line; None when no notice is pending or the rendering is
/// silent.
pub fn component(inst: &Instance) -> Result<Option<String>> {
    let rendering = Rendering::for_build(&inst.build);
    let parts: Vec<String> = pending(&inst.dir, inst.records())?
        .iter()
        .map(|n| render(n, rendering))
        .filter(|s| !s.is_empty())
        .collect();
    Ok((!parts.is_empty()).then(|| parts.join("\n\n")))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sample(migrated: bool) -> Notice {
        Notice {
            old_version: "0.1.0".into(),
            new_version: "0.2.0".into(),
            old_build: "sha256:aa".into(),
            new_build: "sha256:bb".into(),
            summaries: vec![("0.2.0".into(), "fs.write is limited.".into())],
            used: vec!["fs.write".into(), "kv.get".into()],
            changes: vec![
                NoticeChange {
                    id: "w".into(),
                    kind: "precondition_added".into(),
                    target: "fs.write".into(),
                    before: "accepted any content length".into(),
                    after: "len(content) <= 1048576, otherwise LIMIT".into(),
                },
                NoticeChange {
                    id: "k".into(),
                    kind: "state_shape".into(),
                    target: "kv".into(),
                    before: "flat".into(),
                    after: "namespaced".into(),
                },
            ],
            migrations: if migrated {
                vec!["migrations/m.py".into()]
            } else {
                vec![]
            },
            upgrade_seq: 9,
        }
    }

    #[test]
    fn full_rendering_matches_the_model_surface_layout() {
        let text = render(&sample(true), Rendering::Full);
        assert_eq!(
            text,
            "WORLD RECOMPILED  0.1.0 -> 0.2.0   (sha256:aa -> sha256:bb)\n\
             Summary: fs.write is limited.\n\
             \n\
             Changes affecting primitives this instance has used:\n  \
             changed  fs.write\n    \
             before  accepted any content length\n    \
             after   len(content) <= 1048576, otherwise LIMIT\n  \
             changed  kv (state shape)\n    \
             before  flat\n    \
             after   namespaced\n\
             \n\
             State was migrated. Your ledger is intact. The manual above describes 0.2.0."
        );
        assert!(!render(&sample(false), Rendering::Full).contains("State was migrated."));
    }

    #[test]
    fn experimental_renderings_alter_only_the_surface() {
        let n = sample(true);
        assert_eq!(render(&n, Rendering::VersionOnly), header(&n));
        assert_eq!(render(&n, Rendering::Silent), "");
        let mut none = n.clone();
        none.changes.clear();
        assert!(render(&none, Rendering::Full).contains("used:\n  (none)\n"));
    }

    #[test]
    fn stored_form_round_trips_and_hash_is_stable() {
        let n = sample(true);
        let back = Notice::from_json(&n.to_json()).unwrap();
        assert_eq!(back, n);
        assert_eq!(back.hash(), n.hash());
    }

    #[test]
    fn module_targets_count_as_used_through_their_primitives() {
        let used: BTreeSet<String> = ["kv.get".to_string()].into_iter().collect();
        assert!(target_used("kv", &used));
        assert!(target_used("kv.get", &used));
        assert!(!target_used("kv.set", &used));
        assert!(!target_used("k", &used));
        let records = vec![
            json!({"kind": "header", "primitive": null}),
            json!({"kind": "primitive", "primitive": "fs.read"}),
            json!({"kind": "adapter", "primitive": "search.query"}),
            json!({"kind": "turn", "primitive": null}),
        ];
        let u = used_primitives(&records);
        assert_eq!(
            u.into_iter().collect::<Vec<_>>(),
            vec!["fs.read", "search.query"]
        );
    }
}
