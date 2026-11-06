// Copyright Sidiora Labs Inc. CWR® 2026-2027
// SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0

//! Context assembly (model-surface.md section 1; clauses 9.1 and 9.5; task 3.1, case CWR-012).
//!
//! The context is the whole interface: nothing reaches the model except these four components, in
//! this fixed order, plus what its own scripts return through `world.history`:
//!
//! | Order | Component | Present |
//! |---|---|---|
//! | 1 | The Build's `manual.md`, byte for byte | Always; the context's first bytes |
//! | 2 | The recompile notice ([`crate::notice::component`]) | Only before the first turn after an upgrade |
//! | 3 | The ledger tail, K records ([`super::tail`]) | Always (empty before any record) |
//! | 4 | The last result envelope ([`super::envelope_render`]) | After the first turn |
//!
//! The assembler concatenates the components with the fixed separator lines [`SEP_NOTICE`],
//! [`SEP_TAIL`] and [`SEP_ENVELOPE`] and never edits the manual's bytes, so every context of every
//! Instance of one Build starts with the same manual bytes, followed by the same separator: a
//! cache-stable prefix. Assembly is a pure function of the Build and the Instance's live ledger,
//! stored envelope and stored notice: it writes nothing, reads no clock and no host value, and is
//! byte-identical when repeated. No host-derived value (Instance id, host timestamps, durations,
//! paths, the arm label) is part of any component.

use std::path::Path;

use serde_json::{json, Value};

use super::{envelope_render, tail};
use crate::ledger::TailSize;
use crate::state::Instance;
use crate::{io_err, sha256_hex, KernelError, Result};

/// The manual inside a Build directory.
pub const MANUAL_FILE: &str = "manual.md";
/// Opens component 2.
pub const SEP_NOTICE: &str = "\n==== cwr context: recompile notice ====\n";
/// Opens component 3.
pub const SEP_TAIL: &str = "\n==== cwr context: ledger tail ====\n";
/// Opens component 4.
pub const SEP_ENVELOPE: &str = "\n==== cwr context: last result envelope ====\n";
/// Every separator, in component order.
pub const SEPARATORS: [&str; 3] = [SEP_NOTICE, SEP_TAIL, SEP_ENVELOPE];

/// The four components, by name, in the fixed order.
pub const COMPONENTS: [&str; 4] = ["manual", "notice", "tail", "envelope"];

/// One component of an assembled context.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Component {
    /// One of [`COMPONENTS`].
    pub name: &'static str,
    /// The separator written before the component; empty for the manual.
    pub separator: &'static str,
    /// The component's text, ending in `\n` unless empty.
    pub text: String,
}

/// An assembled context: the components present, in order, and the tail size used.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Context {
    /// Present components in the fixed order; the manual is always first.
    pub components: Vec<Component>,
    /// The K the tail was rendered with.
    pub tail_size: TailSize,
    /// The turn number the context is composed for.
    pub next_turn: u64,
}

impl Context {
    /// The context exactly as the model receives it.
    pub fn render(&self) -> String {
        let mut out = String::new();
        for c in &self.components {
            out.push_str(c.separator);
            out.push_str(&c.text);
        }
        out
    }

    /// Operator view: the byte offset, length and sha256 of every component, and of the whole.
    pub fn to_json(&self) -> Value {
        let mut offset = 0usize;
        let mut parts = Vec::new();
        for c in &self.components {
            offset += c.separator.len();
            parts.push(json!({
                "name": c.name,
                "separator": c.separator,
                "offset": offset,
                "length": c.text.len(),
                "sha256": format!("sha256:{}", sha256_hex(c.text.as_bytes())),
            }));
            offset += c.text.len();
        }
        let whole = self.render();
        json!({
            "format": "cwr.context",
            "schema_version": 1,
            "next_turn": self.next_turn,
            "tail_size": self.tail_size.to_json(),
            "components": parts,
            "length": whole.len(),
            "sha256": format!("sha256:{}", sha256_hex(whole.as_bytes())),
        })
    }
}

