// Copyright Sidiora Labs Inc. CWR® 2026-2027
// SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0

//! End-to-end regression tests for kernel review findings, run through the real `cwr` binary
//! against Builds compiled from copies of examples/refworld:
//!
//! * malformed `$bytes` values reach no world code: they are `INVALID` at step 1 and the Instance
//!   is not sealed;
//! * a primitive still running when the turn's `wall_ms` passes ends the turn with
//!   `LIMIT wall_ms`, not a defect, and the Instance keeps working;
//! * on an `--x-drift` Build a drift entry scheduled for a turn the protocol refuses is recorded
//!   in that turn, and a ledger with drift records and protocol refusals replays identically;
//! * a declared effect that fails under `--debug-effects` seals the Instance even for a read_only
//!   primitive;
//! * the context (component 4, the last envelope) still assembles after a restore to an earlier
//!   snapshot.
//!
//! Each test needs bubblewrap and Python 3.12, like every sandboxed turn.

use std::path::{Path, PathBuf};
use std::process::{Command, Output};

use serde_json::Value;

const CWR: &str = env!("CARGO_BIN_EXE_cwr");

fn repo() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../..")
}

fn scratch(name: &str) -> PathBuf {
    let d = std::env::temp_dir().join(format!("cwr-kreg-{}-{name}", std::process::id()));
    let _ = std::fs::remove_dir_all(&d);
    std::fs::create_dir_all(&d).expect("scratch dir");
    d
}

fn cwr<I, S>(args: I) -> Output
where
    I: IntoIterator<Item = S>,
    S: AsRef<std::ffi::OsStr>,
{
    Command::new(CWR).args(args).output().expect("run cwr")
}

fn copy_tree(src: &Path, dst: &Path) {
    std::fs::create_dir_all(dst).expect("mkdir");
    for e in std::fs::read_dir(src).expect("read dir") {
        let e = e.expect("entry");
        let name = e.file_name();
        if name == "__pycache__" || name == "README.md" {
            continue;
        }
        let to = dst.join(&name);
        if e.file_type().expect("type").is_dir() {
            copy_tree(&e.path(), &to);
        } else {
            std::fs::copy(e.path(), &to).expect("copy");
        }
    }
}

/// A copy of examples/refworld under `dir/src`, after `edit` ran on it.
fn world_source(dir: &Path, edit: impl FnOnce(&Path)) -> PathBuf {
    let src = dir.join("src");
    copy_tree(&repo().join("examples/refworld"), &src);
    edit(&src);
    src
}

/// Compiles `src` (with `extra` flags) and returns the Build directory.
fn compile(dir: &Path, src: &Path, extra: &[&str]) -> PathBuf {
    let out = dir.join("builds");
    let mut args: Vec<std::ffi::OsString> = vec![
        "build".into(),
        src.into(),
        "--json".into(),
        "--out".into(),
        out.into(),
    ];
    args.extend(extra.iter().map(Into::into));
    let p = cwr(&args);
    let rep: Value = serde_json::from_slice(&p.stdout).unwrap_or(Value::Null);
    let built = rep["build"]["dir"].as_str().map(PathBuf::from);
    built.unwrap_or_else(|| {
        panic!(
            "cwr build failed ({:?}): {}\n{}",
            p.status.code(),
            String::from_utf8_lossy(&p.stdout),
            String::from_utf8_lossy(&p.stderr)
        )
    })
}

fn create(home: &Path, build: &Path, extra: &[&str]) -> String {
    let mut args: Vec<std::ffi::OsString> = vec![
        "instance".into(),
        "create".into(),
        build.into(),
        "--home".into(),
        home.into(),
        "--seed".into(),
        "7".into(),
    ];
    args.extend(extra.iter().map(Into::into));
    let p = cwr(&args);
    assert!(
        p.status.success(),
        "create: {}",
        String::from_utf8_lossy(&p.stderr)
    );
    String::from_utf8_lossy(&p.stdout).trim().to_string()
}

