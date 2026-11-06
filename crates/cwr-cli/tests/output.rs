// Copyright Sidiora Labs Inc. CWR® 2026-2027
// SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0

//! The binary never panics on a closed or full stdout: it exits 1, quietly for a closed pipe and
//! with one message for a write failure, and never reports success for output it lost.

use std::process::{Command, Stdio};

fn cwr() -> Command {
    Command::new(env!("CARGO_BIN_EXE_cwr"))
}

#[test]
fn a_closed_stdout_pipe_exits_one_quietly() {
    let (reader, writer) = std::io::pipe().expect("pipe");
    drop(reader);
    for args in [&["contract", "codes"][..], &["help"]] {
        let out = cwr()
            .args(args)
            .stdout(writer.try_clone().expect("clone"))
            .stderr(Stdio::piped())
            .output()
            .expect("run cwr");
        let stderr = String::from_utf8_lossy(&out.stderr);
        assert_eq!(out.status.code(), Some(1), "{args:?}: {stderr}");
        assert!(!stderr.contains("panicked"), "{args:?}: {stderr}");
        assert!(stderr.is_empty(), "{args:?}: {stderr}");
    }
}

#[test]
fn a_full_stdout_exits_one_with_a_message() {
    let Ok(full) = std::fs::OpenOptions::new().write(true).open("/dev/full") else {
        return;
    };
    let out = cwr()
        .args(["contract", "codes"])
        .stdout(full)
        .stderr(Stdio::piped())
        .output()
        .expect("run cwr");
    let stderr = String::from_utf8_lossy(&out.stderr);
    assert_eq!(out.status.code(), Some(1), "{stderr}");
    assert!(!stderr.contains("panicked"), "{stderr}");
    assert!(stderr.contains("cannot write to stdout"), "{stderr}");
}

#[test]
fn help_and_usage_exit_codes() {
    assert_eq!(
        cwr()
            .arg("help")
            .stdout(Stdio::null())
            .status()
            .ok()
            .and_then(|s| s.code()),
        Some(0)
    );
    assert_eq!(
        cwr()
            .stderr(Stdio::null())
            .status()
            .ok()
            .and_then(|s| s.code()),
        Some(1)
    );
    assert_eq!(
        cwr()
            .arg("nope")
            .stderr(Stdio::null())
            .status()
            .ok()
            .and_then(|s| s.code()),
        Some(1)
    );
}
