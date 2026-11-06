// Copyright Sidiora Labs Inc. CWR® 2026-2027
// SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0

//! `--x-one-primitive`: one primitive call per turn (compiler.md section 10; clause 10.4; task
//! 3.2).
//!
//! The transform sets `limits.primitive_calls = 1` in the compiled surface, regenerates the manual
//! and the SDK from it with the ordinary generators (so the limits table states 1), and appends
//! [`SENTENCE`] to the manual's limits section. The kernel enforces the limit like any other
//! `primitive_calls` value: the second call of a turn is not executed and fails with `LIMIT`
//! naming `primitive_calls`. Every other contract and limit is the control's.
//!
//! Attribution ([`explains`]): against the control, `contracts.json` differs only in
//! `limits.primitive_calls`; the SDK differs only in the contracts digest it embeds; the manual
//! differs only in that digest, the `primitive_calls` row and the appended sentence.

use std::collections::BTreeMap;
use std::path::Path;

use serde_json::{json, Value};

use crate::diff::Entry;
use crate::manifest::{generate, Generated};

/// Flag name of a one-primitive Build.
pub const FLAG: &str = "x-one-primitive";

/// The sentence appended to the manual's limits section.
pub const SENTENCE: &str = "In this world `primitive_calls = 1`: a turn may make exactly one primitive call; a second call in the same turn is not executed and fails with LIMIT naming `primitive_calls`.";

const LIMITS_HEADING: &str = "## 4. Limits and clock\n";

/// Appends [`SENTENCE`] as the last paragraph of the manual's limits section.
pub fn with_sentence(manual: &str) -> Result<String, String> {
    let start = manual
        .find(LIMITS_HEADING)
        .ok_or("the manual has no limits section (## 4. Limits and clock)")?;
    let body = start + LIMITS_HEADING.len();
    let end = manual[body..]
        .find("\n## ")
        .map(|i| body + i + 1)
        .unwrap_or(manual.len());
    let mut out = String::with_capacity(manual.len() + SENTENCE.len() + 2);
    out.push_str(&manual[..end]);
    if !out.ends_with("\n\n") {
        out.push('\n');
    }
    out.push_str(SENTENCE);
    out.push_str("\n\n");
    out.push_str(&manual[end..]);
    Ok(out)
}

/// The one-primitive Build files from the control generation `g`.
pub fn transform(g: &Generated, prose: &BTreeMap<String, String>) -> Result<Generated, String> {
    let mut contracts = g.contracts.clone();
    let limits = contracts
        .get_mut("limits")
        .and_then(Value::as_object_mut)
        .ok_or("the compiled contracts have no limits")?;
    limits.insert("primitive_calls".into(), json!(1));
    let mut out = generate(contracts, prose);
    out.manual = with_sentence(&out.manual)?;
    Ok(out)
}

/// Whether a ContractDiff entry is the flag's single `limit_changed` entry.
pub fn is_flag_entry(e: &Entry) -> bool {
    e.kind_str() == "limit_changed"
        && e.target == "limits.primitive_calls"
        && e.after == Some(json!(1))
}

/// The attribution entry of a one-primitive Build: the manual sentence.
pub fn entry() -> Value {
    json!({"kind": "manual_sentence", "target": "manual", "field": "limits", "after": SENTENCE})
}

fn contracts_sha(dir: &Path) -> String {
    cwr_kernel::sha256_hex(&std::fs::read(dir.join("contracts.json")).unwrap_or_default())
}

/// Whether the flag explains the difference of one Build file between the control (`bc` in
/// `control`) and the one-primitive Build (`bx` in `experimental`).
pub fn explains(rel: &str, bc: &[u8], bx: &[u8], control: &Path, experimental: &Path) -> bool {
    let (sc, sx) = (contracts_sha(control), contracts_sha(experimental));
    match rel {
        "contracts.json" => {
            let (Ok(mut c), Ok(x)) = (
                serde_json::from_slice::<Value>(bc),
                serde_json::from_slice::<Value>(bx),
            ) else {
                return false;
            };
            c["limits"]["primitive_calls"] = json!(1);
            c == x
        }
        "manual.md" => {
            let (Ok(c), Ok(x)) = (std::str::from_utf8(bc), std::str::from_utf8(bx)) else {
                return false;
            };
            let Some(old) = serde_json::from_slice::<Value>(
                &std::fs::read(control.join("contracts.json")).unwrap_or_default(),
            )
            .ok()
            .and_then(|v| v["limits"]["primitive_calls"].as_u64()) else {
                return false;
            };
            let expected = c.replace(&sc, &sx).replacen(
                &format!("| `primitive_calls` | {old} |"),
                "| `primitive_calls` | 1 |",
                1,
            );
            with_sentence(&expected).is_ok_and(|m| m == x)
        }
        _ if rel.starts_with("sdk/") => {
            let (Ok(c), Ok(x)) = (std::str::from_utf8(bc), std::str::from_utf8(bx)) else {
                return false;
            };
            c.replace(&sc, &sx) == x
        }
        _ => false,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_sentence_closes_the_limits_section() {
        let m = "# w\n\n## 4. Limits and clock\n\n| a | b |\n\nTicks.\n\n## 5. Modules\n\nx\n";
        let out = with_sentence(m).unwrap();
        assert_eq!(
            out,
            format!("# w\n\n## 4. Limits and clock\n\n| a | b |\n\nTicks.\n\n{SENTENCE}\n\n## 5. Modules\n\nx\n")
        );
        assert!(with_sentence("# w\n").is_err());
        let last = with_sentence("## 4. Limits and clock\n\nT.\n").unwrap();
        assert!(last.ends_with(&format!("T.\n\n{SENTENCE}\n\n")));
    }
}
