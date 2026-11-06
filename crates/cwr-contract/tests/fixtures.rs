// Copyright Sidiora Labs Inc. CWR® 2026-2027
// SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0

//! Integration tests over tests/fixtures: the valid World Source is accepted and
//! round-trips through JSON and canonical KVX; every malformed class is rejected with
//! exactly the located errors recorded in its case.json; the pack's illustrative source
//! under spec/cwr/examples/refworld is accepted and round-trips.

use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};

use cwr_contract::{
    AdapterFile, Code, ContractFile, DriftFile, ErrorKind, SourceTree, WorldSource,
};

fn fixtures() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures")
}

fn read_tree(dir: &Path) -> SourceTree {
    SourceTree::read_dir(dir).unwrap_or_else(|e| panic!("{}: {e}", dir.display()))
}

fn overlay(base: &SourceTree, case_dir: &Path, remove: &[String]) -> SourceTree {
    let mut tree = base.clone();
    for path in remove {
        assert!(
            tree.files.remove(path).is_some(),
            "remove {path} not in valid tree"
        );
    }
    let files_dir = case_dir.join("files");
    if files_dir.exists() {
        for (path, bytes) in read_tree(&files_dir).files {
            tree.files.insert(path, bytes);
        }
    }
    tree
}

fn assert_round_trips(src: &WorldSource, tree: &SourceTree) {
    // JSON round trip.
    let json = src.to_json().expect("model serialises");
    let back = WorldSource::from_json(&json).expect("JSON model re-parses");
    assert_eq!(&back, src);
    assert_eq!(back.to_json().expect("model serialises"), json);
    // Canonical KVX round trip: replace every KVX file by its canonical rendering.
    let mut emitted = tree.clone();
    for (path, text) in src.to_kvx_files() {
        assert!(
            emitted.files.contains_key(&path),
            "emitted unknown file {path}"
        );
        emitted.files.insert(path, Some(text.into_bytes()));
    }
    let reparsed =
        WorldSource::load_tree(&emitted).unwrap_or_else(|e| panic!("canonical KVX rejected:\n{e}"));
    assert_eq!(&reparsed, src);
    // Canonical rendering is a fixed point.
    assert_eq!(reparsed.to_kvx_files(), src.to_kvx_files());
}

#[test]
fn valid_fixture_is_accepted_and_round_trips() {
    let tree = read_tree(&fixtures().join("valid"));
    let src =
        WorldSource::load_tree(&tree).unwrap_or_else(|e| panic!("valid fixture rejected:\n{e}"));
    assert_eq!(src.world.world.name, "fixtureworld");
    assert_eq!(src.world.limits.memory_mb.get(), 256);
    let set = src.primitives().find(|p| p.name == "kv.set").unwrap();
    assert_eq!(
        set.params.names().collect::<Vec<_>>(),
        ["ns", "key", "value"]
    );
    assert_eq!(set.preconditions[1].code(), cwr_contract::Code::Limit);
    assert_eq!(src.adapter_contracts().count(), 1);
    let query = src.adapter_contracts().next().unwrap();
    assert_eq!(query.undeclared_status, Some(Code::Invalid));
    assert_eq!(query.retry_ratio(), 7);
    assert_eq!(src.migrations[0].changes, ["kv_namespaces"]);
    assert_round_trips(&src, &tree);
}

#[test]
fn pack_example_is_accepted_and_round_trips() {
    let dir = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../spec/cwr/examples/refworld");
    let tree = read_tree(&dir);
    let src =
        WorldSource::load_tree(&tree).unwrap_or_else(|e| panic!("pack example rejected:\n{e}"));
    assert!(src.is_example());
    assert_round_trips(&src, &tree);
}

#[derive(Debug, PartialEq, Eq, PartialOrd, Ord, Clone)]
struct Located {
    file: String,
    line: usize,
    col: usize,
    kind: String,
    code: Option<String>,
}

