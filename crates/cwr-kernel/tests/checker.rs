// Copyright Sidiora Labs Inc. CWR® 2026-2027
// SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0

//! The atomic contract check against the real world-code worker (kernel.md section 4): a spent
//! `primitive_calls` budget records one `LIMIT` and no more, a read_only effect that fails under
//! debug mode seals the Instance, and a primitive cut off by the turn's wall-clock deadline is
//! `LIMIT wall_ms`, not a Build defect.

use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

use cwr_kernel::contract_check::{Checker, ContractTable, LibWorker, NoGateway, SEAL_FILE};
use cwr_kernel::limits::Limits;
use cwr_kernel::{CreateOptions, Instance};
use serde_json::{json, Value};

const KV: &str = "# Copyright Sidiora Labs Inc. CWR® 2026-2027\n# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0\n\"\"\"Checker test module.\"\"\"\n\nimport time\n\nINITIAL_STATE = {\"items\": {}}\n\n\ndef get(state, key):\n    return state.module(\"kv\")[\"items\"].get(key, \"\")\n\n\ndef slow(state):\n    time.sleep(3)\n    return \"\"\n";

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

/// The fixture Build with a `kv` module of two read_only primitives: `kv.get`, whose declared
/// effect never holds, and `kv.slow`, which sleeps for three seconds of host time.
fn build(name: &str) -> PathBuf {
    let src = Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/minbuild");
    let dst = std::env::temp_dir().join(format!(
        "cwr-kernel-checker-{}-{name}-build",
        std::process::id()
    ));
    let _ = std::fs::remove_dir_all(&dst);
    copy(&src, &dst);
    std::fs::write(dst.join("lib/kv.py"), KV).unwrap();
    let mut c: Value =
        serde_json::from_slice(&std::fs::read(dst.join("contracts.json")).unwrap()).unwrap();
    c["primitives"] = json!([
        {"name": "kv.get", "module": "kv", "params": [["key", "str"]], "returns": "str",
         "side_effect": "read_only", "preconditions": [], "effects": ["result == \"never\""]},
        {"name": "kv.slow", "module": "kv", "params": [], "returns": "str",
         "side_effect": "read_only", "preconditions": [], "effects": []}
    ]);
    c["world"]["modules"] = json!(["kv"]);
    std::fs::write(dst.join("contracts.json"), serde_json::to_vec(&c).unwrap()).unwrap();
    let manifest = dst.join("manifest.kvx");
    let text = std::fs::read_to_string(&manifest).unwrap();
    let body = text
        .strip_suffix('\n')
        .and_then(|t| t.rsplit_once('\n'))
        .map(|(head, _)| head.to_string())
        .expect("fixture manifest ends with its hash line");
    let hash = cwr_kernel::Build::compute_hash(&dst).unwrap();
    std::fs::write(&manifest, format!("{body}\nhash = \"sha256:{hash}\"\n")).unwrap();
    assert_eq!(cwr_kernel::Build::compute_hash(&dst).unwrap(), hash);
    dst
}

fn instance(name: &str) -> Instance {
    let h = std::env::temp_dir().join(format!(
        "cwr-kernel-checker-{}-{name}-home",
        std::process::id()
    ));
    let _ = std::fs::remove_dir_all(&h);
    Instance::create(&h, &build(name), CreateOptions::default()).unwrap()
}

fn limits(primitive_calls: u64) -> Limits {
    Limits {
        cpu_ms: 5000,
        wall_ms: 30000,
        memory_mb: 256,
        primitive_calls,
        adapter_calls: 1,
    }
}

#[test]
fn refusals_past_the_call_budget_record_one_limit_and_no_more() {
    let mut inst = instance("budget");
    let table = ContractTable::load(&inst.build).unwrap();
    let mut worker = LibWorker::spawn(&inst.build, &table, &inst.state, 1).unwrap();
    let turn = inst.begin_turn().unwrap();
    let before = inst.records().len();
    let mut gw = NoGateway;
    let mut c = Checker::new(
        &mut inst,
        &mut worker,
        &table,
        &mut gw,
        limits(2),
        false,
        turn,
    );
    for _ in 0..200 {
        let r = c.closure("sandbox.import", "import time").unwrap();
        assert!(!r.seal);
    }
    let r = c.call("kv.get", &json!({"key": "a"})).unwrap();
    assert_eq!(r.result["error"]["limit"], json!("primitive_calls"));
    drop(c);
    let added: Vec<&Value> = inst.records()[before..].iter().collect();
    assert_eq!(
        added.len(),
        3,
        "two refusals within budget and one LIMIT record"
    );
    assert_eq!(added[2]["outcome"]["code"], json!("LIMIT"));
    assert_eq!(added[2]["limit"], json!("primitive_calls"));
}

#[test]
fn a_read_only_effect_failure_seals_the_instance() {
    let mut inst = instance("effect");
    let table = ContractTable::load(&inst.build).unwrap();
    let mut worker = LibWorker::spawn(&inst.build, &table, &inst.state, 1).unwrap();
    let turn = inst.begin_turn().unwrap();
    let mut gw = NoGateway;
    let mut c = Checker::new(
        &mut inst,
        &mut worker,
        &table,
        &mut gw,
        limits(10),
        true,
        turn,
    );
    let r = c.call("kv.get", &json!({"key": "a"})).unwrap();
    assert!(
        r.seal,
        "a step-4 effect failure seals whatever the side effect"
    );
    assert_eq!(r.result["error"]["code"], json!("INVARIANT_VIOLATION"));
    drop(c);
    assert!(inst.dir.join(SEAL_FILE).is_file());
    let last = inst.records().last().unwrap();
    assert_eq!(last["sealed"], json!(true));
}

#[test]
fn a_primitive_past_the_turn_deadline_is_limit_wall_ms_not_a_defect() {
    let mut inst = instance("wall");
    let table = ContractTable::load(&inst.build).unwrap();
    let mut worker = LibWorker::spawn(&inst.build, &table, &inst.state, 1).unwrap();
    let turn = inst.begin_turn().unwrap();
    let wt = inst.clock.now();
    let mut gw = NoGateway;
    let mut c = Checker::new(
        &mut inst,
        &mut worker,
        &table,
        &mut gw,
        limits(10),
        false,
        turn,
    );
    c.set_deadline(Instant::now() + Duration::from_millis(400));
    let started = Instant::now();
    let r = c.call("kv.slow", &json!({})).unwrap();
    assert!(
        started.elapsed() < Duration::from_millis(2500),
        "the worker was cut off"
    );
    assert!(r.wall_expired && !r.seal);
    assert_eq!(r.result["error"]["limit"], json!("wall_ms"));
    drop(c);
    assert!(!inst.dir.join(SEAL_FILE).exists());
    assert_eq!(
        inst.clock.now(),
        wt,
        "a call that did not complete does not tick"
    );
    let last = inst.records().last().unwrap();
    assert_eq!(last["outcome"]["code"], json!("LIMIT"));
    assert_eq!(last["limit"], json!("wall_ms"));
}
