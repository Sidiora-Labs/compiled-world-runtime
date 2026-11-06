// Copyright Sidiora Labs Inc. CWR® 2026-2027
// SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0

//! The turn protocol (model-surface.md section 3; clause 9.3; task 3.1, case CWR-012).
//!
//! A model message is accepted as a turn only when fence extraction yields exactly one fenced code
//! block and that block's language is `python`. Text outside the block is ignored and never
//! recorded. Every other message is refused before anything executes: no sandbox is started, no
//! primitive is called, `world_time` does not advance, and the refusal is one `turn` record with
//! status `error`, code `INVALID` and `primitive = "turn.protocol"` (so the refusal is in the
//! ledger tail the model sees next) plus a result envelope naming the reason.
//!
//! Fences follow the CommonMark rules: an opening fence is a line indented by at most three spaces
//! that starts with at least three backticks or tildes; its info string's first word is the block
//! language (a backtick fence whose info string contains a backtick is not a fence); the block ends
//! at a line indented by at most three spaces holding only a run of the same fence character at
//! least as long as the opening run; up to the opening fence's indentation is removed from every
//! content line. An unclosed fence is refused rather than read to the end of the message, so a
//! truncated message never executes a truncated script.

use cwr_contract::Code;
use serde_json::{json, Value};

use crate::contract_check::ContractTable;
use crate::envelope::{turn_envelope, Failure, Resources};
use crate::executor::sealed;
use crate::ledger::{Kind, RecordDraft};
use crate::limits::Limits;
use crate::state::Instance;
use crate::{canonical_json, sha256_hex, KernelError, Result};

/// The pseudo-primitive a protocol refusal names in its envelope and its `turn` record.
pub const PROTOCOL_PRIMITIVE: &str = "turn.protocol";
/// The only block language the protocol executes.
pub const SCRIPT_LANGUAGE: &str = "python";

/// One fenced block of a model message.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Block {
    /// First word of the info string; empty when the fence has none.
    pub language: String,
    /// The block content, each line ending in `\n`.
    pub body: String,
    /// 1-based line of the opening fence.
    pub line: usize,
}

/// Why a message is not a valid turn. Each variant is refused as `INVALID`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Violation {
    /// The message holds no fenced block.
    NoBlock,
    /// The message holds more than one fenced block; the kernel does not pick one.
    MultipleBlocks {
        /// 1-based lines of every opening fence.
        lines: Vec<usize>,
    },
    /// The one fenced block is not `python`.
    WrongLanguage {
        /// The block's language, empty when the fence has none.
        language: String,
        /// 1-based line of the opening fence.
        line: usize,
    },
    /// A fence opens and never closes.
    UnclosedFence {
        /// 1-based line of the opening fence.
        line: usize,
    },
}

impl Violation {
    /// The envelope `detail`: a fixed sentence naming the rule and, where there is one, the line.
    pub fn detail(&self) -> String {
        match self {
            Violation::NoBlock => {
                "the message holds no fenced python block; a turn is exactly one fenced python block, nothing was executed".into()
            }
            Violation::MultipleBlocks { lines } => format!(
                "the message holds {} fenced blocks (opening fences at lines {}); a turn is exactly one fenced python block, nothing was executed",
                lines.len(),
                lines
                    .iter()
                    .map(usize::to_string)
                    .collect::<Vec<_>>()
                    .join(", ")
            ),
            Violation::WrongLanguage { language, line } => format!(
                "the fenced block at line {line} is {}, not python; a turn is exactly one fenced python block, nothing was executed",
                if language.is_empty() {
                    "untagged".to_string()
                } else {
                    format!("{language:?}")
                }
            ),
            Violation::UnclosedFence { line } => format!(
                "the fence opened at line {line} is never closed; a turn is exactly one fenced python block, nothing was executed"
            ),
        }
    }
}

struct Open {
    ch: char,
    run: usize,
    indent: usize,
    language: String,
    line: usize,
    body: String,
}

fn leading_spaces(s: &str) -> usize {
    s.len() - s.trim_start_matches(' ').len()
}

