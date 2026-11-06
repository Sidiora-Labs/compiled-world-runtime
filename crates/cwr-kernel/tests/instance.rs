// Copyright Sidiora Labs Inc. CWR® 2026-2027
// SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0

//! Instance store integration test against the hand-written fixture Build (task 2.1).
//! Mutates module state through the library API (the sandboxed executor is task 2.2), then
//! snapshots, mutates further, restores and checks the Instance is observably identical.

use std::path::{Path, PathBuf};

use cwr_kernel::{
    CreateOptions, Instance, KernelError, Kind, Outcome, ProbeCall, RecordDraft, TailSize,
};
use serde_json::{json, Value};

/// The hand-written fixture Build, packaged for one test the way the compiler packages a Build
/// (compiler.md section 7): its source tree is copied and the recorded hash is computed over the
/// copy. Repository-wide edits to the source files (licence headers, formatting) therefore never
/// leave the tests reading a stale pin; tamper detection is exercised by the case driver.
fn fixture_for(name: &str) -> PathBuf {
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
    let src = Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/minbuild");
    let dst =
        std::env::temp_dir().join(format!("cwr-kernel-it-{}-{name}-build", std::process::id()));
    let _ = std::fs::remove_dir_all(&dst);
    copy(&src, &dst);
    let manifest = dst.join("manifest.kvx");
    let text = std::fs::read_to_string(&manifest).unwrap();
    let body = text
        .strip_suffix('\n')
        .and_then(|t| t.rsplit_once('\n'))
        .map(|(head, _)| head)
        .expect("fixture manifest ends with its hash line");
    let hash = cwr_kernel::Build::compute_hash(&dst).unwrap();
    std::fs::write(&manifest, format!("{body}\nhash = \"sha256:{hash}\"\n")).unwrap();
    assert_eq!(cwr_kernel::Build::compute_hash(&dst).unwrap(), hash);
    dst
}

fn home(name: &str) -> PathBuf {
    let d = std::env::temp_dir().join(format!("cwr-kernel-it-{}-{name}", std::process::id()));
    let _ = std::fs::remove_dir_all(&d);
    std::fs::create_dir_all(&d).unwrap();
    d
}

fn kv_set(inst: &mut Instance, key: &str, value: &str) {
    let turn = inst.current_turn().expect("in turn");
    let mut state = inst.state.clone();
    let mut kv = state.get("kv").cloned().unwrap();
    kv.as_object_mut()
        .unwrap()
        .entry("items")
        .or_insert(json!({}))[key] = json!(value);
    state.set("kv", kv).unwrap();
    inst.clock.advance_primitive().unwrap();
    let mut d = RecordDraft::new(Kind::Primitive, turn, inst.clock.now());
    d.primitive = Some("kv.set".into());
    d.params_hash = Some(cwr_kernel::params_hash(
        &json!({"key": key, "value": value}),
    ));
    d.outcome = Some(Outcome::ok("None", "mutating"));
    inst.append(d, Some(state)).unwrap();
}

fn observe(inst: &Instance) -> Value {
    json!({
        "state": inst.state.to_json(),
        "world_time": inst.clock.now(),
        "turn": inst.meta.turn,
        "rng": inst.rng.state().to_json(),
        "tail_size": inst.meta.tail_size.to_json(),
        "build": inst.meta.build,
        "seed": inst.meta.seed,
    })
}

#[test]
fn snapshot_restore_is_observably_identical() {
    let h = home("restore");
    let mut inst = Instance::create(
        &h,
        &fixture_for("restore"),
        CreateOptions {
            seed: Some(42),
            tail: Some(TailSize::Last(5)),
            ..Default::default()
        },
    )
    .unwrap();
    let id = inst.meta.id.clone();
    inst.begin_turn().unwrap();
    kv_set(&mut inst, "a", "1");
    inst.end_turn("ok", None, 1, &Value::Null, json!({}))
        .unwrap();
    inst.probe(
        &[ProbeCall {
            primitive: "world.random".into(),
            params: json!({}),
        }],
        |_| {},
    )
    .unwrap();
    let snap = inst.snapshot().unwrap();
    let at_snap = observe(&inst);
    let seq_at_snap = inst.meta.seq;
    let tail_at_snap = inst.tail();

    // Mutate further: state, clock, RNG.
    inst.begin_turn().unwrap();
    kv_set(&mut inst, "a", "2");
    kv_set(&mut inst, "b", "3");
    inst.end_turn("ok", None, 2, &Value::Null, json!({}))
        .unwrap();
    let after = inst
        .probe(
            &[ProbeCall {
                primitive: "world.random".into(),
                params: json!({}),
            }],
            |_| {},
        )
        .unwrap();
    assert_ne!(observe(&inst), at_snap);
    drop(inst);

    let (inst, rec) = Instance::restore(&h, &id, &snap).unwrap();
    assert_eq!(observe(&inst), at_snap);
    assert_eq!(rec["kind"], "recovery");
    assert_eq!(rec["resumed_from"], json!(seq_at_snap));
    assert_eq!(rec["seq"], json!(seq_at_snap + 1));
    assert!(inst.tail().starts_with(&tail_at_snap) || inst.tail().lines().count() <= 5);
    drop(inst);

    // Reopen from disk and redo the same draw: equal to the abandoned branch's draw.
    let mut inst = Instance::open(&h, &id).unwrap();
    assert_eq!(observe(&inst), at_snap);
    let again = inst
        .probe(
            &[ProbeCall {
                primitive: "world.random".into(),
                params: json!({}),
            }],
            |_| {},
        )
        .unwrap();
    assert_eq!(again["returned"], after["returned"]);
}