#[test]
fn every_malformed_class_is_rejected_with_located_errors() {
    let base = read_tree(&fixtures().join("valid"));
    let mut dirs: Vec<PathBuf> = fs::read_dir(fixtures().join("malformed"))
        .unwrap()
        .map(|e| e.unwrap().path())
        .collect();
    dirs.sort();
    assert!(
        dirs.len() >= 40,
        "expected the full malformed set, found {}",
        dirs.len()
    );
    let mut failures = BTreeMap::new();
    for dir in &dirs {
        let case: serde_json::Value =
            serde_json::from_str(&fs::read_to_string(dir.join("case.json")).unwrap()).unwrap();
        let remove: Vec<String> = case["remove"]
            .as_array()
            .unwrap()
            .iter()
            .map(|v| v.as_str().unwrap().to_string())
            .collect();
        let tree = overlay(&base, dir, &remove);
        let mut expected: Vec<Located> = case["errors"]
            .as_array()
            .unwrap()
            .iter()
            .map(|e| Located {
                file: e["file"].as_str().unwrap().into(),
                line: e["line"].as_u64().unwrap() as usize,
                col: e["col"].as_u64().unwrap() as usize,
                kind: e["kind"].as_str().unwrap().into(),
                code: e["code"].as_str().map(String::from),
            })
            .collect();
        expected.sort();
        match WorldSource::load_tree(&tree) {
            Ok(_) => {
                failures.insert(dir.display().to_string(), "accepted".to_string());
            }
            Err(errors) => {
                let mut got: Vec<Located> = errors
                    .0
                    .iter()
                    .map(|e| Located {
                        file: e.file.clone(),
                        line: e.line,
                        col: e.col,
                        kind: serde_json::to_value(e.kind)
                            .unwrap()
                            .as_str()
                            .unwrap()
                            .to_string(),
                        code: e.code.map(String::from),
                    })
                    .collect();
                got.sort();
                if got != expected {
                    failures.insert(
                        dir.display().to_string(),
                        format!("expected {expected:?}\n got {got:?}\n{errors}"),
                    );
                }
            }
        }
    }
    assert!(failures.is_empty(), "{failures:#?}");
}

fn first_error(tree: &SourceTree) -> cwr_contract::Error {
    WorldSource::load_tree(tree)
        .expect_err("must be rejected")
        .0
        .remove(0)
}

#[test]
fn non_regular_files_invalid_utf8_and_missing_required_files_are_rejected() {
    let base = read_tree(&fixtures().join("valid"));

    let mut t = base.clone();
    t.files.insert("lib/link.py".into(), None);
    let e = first_error(&t);
    assert_eq!(
        (e.file.as_str(), e.line, e.col, e.code),
        ("lib/link.py", 1, 1, Some("CWR-C001"))
    );

    let mut t = base.clone();
    t.files.insert(
        "manual/intro.md".into(),
        Some(b"ok\nbad \xff byte\n".to_vec()),
    );
    let e = first_error(&t);
    assert_eq!(
        (e.file.as_str(), e.line, e.kind),
        ("manual/intro.md", 2, cwr_contract::ErrorKind::Io)
    );

    for required in ["world.kvx", "CHANGELOG.kvx"] {
        let mut t = base.clone();
        t.files.remove(required);
        let errors = WorldSource::load_tree(&t).expect_err(required);
        assert!(
            errors
                .0
                .iter()
                .any(|e| e.file == required && e.kind == cwr_contract::ErrorKind::MissingKey),
            "{errors}"
        );
    }
}