fn fence_run(s: &str, ch: char) -> usize {
    s.chars().take_while(|c| *c == ch).count()
}

/// Every fenced block of `message`, in order. An unclosed fence is a violation.
pub fn fenced_blocks(message: &str) -> std::result::Result<Vec<Block>, Violation> {
    let mut blocks = Vec::new();
    let mut open: Option<Open> = None;
    for (i, raw) in message.split('\n').enumerate() {
        let line = raw.strip_suffix('\r').unwrap_or(raw);
        let indent = leading_spaces(line);
        let rest = &line[indent..];
        match open.as_mut() {
            Some(o) => {
                let run = fence_run(rest, o.ch);
                let closes = indent <= 3 && run >= o.run && rest[run..].trim().is_empty();
                if closes {
                    if let Some(o) = open.take() {
                        blocks.push(Block {
                            language: o.language,
                            body: o.body,
                            line: o.line,
                        });
                    }
                } else {
                    let strip = leading_spaces(line).min(o.indent);
                    o.body.push_str(&line[strip..]);
                    o.body.push('\n');
                }
            }
            None => {
                if indent > 3 {
                    continue;
                }
                let Some(ch) = rest.chars().next().filter(|c| *c == '`' || *c == '~') else {
                    continue;
                };
                let run = fence_run(rest, ch);
                if run < 3 {
                    continue;
                }
                let info = rest[run..].trim();
                if ch == '`' && info.contains('`') {
                    continue;
                }
                open = Some(Open {
                    ch,
                    run,
                    indent,
                    language: info.split_whitespace().next().unwrap_or("").to_string(),
                    line: i + 1,
                    body: String::new(),
                });
            }
        }
    }
    match open {
        Some(o) => Err(Violation::UnclosedFence { line: o.line }),
        None => Ok(blocks),
    }
}

/// The turn script of `message`: the body of its only fenced block, which must be `python`.
pub fn extract_script(message: &str) -> std::result::Result<String, Violation> {
    let mut blocks = fenced_blocks(message)?;
    match blocks.len() {
        0 => Err(Violation::NoBlock),
        1 => {
            let b = blocks.remove(0);
            if b.language == SCRIPT_LANGUAGE {
                Ok(b.body)
            } else {
                Err(Violation::WrongLanguage {
                    language: b.language,
                    line: b.line,
                })
            }
        }
        _ => Err(Violation::MultipleBlocks {
            lines: blocks.iter().map(|b| b.line).collect(),
        }),
    }
}