#[test]
fn module_state_outside_build_and_unknown_module_refused() {
    let h = home("modules");
    let mut inst = Instance::create(&h, &fixture_for("modules"), CreateOptions::default()).unwrap();
    let mut s = inst.state.clone();
    assert!(s.set("fs", json!({})).is_err());
    assert!(s.set("kv", json!(3)).is_err());
    assert!(!inst.dir.starts_with(&inst.build.dir));
    inst.begin_turn().unwrap();
    kv_set(&mut inst, "x", "y");
    let obj = inst
        .dir
        .join("objects")
        .join(inst.meta.state_hash.trim_start_matches("sha256:"));
    assert!(obj.is_file());
    let id = inst.meta.id.clone();
    drop(inst);
    // A second open while in_turn is allowed to read but a new turn is refused (CONFLICT).
    let mut inst = Instance::open(&h, &id).unwrap();
    assert!(matches!(inst.begin_turn(), Err(KernelError::Conflict(_))));
}

#[test]
fn concurrent_open_refused() {
    let h = home("lock");
    let inst = Instance::create(&h, &fixture_for("lock"), CreateOptions::default()).unwrap();
    let refused = Instance::open(&h, &inst.meta.id);
    assert!(matches!(refused, Err(KernelError::Conflict(_))));
    assert_eq!(
        refused.map(|_| ()).unwrap_err().exit_code(),
        cwr_kernel::EXIT_CONFLICT
    );
}

#[test]
fn replay_reproduces_protocol_refusals_and_probe_turns() {
    use cwr_kernel::replay::{self, ReplayInputs};
    use cwr_kernel::surface::protocol;

    let h = home("replay");
    let build = fixture_for("replay");
    let mut inst = Instance::create(
        &h,
        &build,
        CreateOptions {
            seed: Some(3),
            ..Default::default()
        },
    )
    .unwrap();
    let id = inst.meta.id.clone();
    let message = "Two plans.\n```python\nx = 1\n```\n```python\ny = 2\n```\n";
    let violation = protocol::extract_script(message).unwrap_err();
    replay::record_refusal(&inst, &violation).unwrap();
    let refused = protocol::refuse(&mut inst, &violation).unwrap();
    assert_eq!(refused["error"]["primitive"], protocol::PROTOCOL_PRIMITIVE);
    let calls = vec![
        ProbeCall {
            primitive: "world.random".into(),
            params: json!({}),
        },
        ProbeCall {
            primitive: "world.now".into(),
            params: json!({}),
        },
    ];
    replay::record_probe(&inst, &calls).unwrap();
    let probed = inst.probe(&calls, |_| {}).unwrap();
    assert_eq!(probed["status"], "ok");
    assert_eq!(probed["resources"]["cpu_ms"], 0);
    assert_eq!(probed["trace"][0]["content_guaranteed"], true);
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
        build_dir: build.clone(),
        initial,
        cassette: None,
        scripts: dir.join(replay::SCRIPTS_DIR),
        builds: Vec::new(),
        home: h.join("replayed"),
        into: None,
        opts: cwr_kernel::TurnOptions::default(),
    };
    let report = replay::replay(&inputs).unwrap();
    assert_eq!(report.divergence, None);
    assert_eq!(report.turns, 2);

    // The marker holds the rule broken, never the message text.
    let marker = replay::refusal_file(&dir.join(replay::SCRIPTS_DIR), 1);
    assert!(!std::fs::read_to_string(&marker)
        .unwrap()
        .contains("Two plans"));
    // Without the marker the refusal is named, not reported as a missing file.
    std::fs::remove_file(marker).unwrap();
    match replay::replay(&inputs) {
        Err(KernelError::Refused(m)) => assert!(m.contains("turn 1 was a protocol refusal"), "{m}"),
        other => panic!("expected a named refusal, got {other:?}"),
    }
}