/// One turn from a file: `--script` or `--message`. Returns the exit code and the envelope.
fn turn(home: &Path, id: &str, how: &str, text: &str, n: usize) -> (i32, Value) {
    let f = home.join(format!("input-{n}.txt"));
    std::fs::write(&f, text).expect("write input");
    let p = cwr([
        "instance".as_ref(),
        "turn".as_ref(),
        id.as_ref(),
        "--home".as_ref(),
        home.as_os_str(),
        how.as_ref(),
        f.as_os_str(),
    ]);
    let env: Value = serde_json::from_slice(&p.stdout).unwrap_or_else(|_| {
        panic!(
            "turn {n} printed no envelope ({:?}): {}",
            p.status.code(),
            String::from_utf8_lossy(&p.stderr)
        )
    });
    (p.status.code().unwrap_or(-1), env)
}

fn ledger(home: &Path, id: &str) -> Vec<Value> {
    std::fs::read_to_string(home.join("instances").join(id).join("ledger.jsonl"))
        .expect("ledger")
        .lines()
        .map(|l| serde_json::from_str(l).expect("record"))
        .collect()
}

#[test]
fn malformed_bytes_are_invalid_and_never_seal_the_instance() {
    let dir = scratch("bytes");
    let src = world_source(&dir, |_| {});
    let build = compile(&dir, &src, &[]);
    let home = dir.join("home");
    let id = create(&home, &build, &[]);
    let script = "import cwr\nout = []\nfor b in ['=', 'A', 'AAE', 'AA==AAAA']:\n    try:\n        cwr._rpc.call('fs.write', {'path': '/docs/x', 'content': {'$bytes': b}})\n        out.append('ok')\n    except Exception as e:\n        out.append(getattr(e, 'code', type(e).__name__))\nreturn out\n";
    let (_, env) = turn(&home, &id, "--script", script, 1);
    assert_eq!(
        env["returned"],
        serde_json::json!(["INVALID", "INVALID", "INVALID", "INVALID"]),
        "{env}"
    );
    let recs = ledger(&home, &id);
    assert!(
        recs.iter()
            .all(|r| r["outcome"]["code"] != "INVARIANT_VIOLATION"),
        "no defect is recorded"
    );
    assert!(!home
        .join("instances")
        .join(&id)
        .join("sealed.json")
        .exists());
    let (_, env) = turn(
        &home,
        &id,
        "--script",
        "import world\nreturn world.now()\n",
        2,
    );
    assert_eq!(env["status"], "ok", "the Instance keeps working: {env}");
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn a_primitive_past_the_wall_deadline_is_a_wall_limit_not_a_defect() {
    let dir = scratch("wall");
    let src = world_source(&dir, |src| {
        let lib = src.join("lib/fs.py");
        let text = std::fs::read_to_string(&lib).expect("fs.py");
        let head = "def list(state: Any, prefix: str) -> list:\n";
        assert!(text.contains(head), "refworld fs.list signature changed");
        std::fs::write(
            &lib,
            text.replace(
                head,
                &format!("{head}    while prefix == \"/spin\":\n        pass\n"),
            ),
        )
        .expect("write fs.py");
        let world = src.join("world.kvx");
        let w = std::fs::read_to_string(&world).expect("world.kvx");
        assert!(w.contains("wall_ms = 30000"), "refworld wall_ms changed");
        std::fs::write(&world, w.replace("wall_ms = 30000", "wall_ms = 8000"))
            .expect("write world.kvx");
    });
    let build = compile(&dir, &src, &[]);
    let home = dir.join("home");
    let id = create(&home, &build, &[]);
    let started = std::time::Instant::now();
    let (_, env) = turn(
        &home,
        &id,
        "--script",
        "import fs\nreturn fs.list('/spin')\n",
        1,
    );
    assert!(
        started.elapsed() < std::time::Duration::from_secs(25),
        "the kernel stopped waiting at the turn deadline"
    );
    assert_eq!(env["error"]["code"], "LIMIT", "{env}");
    assert_eq!(env["error"]["limit"], "wall_ms", "{env}");
    assert!(!home
        .join("instances")
        .join(&id)
        .join("sealed.json")
        .exists());
    let recs = ledger(&home, &id);
    let listed = recs
        .iter()
        .find(|r| r["primitive"] == "fs.list")
        .expect("the cut-off call is recorded");
    assert_eq!(listed["outcome"]["code"], "LIMIT");
    assert_eq!(listed["limit"], "wall_ms");
    let (_, env) = turn(
        &home,
        &id,
        "--script",
        "import world\nreturn world.now()\n",
        2,
    );
    assert_eq!(env["status"], "ok", "the Instance keeps working: {env}");
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn drift_on_a_refused_turn_is_recorded_and_the_ledger_replays() {
    let dir = scratch("drift");
    let src = world_source(&dir, |_| {});
    let schedule = dir.join("drift.kvx");
    std::fs::write(
        &schedule,
        "[meta]\nformat = \"cwr.drift\"\nschema_version = 1\n\n[drift.1]\nturn = 2\nprimitive = \"fs.delete\"\nchange = \"precondition_added\"\nexpr = \"not fs.locked(path) and fs.size(path) < 4096\"\nfailure = \"LOCKED\"\ndoc = \"From turn 2 large files refuse deletion.\"\n",
    )
    .expect("schedule");
    let build = compile(
        &dir,
        &src,
        &["--x-drift", schedule.to_str().expect("utf-8")],
    );
    let home = dir.join("home");
    let id = create(&home, &build, &["--allow-experimental"]);
    let ok = "Reading the clock.\n\n```python\nimport world\nreturn world.now()\n```\n";
    let refused = "```python\nimport world\n```\n\n```python\nreturn 1\n```\n";
    assert_eq!(turn(&home, &id, "--message", ok, 1).0, 0);
    let (code, env) = turn(&home, &id, "--message", refused, 2);
    assert_eq!(code, 4, "a two-block message is refused: {env}");
    assert_eq!(env["error"]["primitive"], "turn.protocol");
    assert_eq!(turn(&home, &id, "--message", ok, 3).0, 0);
    let recs = ledger(&home, &id);
    let drift = recs
        .iter()
        .position(|r| r["kind"] == "drift")
        .expect("the drift scheduled for the refused turn is recorded");
    let refusal = recs
        .iter()
        .position(|r| r["kind"] == "turn" && r["primitive"] == "turn.protocol")
        .expect("refusal record");
    assert_eq!(recs[drift]["turn"], serde_json::json!(2));
    assert!(
        drift < refusal,
        "the drift record precedes the refusal record"
    );
    assert_eq!(
        recs.iter().filter(|r| r["kind"] == "drift").count(),
        1,
        "the injection is recorded once, at its scheduled turn"
    );
    let into = dir.join("replayed");
    let p = cwr([
        "instance".as_ref(),
        "replay".as_ref(),
        id.as_ref(),
        "--home".as_ref(),
        home.as_os_str(),
        "--into-home".as_ref(),
        into.as_os_str(),
    ]);
    assert!(
        p.status.success(),
        "replay of drift and refusal turns is identical ({:?}): {}\n{}",
        p.status.code(),
        String::from_utf8_lossy(&p.stdout),
        String::from_utf8_lossy(&p.stderr)
    );
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn the_context_assembles_after_a_restore_to_an_earlier_snapshot() {
    let dir = scratch("restore");
    let src = world_source(&dir, |_| {});
    let build = compile(&dir, &src, &[]);
    let home = dir.join("home");
    let id = create(&home, &build, &[]);
    let (_, first) = turn(&home, &id, "--script", "import world\nreturn 'first'\n", 1);
    let snap = cwr([
        "instance".as_ref(),
        "snapshot".as_ref(),
        id.as_ref(),
        "--home".as_ref(),
        home.as_os_str(),
    ]);
    assert!(snap.status.success());
    let snap = String::from_utf8_lossy(&snap.stdout).trim().to_string();
    turn(&home, &id, "--script", "import world\nreturn 'second'\n", 2);
    let r = cwr([
        "instance".as_ref(),
        "restore".as_ref(),
        id.as_ref(),
        snap.as_ref(),
        "--home".as_ref(),
        home.as_os_str(),
    ]);
    assert!(
        r.status.success(),
        "restore: {}",
        String::from_utf8_lossy(&r.stderr)
    );
    let ctx = cwr([
        "instance".as_ref(),
        "context".as_ref(),
        id.as_ref(),
        "--home".as_ref(),
        home.as_os_str(),
    ]);
    assert!(
        ctx.status.success(),
        "context after restore: {}",
        String::from_utf8_lossy(&ctx.stderr)
    );
    let text = String::from_utf8_lossy(&ctx.stdout);
    assert!(
        text.contains("\"first\""),
        "the restored turn's envelope is shown"
    );
    assert!(
        !text.contains("\"second\""),
        "the abandoned turn's envelope is not"
    );
    assert_eq!(first["returned"], "first");
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn a_read_only_effect_failure_under_debug_effects_seals_the_instance() {
    let dir = scratch("seal");
    // The compiler refuses declared effects on a read_only primitive; an `--x-drift` entry of kind
    // effect_changed is how one reaches a read_only primitive's step 4.
    let src = world_source(&dir, |_| {});
    let schedule = dir.join("drift.kvx");
    std::fs::write(
        &schedule,
        "[meta]\nformat = \"cwr.drift\"\nschema_version = 1\n\n[drift.1]\nturn = 1\nprimitive = \"fs.list\"\nchange = \"effect_changed\"\nexpr = \"prefix != \\\"/lie\\\"\"\nfailure = \"INVALID\"\ndoc = \"fs.list asserts it is never asked for /lie.\"\n",
    )
    .expect("schedule");
    let build = compile(
        &dir,
        &src,
        &["--x-drift", schedule.to_str().expect("utf-8")],
    );
    let home = dir.join("home");
    let id = create(&home, &build, &["--allow-experimental"]);
    let f = home.join("lie.py");
    std::fs::write(&f, "import fs\nreturn fs.list('/lie')\n").expect("script");
    let p = cwr([
        "instance".as_ref(),
        "turn".as_ref(),
        id.as_ref(),
        "--home".as_ref(),
        home.as_os_str(),
        "--script".as_ref(),
        f.as_os_str(),
        "--debug-effects".as_ref(),
    ]);
    let env: Value = serde_json::from_slice(&p.stdout).expect("envelope");
    assert_eq!(env["error"]["code"], "INVARIANT_VIOLATION", "{env}");
    assert!(
        home.join("instances")
            .join(&id)
            .join("sealed.json")
            .exists(),
        "a read_only effect failure seals the Instance like any defect"
    );
    let rec = ledger(&home, &id)
        .into_iter()
        .find(|r| r["kind"] == "primitive" && r["primitive"] == "fs.list")
        .expect("record");
    assert_eq!(rec["sealed"], serde_json::json!(true));
    let f2 = home.join("after.py");
    std::fs::write(&f2, "import world\nreturn world.now()\n").expect("script");
    let after = cwr([
        "instance".as_ref(),
        "turn".as_ref(),
        id.as_ref(),
        "--home".as_ref(),
        home.as_os_str(),
        "--script".as_ref(),
        f2.as_os_str(),
    ]);
    assert!(
        !after.status.success(),
        "a sealed Instance runs no more turns"
    );
    let _ = std::fs::remove_dir_all(&dir);
}