#[test]
fn json_model_rejects_values_the_kvx_reader_would_reject() {
    let src = WorldSource::load_dir(&fixtures().join("valid")).unwrap();
    let json = src.to_json().expect("model serialises");
    let cases = [
        ("\"NOT_FOUND\"", "\"GONE\""),
        (
            "\"content_guaranteed\": false",
            "\"content_guaranteed\": true",
        ),
        ("\"memory_mb\": 256", "\"memory_mb\": 0"),
        (
            "\"migration\": \"migrations/0_2_0_kv_namespaces.py\"",
            "\"migration\": \"\"",
        ),
        (
            "\"side_effect\": \"external\"",
            "\"side_effect\": \"mutating\"",
        ),
        (
            "\"seed_visible\": true",
            "\"seed_visible\": true, \"seed\": 1",
        ),
        ("\"version\": \"0.2.0\"", "\"version\": \"0.3.0\""),
        (
            "\"effects\": [\n            \"not fs.exists(path)\"\n          ]",
            "\"effects\": []",
        ),
        // Fixture values and names.
        ("\"primitive\": \"fs.delete\"", "\"primitive\": \"fs.nonexistent\""),
        ("\"sequence\": 1,", "\"sequence\": -4,"),
        ("\"sequence\": 1,", "\"sequence\": 0,"),
        ("\"world_time\": 0,\n", "\"world_time\": 0, \"seed\": \"abc\",\n"),
        (
            "\"request_hash\": \"sha256:5531",
            "\"request_hash\": \"sha256:XY31",
        ),
        // Adapter endpoint, optional keys and failure sets.
        (
            "\"endpoint\": \"stub://search/v1/query\"",
            "\"endpoint\": \"file:///etc/passwd\"",
        ),
        (
            "\"endpoint\": \"stub://search/v1/query\"",
            "\"endpoint\": \"stub://a\\nb\"",
        ),
        (
            "\"undeclared_status\": \"INVALID\"",
            "\"undeclared_status\": \"LOCKED\"",
        ),
        ("\"retry_after_ratio\": 7", "\"retry_after_ratio\": 0"),
        ("\"idempotency\": \"none\"", "\"idempotency\": \"key\""),
        ("\"TIMEOUT\"\n", "\"REJECTED\"\n"),
        // Names, modules and paths.
        ("\"name\": \"search.query\"", "\"name\": \"search.Query\""),
        ("\"name\": \"search.query\"", "\"name\": \"fs.query\""),
        ("\"module\": \"search\"", "\"module\": \"Search\""),
        ("\"path\": \"contracts/fs.kvx\"", "\"path\": \"contracts/kv2.kvx\""),
        // Inventory: class, digest, completeness; migration links.
        (
            "\"path\": \"lib/fs.py\",\n      \"class\": \"lib\"",
            "\"path\": \"lib/fs.py\",\n      \"class\": \"manual\"",
        ),
        ("\"sha256\": \"", "\"sha256\": \"zz"),
        (
            "\"path\": \"manual/intro.md\",",
            "\"path\": \"manual/notes.md\",",
        ),
        ("\"changes\": [\n        \"kv_namespaces\"\n      ]\n    }\n  ],\n  \"fixtures\"", "\"changes\": []\n    }\n  ],\n  \"fixtures\""),
        // Deep nesting is an error, not a crash.
        (
            "\"returns\": \"list[SearchHit]\"",
            "\"returns\": \"list[list[list[list[list[list[list[list[list[list[list[list[list[list[list[list[list[list[list[list[list[list[list[list[list[list[list[list[list[list[list[list[list[list[list[list[list[list[list[list[list[list[list[list[list[list[list[list[list[list[list[list[list[list[list[list[list[list[list[list[list[list[list[list[list[int]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]\"",
        ),
    ];
    for (old, new) in cases {
        assert!(json.contains(old), "fixture JSON lacks {old}");
        let tampered = json.replacen(old, new, 1);
        assert!(
            WorldSource::from_json(&tampered).is_err(),
            "accepted tampered model: {old} -> {new}"
        );
    }
}

