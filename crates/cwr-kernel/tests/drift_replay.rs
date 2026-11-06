// Copyright Sidiora Labs Inc. CWR® 2026-2027
// SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0

//! Drift on an `--x-drift` Build is recorded at its scheduled turn even when the model's message
//! for that turn is refused by the turn protocol, and replay reproduces the drift records
//! (model-surface.md section 8; kernel.md section 9).

use std::path::{Path, PathBuf};

use cwr_kernel::replay::{self, ReplayInputs};
use cwr_kernel::surface::protocol;
use cwr_kernel::{CreateOptions, Instance, ProbeCall};
use serde_json::{json, Value};

const SCHEDULE: &str = "# Copyright Sidiora Labs Inc. CWR® 2026-2027\n# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0\n\n[meta]\nformat = \"cwr.drift\"\nschema_version = 1\n\n[drift.1]\nturn = 1\nprimitive = \"kv.set\"\nchange = \"precondition_added\"\nexpr = \"len(key) < 100\"\nfailure = \"INVALID\"\ndoc = \"keys get shorter\"\n";

fn copy(src: &Path, dst: &Path) {
    std::fs::create_dir_all(dst).unwrap();
    for e in std::fs::read_dir(src).unwrap() {
        let e = e.unwrap();
        let to = dst.join(e.file_name());
        if e.file_type().unwrap().is_dir() {
            copy(&e.path(), &to);
        } else {
            std::fs::copy(e.path(), &to).unwrap();
        }
    }
}

/// The fixture Build packaged as an `--x-drift` Build whose one schedule entry fires at turn 1.
fn drift_build(name: &str) -> PathBuf {
    let src = Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/minbuild");
    let dst = std::env::temp_dir().join(format!(
        "cwr-kernel-drift-{}-{name}-build",
        std::process::id()
    ));
    let _ = std::fs::remove_dir_all(&dst);
    copy(&src, &dst);
    std::fs::create_dir_all(dst.join("fixtures")).unwrap();
    std::fs::write(
        dst.join(cwr_kernel::drift_injector::SCHEDULE_PATH),
        SCHEDULE,
    )
    .unwrap();
    let manifest = dst.join("manifest.kvx");
    let text = std::fs::read_to_string(&manifest).unwrap();
    let body = text
        .strip_suffix('\n')
        .and_then(|t| t.rsplit_once('\n'))
        .map(|(head, _)| head)
        .expect("fixture manifest ends with its hash line")
        .replace("experimental = false", "experimental = true")
        .replace(
            "experimental_flag = null",
            "experimental_flag = \"x-drift\"",
        );
    let placeholder = format!("hash = \"sha256:{}\"", "0".repeat(64));
    std::fs::write(&manifest, format!("{body}\n{placeholder}\n")).unwrap();
    let hash = cwr_kernel::Build::compute_hash(&dst).unwrap();
    std::fs::write(&manifest, format!("{body}\nhash = \"sha256:{hash}\"\n")).unwrap();
    assert_eq!(cwr_kernel::Build::compute_hash(&dst).unwrap(), hash);
    dst
}

#[test]
fn drift_fires_on_a_refused_turn_and_replays() {
    let h = std::env::temp_dir().join(format!("cwr-kernel-drift-{}-home", std::process::id()));
    let _ = std::fs::remove_dir_all(&h);
    let build = drift_build("refused");
    let mut inst = Instance::create(
        &h,
        &build,
        CreateOptions {
            seed: Some(5),
            allow_experimental: true,
            ..Default::default()
        },
    )
    .unwrap();
    let id = inst.meta.id.clone();
    let wt0 = inst.clock.now();

    // Turn 1, the scheduled turn, is refused by the turn protocol: two fenced blocks.
    let message = "```python\na = 1\n```\n```python\nb = 2\n```\n";
    let violation = protocol::extract_script(message).unwrap_err();
    replay::record_refusal(&inst, &violation).unwrap();
    protocol::refuse(&mut inst, &violation).unwrap();
    let drift: Vec<&Value> = inst
        .records()
        .iter()
        .filter(|r| r["kind"] == "drift")
        .collect();
    assert_eq!(drift.len(), 1, "the injection is recorded either way");
    assert_eq!(drift[0]["turn"], json!(1));
    assert_eq!(drift[0]["first_turn"], json!(1));
    assert_eq!(drift[0]["primitive"], json!("kv.set"));
    let last = inst.records().last().unwrap();
    assert_eq!(last["kind"], json!("turn"));
    assert_eq!(last["primitive"], json!(protocol::PROTOCOL_PRIMITIVE));
    assert_eq!(
        inst.clock.now(),
        wt0,
        "a refused turn does not advance world time"
    );
    assert_eq!(inst.meta.status, "idle");

    // Turn 2 is a probe turn; the entry does not fire again.
    let calls = vec![ProbeCall {
        primitive: "world.now".into(),
        params: json!({}),
    }];
    replay::record_probe(&inst, &calls).unwrap();
    inst.probe(&calls, |_| {}).unwrap();
    assert_eq!(
        inst.records()
            .iter()
            .filter(|r| r["kind"] == "drift")
            .count(),
        1
    );
    let initial_hash = inst.records()[0]["initial_state"]
        .as_str()
        .unwrap()
        .to_string();
    drop(inst);

    let dir = Instance::dir_of(&h, &id);
    let initial: Value = serde_json::from_slice(
        &cwr_kernel::ObjectStore::new(dir.join("objects"))
            .get(&initial_hash)
            .unwrap(),
    )
    .unwrap();
    let inputs = ReplayInputs {
        ledger: dir.join("ledger.jsonl"),
        build_dir: build,
        initial,
        cassette: None,
        scripts: dir.join(replay::SCRIPTS_DIR),
        builds: Vec::new(),
        home: h.join("replayed"),
        into: None,
        opts: cwr_kernel::TurnOptions::default(),
    };
    let report = replay::replay(&inputs).unwrap();
    assert_eq!(report.divergence, None, "drift records replay identically");
    assert_eq!(report.turns, 2);
    let _ = std::fs::remove_dir_all(&h);
}