#[test]
fn operator_ids_are_ulids_and_destroy_needs_idle() {
    let h = home("ids");
    for bad in ["../x", "a/b", "", "01M6RJAB"] {
        assert!(matches!(
            Instance::open(&h, bad),
            Err(KernelError::Refused(_))
        ));
        assert!(matches!(
            Instance::destroy(&h, bad),
            Err(KernelError::Refused(_))
        ));
    }
    let mut inst = Instance::create(&h, &fixture_for("ids"), CreateOptions::default()).unwrap();
    let id = inst.meta.id.clone();
    assert!(cwr_kernel::is_ulid(&id));
    inst.begin_turn().unwrap();
    kv_set(&mut inst, "a", "1");
    drop(inst);
    // Left `in_turn`: destroy refuses until recovery has run.
    assert!(matches!(
        Instance::destroy(&h, &id),
        Err(KernelError::Conflict(_))
    ));
    let dir = Instance::dir_of(&h, &id);
    assert!(dir.join("objects").is_dir());
}

#[test]
fn replay_reapplies_a_restore_and_reads_only_the_live_turn_input() {
    use cwr_kernel::replay::{self, ReplayInputs};
    use cwr_kernel::surface::protocol;

    let h = home("replay-restore");
    let build = fixture_for("replay-restore");
    let mut inst = Instance::create(
        &h,
        &build,
        CreateOptions {
            seed: Some(5),
            ..Default::default()
        },
    )
    .unwrap();
    let id = inst.meta.id.clone();
    let random = vec![ProbeCall {
        primitive: "world.random".into(),
        params: json!({}),
    }];
    replay::record_probe(&inst, &random).unwrap();
    inst.probe(&random, |_| {}).unwrap();
    let snap = inst.snapshot().unwrap();

    // A refused probe records its calls before the kernel refuses them; its turn never begins.
    let bad = vec![ProbeCall {
        primitive: "world.nothing".into(),
        params: json!({}),
    }];
    let refused_probe = replay::record_probe(&inst, &bad).unwrap();
    assert!(inst.probe(&bad, |_| {}).is_err());
    assert_eq!(inst.meta.turn, 1);
    // Turn 2 is a protocol refusal, which the restore below abandons.
    let violation = protocol::extract_script("no code here").unwrap_err();
    let marker = replay::record_refusal(&inst, &violation).unwrap();
    assert!(
        !refused_probe.exists(),
        "one recorded input per turn number"
    );
    protocol::refuse(&mut inst, &violation).unwrap();
    drop(inst);
    let (mut inst, rec) = Instance::restore(&h, &id, &snap).unwrap();
    assert_eq!(rec["abandoned_from"], json!(3));
    // The live turn 2 is a probe; recording it removes the abandoned turn's refusal marker.
    replay::record_probe(&inst, &random).unwrap();
    assert!(!marker.exists(), "the abandoned turn's input is superseded");
    inst.probe(&random, |_| {}).unwrap();
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
        build_dir: build.clone(),
        initial,
        cassette: None,
        scripts: dir.join(replay::SCRIPTS_DIR),
        builds: Vec::new(),
        home: h.join("replayed"),
        into: None,
        opts: cwr_kernel::TurnOptions::default(),
    };
    let report = replay::replay(&inputs).unwrap();
    assert_eq!(report.divergence, None);
    assert_eq!(report.turns, 2);
    // The replay's recovery record names its own snapshot (which carries its own Instance id)
    // and abandons nothing, so its raw chain differs; under the original identity it is equal.
    let original = Instance::open(&h, &id).unwrap();
    let replayed = Instance::open(&h.join("replayed"), &report.into).unwrap();
    assert_eq!(replayed.records()[3]["kind"], "recovery");
    assert_eq!(replayed.records()[3]["abandoned_from"], Value::Null);
    let rewritten = replay::as_original(original.records(), &id, &replayed).unwrap();
    assert_eq!(rewritten[3]["snapshot"], original.records()[3]["snapshot"]);
    assert_eq!(
        rewritten.last().unwrap()["chain"],
        original.records().last().unwrap()["chain"]
    );
}