/// Refuses the next turn of `inst` for `violation`, without executing anything.
///
/// Guarantees: the Instance must be `idle` and not sealed (otherwise nothing is written); the turn
/// counter advances by one, `world_time` does not; the refusal is one ledger record of kind
/// `turn`, with status `error`, code `INVALID`, `calls` 0 and `primitive = "turn.protocol"`; the
/// returned envelope (also stored as the turn's result envelope) has status `error`, an empty
/// trace, zero resource use and the error `{code: INVALID, primitive: turn.protocol}`.
///
/// On an `--x-drift` Build the refused turn is still a turn: every drift entry scheduled for it
/// is recorded (kind `drift`, before the refusal's `turn` record) exactly as an executed turn
/// would record it, so the ledger records the injection either way (model-surface.md section 8).
/// The turn is opened (`idle` to `in_turn`) before those records, so a crash between them is
/// closed by recovery like any interrupted turn.
pub fn refuse(inst: &mut Instance, violation: &Violation) -> Result<Value> {
    if let Some(s) = sealed(inst) {
        return Err(KernelError::State(format!(
            "instance {} is sealed for inspection after INVARIANT_VIOLATION: {}",
            inst.meta.id,
            canonical_json(&s)
        )));
    }
    if inst.meta.status != "idle" {
        return Err(KernelError::Conflict(format!(
            "instance is {}, not idle",
            inst.meta.status
        )));
    }
    let limits = Limits::of(&inst.build)?;
    let mut table = ContractTable::load(&inst.build)?;
    let drift = crate::drift_injector::prepare(&inst.build, &mut table, inst.meta.turn + 1)?;
    let failure = Failure::new(Code::Invalid, PROTOCOL_PRIMITIVE, violation.detail());
    let resources = Resources {
        remaining_primitive_calls: limits.primitive_calls,
        remaining_adapter_calls: limits.adapter_calls,
        ..Resources::default()
    };
    let turn = inst.begin_turn()?;
    drift.record(inst, turn)?;
    let returned_hash = format!(
        "sha256:{}",
        sha256_hex(canonical_json(&Value::Null).as_bytes())
    );
    let mut draft = RecordDraft::new(Kind::Turn, turn, inst.clock.now())
        .with("status", json!("error"))
        .with("code", json!(Code::Invalid.as_str()))
        .with("calls", json!(0))
        .with("returned_hash", json!(returned_hash))
        .with("resources", resources.ledger_json());
    draft.primitive = Some(PROTOCOL_PRIMITIVE.to_string());
    let rec = inst.end_turn_refused(draft)?;
    let world = json!({
        "version": inst.build.version,
        "build": inst.build.hash,
        "time": rec["world_time"],
        "turn": rec["turn"],
    });
    let env = turn_envelope(
        "error",
        Value::Null,
        Some(&failure),
        Vec::new(),
        world,
        &resources,
    );
    super::envelope_render::store(&inst.dir, &rec, &env)?;
    Ok(env)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn exactly_one_python_block_is_the_script() {
        let m = "I will write a file.\n\n```python\nimport fs\nreturn 1\n```\nDone.\n";
        assert_eq!(extract_script(m).unwrap(), "import fs\nreturn 1\n");
    }

    #[test]
    fn crlf_tilde_and_indented_fences() {
        assert_eq!(
            extract_script("~~~python\r\nx = 1\r\n~~~\r\n").unwrap(),
            "x = 1\n"
        );
        assert_eq!(
            extract_script("  ```python\n  a = 1\n    b = 2\n  ```\n").unwrap(),
            "a = 1\n  b = 2\n"
        );
        assert_eq!(extract_script("````python\n```\n````\n").unwrap(), "```\n");
    }

    #[test]
    fn fence_lookalikes_inside_a_block_are_content() {
        let m = "```python\ns = '''\n```python\n'''\n```\n";
        assert_eq!(extract_script(m).unwrap(), "s = '''\n```python\n'''\n");
    }

    #[test]
    fn violations() {
        assert_eq!(
            extract_script("no code here").unwrap_err(),
            Violation::NoBlock
        );
        assert_eq!(
            extract_script("```python\na\n```\nthen\n```python\nb\n```\n").unwrap_err(),
            Violation::MultipleBlocks { lines: vec![1, 5] }
        );
        assert_eq!(
            extract_script("```py\na\n```\n").unwrap_err(),
            Violation::WrongLanguage {
                language: "py".into(),
                line: 1
            }
        );
        assert_eq!(
            extract_script("```\na\n```\n").unwrap_err(),
            Violation::WrongLanguage {
                language: String::new(),
                line: 1
            }
        );
        assert_eq!(
            extract_script("text\n```python\na\n").unwrap_err(),
            Violation::UnclosedFence { line: 2 }
        );
        assert_eq!(
            extract_script("```python\na\n```\n```text\nplan\n```\n").unwrap_err(),
            Violation::MultipleBlocks { lines: vec![1, 4] }
        );
        assert_eq!(
            extract_script("    ```python\n    a\n    ```\n").unwrap_err(),
            Violation::NoBlock,
            "a fence indented by four spaces is an indented code line, not a fence"
        );
        assert_eq!(
            extract_script("``` python`x\n").unwrap_err(),
            Violation::NoBlock,
            "a backtick info string holding a backtick is not a fence"
        );
    }

    #[test]
    fn details_name_the_rule() {
        for v in [
            Violation::NoBlock,
            Violation::MultipleBlocks { lines: vec![1, 2] },
            Violation::WrongLanguage {
                language: "text".into(),
                line: 3,
            },
            Violation::UnclosedFence { line: 4 },
        ] {
            assert!(v.detail().contains("exactly one fenced python block"));
            assert!(v.detail().contains("nothing was executed"));
        }
    }
}