#[test]
fn json_model_errors_are_typed_and_located() {
    let src = WorldSource::load_dir(&fixtures().join("valid")).unwrap();
    let json = src.to_json().expect("model serialises");
    // A rule violation is the KVX reader's own error, located in the file it concerns.
    let tampered = json.replacen(
        "\"endpoint\": \"stub://search/v1/query\"",
        "\"endpoint\": \"file:///etc/passwd\"",
        1,
    );
    let errors = WorldSource::from_json(&tampered).unwrap_err();
    assert_eq!(errors.0.len(), 1, "{errors}");
    assert_eq!(errors.0[0].file, "adapters/search.kvx");
    assert_eq!(errors.0[0].kind, ErrorKind::BadValue);
    assert!(errors.0[0].line > 1);
    // A reclassified inventory entry is reported at its path.
    let tampered = json.replacen(
        "\"path\": \"lib/fs.py\",\n      \"class\": \"lib\"",
        "\"path\": \"lib/fs.py\",\n      \"class\": \"manual\"",
        1,
    );
    let errors = WorldSource::from_json(&tampered).unwrap_err();
    assert!(errors.0.iter().all(|e| e.file == "lib/fs.py"), "{errors}");
    // JSON syntax errors carry the line and column of the JSON text.
    let errors = WorldSource::from_json("{\n  \"world\": ]").unwrap_err();
    assert_eq!(
        (
            errors.0[0].file.as_str(),
            errors.0[0].line,
            errors.0[0].kind
        ),
        ("<json>", 2, ErrorKind::Json)
    );
}

#[test]
fn single_files_parse_with_the_source_rules() {
    let valid = fixtures().join("valid");
    let read = |p: &str| fs::read_to_string(valid.join(p)).unwrap();
    let contract = ContractFile::parse("contracts/fs.kvx", &read("contracts/fs.kvx")).unwrap();
    assert_eq!(contract.module, "fs");
    let adapter = AdapterFile::parse("adapters/search.kvx", &read("adapters/search.kvx")).unwrap();
    assert_eq!(adapter.adapters[0].retry_ratio(), 7);
    let drift = read("drift/drift-1.kvx");
    let schedule = DriftFile::parse("drift/drift-1.kvx", &drift, &|n| n == "fs.delete").unwrap();
    assert_eq!(schedule.entries[0].n, 1);
    // The per-file reader refuses what load_tree refuses, located in the file.
    let errors = DriftFile::parse("drift/drift-1.kvx", &drift, &|_| false).unwrap_err();
    assert_eq!(errors.0[0].kind, ErrorKind::BadValue);
    let leading_zero = drift.replace("[drift.1]", "[drift.01]");
    let errors = DriftFile::parse("drift/drift-1.kvx", &leading_zero, &|_| true).unwrap_err();
    assert_eq!(errors.0[0].kind, ErrorKind::UnknownSection);
    let no_doc = drift.replace(
        "doc = \"From turn 8, fs.delete refuses files of 4 KiB or more.\"\n",
        "",
    );
    assert!(DriftFile::parse("drift/drift-1.kvx", &no_doc, &|_| true).is_err());
    let unknown_key = format!("{drift}note = 1\n");
    assert!(DriftFile::parse("drift/drift-1.kvx", &unknown_key, &|_| true).is_err());
    // A path of another class is refused before reading.
    let errors =
        ContractFile::parse("adapters/search.kvx", &read("adapters/search.kvx")).unwrap_err();
    assert_eq!(errors.0[0].kind, ErrorKind::UnknownFile);
    // The module must match the file name.
    assert!(ContractFile::parse("contracts/kv.kvx", &read("contracts/fs.kvx")).is_err());
}

#[test]
fn deeply_nested_values_in_a_source_are_errors_not_crashes() {
    let base = read_tree(&fixtures().join("valid"));
    let n = 10_000;
    let nested_expr = format!("{}len(q){} > 0", "(".repeat(n), ")".repeat(n));
    let nested_type = format!("{}int{}", "list[".repeat(n), "]".repeat(n));
    let adapter = |t: &SourceTree| {
        String::from_utf8(t.files["adapters/search.kvx"].clone().unwrap()).unwrap()
    };
    for (old, new) in [
        ("\"len(q) > 0\"", format!("\"{nested_expr}\"")),
        ("\"list[SearchHit]\"", format!("\"{nested_type}\"")),
    ] {
        let mut t = base.clone();
        let text = adapter(&t).replacen(old, &new, 1);
        t.files
            .insert("adapters/search.kvx".into(), Some(text.into_bytes()));
        let errors = WorldSource::load_tree(&t).expect_err("bounded nesting");
        assert_eq!(errors.0[0].file, "adapters/search.kvx", "{errors}");
    }
}