/// The manual bytes of the Build in `build_dir`, unchanged. Refused when the manual is not UTF-8
/// or contains a separator line, which would make the context ambiguous.
pub fn manual(build_dir: &Path) -> Result<String> {
    let path = build_dir.join(MANUAL_FILE);
    let bytes = std::fs::read(&path).map_err(|e| io_err("read", &path, e))?;
    let text = String::from_utf8(bytes)
        .map_err(|_| KernelError::Corrupt(format!("{} is not UTF-8", path.display())))?;
    if let Some(sep) = SEPARATORS
        .iter()
        .find(|s| text.contains(s.trim_matches('\n')))
    {
        return Err(KernelError::Refused(format!(
            "{} contains the context separator {:?}",
            path.display(),
            sep.trim_matches('\n')
        )));
    }
    Ok(text)
}

/// Assembles the context for the next turn of `inst`. `tail` overrides K for this rendering (the
/// H3 sweep); by default K is the one recorded in the Instance's header record.
pub fn assemble(inst: &Instance, tail: Option<TailSize>) -> Result<Context> {
    let records = inst.records();
    let k = match tail {
        Some(k) => k,
        None => tail::header_tail_size(records)?,
    };
    let mut components = vec![Component {
        name: "manual",
        separator: "",
        text: manual(&inst.build.dir)?,
    }];
    if let Some(text) = crate::notice::component(inst)? {
        components.push(Component {
            name: "notice",
            separator: SEP_NOTICE,
            text: with_final_newline(text),
        });
    }
    components.push(Component {
        name: "tail",
        separator: SEP_TAIL,
        text: tail::render(records, k),
    });
    if let Some(env) = envelope_render::render(&inst.dir, records)? {
        components.push(Component {
            name: "envelope",
            separator: SEP_ENVELOPE,
            text: with_final_newline(env),
        });
    }
    Ok(Context {
        components,
        tail_size: k,
        next_turn: inst.meta.turn + 1,
    })
}

fn with_final_newline(mut s: String) -> String {
    if !s.is_empty() && !s.ends_with('\n') {
        s.push('\n');
    }
    s
}

#[cfg(test)]
mod tests {
    use super::*;

    fn part(name: &'static str, separator: &'static str, text: &str) -> Component {
        Component {
            name,
            separator,
            text: text.into(),
        }
    }

    #[test]
    fn render_concatenates_separators_and_components_in_order() {
        let c = Context {
            components: vec![
                part("manual", "", "# m\n"),
                part("tail", SEP_TAIL, "t1  wt1    fs.write             ok\n"),
                part("envelope", SEP_ENVELOPE, "{\"status\":\"ok\"}\n"),
            ],
            tail_size: TailSize::DEFAULT,
            next_turn: 2,
        };
        let r = c.render();
        assert!(r.starts_with("# m\n\n==== cwr context: ledger tail ====\nt1  "));
        assert!(r.ends_with("==== cwr context: last result envelope ====\n{\"status\":\"ok\"}\n"));
        let j = c.to_json();
        assert_eq!(j["components"][0]["offset"], 0);
        assert_eq!(j["components"][1]["offset"], 4 + SEP_TAIL.len());
        assert_eq!(j["length"], r.len());
    }

    #[test]
    fn manual_with_a_separator_is_refused() {
        let dir = std::env::temp_dir().join(format!("cwr-ctx-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::write(dir.join(MANUAL_FILE), "# m\n").unwrap();
        assert_eq!(manual(&dir).unwrap(), "# m\n");
        std::fs::write(
            dir.join(MANUAL_FILE),
            "# m\n==== cwr context: ledger tail ====\n",
        )
        .unwrap();
        assert!(manual(&dir).is_err());
        std::fs::write(dir.join(MANUAL_FILE), [0xff, 0xfe]).unwrap();
        assert!(manual(&dir).is_err());
        let _ = std::fs::remove_dir_all(&dir);
    }
}
