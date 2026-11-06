// Copyright Sidiora Labs Inc. CWR® 2026-2027
// SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0

//! The proposal outbox (kernel.md section 11; clause 5.4; decisions.md D10).
//!
//! `world.propose(text)` is the model's one upward channel. The kernel answers it with two
//! durable writes and nothing else: a `proposal` ledger record carrying the proposal id and the
//! sha256 of the text (written by the contract check), and one line appended here to
//! `<instance>/proposals.jsonl`, outside the Build directory and outside the state store.
//!
//! This module is write-only by construction: it exposes no function that reads the outbox, and
//! no other kernel module opens [`OUTBOX_FILE`]. The outbox therefore cannot influence contracts,
//! state, limits or scheduling; the owner reads it outside the world (`cwr instance outbox`).

use std::fs::OpenOptions;
use std::io::Write;
use std::path::Path;

use serde_json::json;

use crate::{canonical_json, io_err, Result};

/// File name of the outbox inside an Instance directory.
pub const OUTBOX_FILE: &str = "proposals.jsonl";

/// Appends one proposal line `{"id", "turn", "text"}` (canonical JSON plus a newline) to the
/// outbox of the Instance at `instance_dir` and fsyncs it before returning. The line is written
/// with one `write` on an `O_APPEND` descriptor, so concurrent readers never see a torn line.
pub fn append(instance_dir: &Path, id: &str, turn: u64, text: &str) -> Result<()> {
    let path = instance_dir.join(OUTBOX_FILE);
    let mut f = OpenOptions::new()
        .create(true)
        .append(true)
        .open(&path)
        .map_err(|e| io_err("open outbox", &path, e))?;
    let line = format!(
        "{}\n",
        canonical_json(&json!({"id": id, "turn": turn, "text": text}))
    );
    f.write_all(line.as_bytes())
        .map_err(|e| io_err("append outbox", &path, e))?;
    f.sync_all().map_err(|e| io_err("fsync outbox", &path, e))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tmp(name: &str) -> std::path::PathBuf {
        let d = std::env::temp_dir().join(format!("cwr-outbox-{}-{name}", std::process::id()));
        let _ = std::fs::remove_dir_all(&d);
        std::fs::create_dir_all(&d).unwrap();
        d
    }

    #[test]
    fn appends_one_canonical_line_per_proposal() {
        let d = tmp("lines");
        append(&d, "p1.1", 1, "first").unwrap();
        append(&d, "p2.1", 2, "line\nbreak \u{e9}").unwrap();
        let text = std::fs::read_to_string(d.join(OUTBOX_FILE)).unwrap();
        let lines: Vec<&str> = text.lines().collect();
        assert_eq!(lines.len(), 2);
        assert_eq!(lines[0], r#"{"id":"p1.1","text":"first","turn":1}"#);
        let v: serde_json::Value = serde_json::from_str(lines[1]).unwrap();
        assert_eq!(v["text"], "line\nbreak \u{e9}");
        std::fs::remove_dir_all(&d).unwrap();
    }
}
