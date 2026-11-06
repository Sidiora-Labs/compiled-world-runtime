// Copyright Sidiora Labs Inc. CWR® 2026-2027
// SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0

//! The kernel side of the sandbox RPC channel (kernel.md sections 3 and 4; clause 6.2).
//!
//! Framing is pinned by the SDK (`sdk/python/cwr/_rpc.py`): a 4-byte unsigned big-endian payload
//! length followed by that many bytes of UTF-8 JSON; frames above [`MAX_FRAME`] are refused. One
//! primitive call is one request frame and one response frame:
//!
//! * request `{"v": 1, "id": n, "primitive": "<module.name>", "params": {...}}`
//! * response `{"v": 1, "id": n, "result": <per-call result>}`
//!
//! The runner (`sdk/python/cwr/_runner.py`) adds `start` (kernel to runner, first frame),
//! `refuse` (a closure refusal, answered like a call), `limit` and `end`. The reader is
//! incremental and never blocks longer than the socket read timeout, so a script that sends half
//! a frame and stalls cannot stall the kernel past its wall limit.

use std::io::{ErrorKind, Read, Write};
use std::os::unix::net::UnixStream;

use serde_json::Value;

use crate::canonical_json;

/// The `v` value every frame carries.
pub const PROTOCOL: u64 = 1;
/// Largest payload accepted in one frame, in bytes.
pub const MAX_FRAME: usize = 64 * 1024 * 1024;

/// One decoded message from the runner.
#[derive(Debug, Clone, PartialEq)]
pub enum Request {
    /// A primitive call made by the SDK.
    Call {
        /// Request id echoed in the response.
        id: Value,
        /// Qualified primitive name.
        primitive: String,
        /// Call parameters.
        params: Value,
    },
    /// A closure refusal reported by the runner.
    Refuse {
        /// Request id echoed in the response.
        id: Value,
        /// Closure facility the runner refused.
        facility: String,
        /// What was refused.
        detail: String,
    },
    /// The runner observed a process limit (only `memory_mb`).
    Limit {
        /// Name of the limit, e.g. `memory_mb`.
        limit: String,
    },
    /// The script finished.
    End {
        /// How the script ended: `ok`, `raised` or `unencodable`.
        outcome: String,
        /// The script's return value.
        returned: Value,
        /// The failure value of an uncaught kernel failure, or null.
        failure: Value,
        /// Text of an uncaught exception, if any.
        exception: Option<String>,
    },
    /// A well-framed message that is none of the above; answered with INVALID when it has an id.
    Malformed {
        /// The request id, when the frame carried one.
        id: Option<Value>,
        /// Why the frame is malformed.
        why: String,
    },
}

impl Request {
    /// Classifies one decoded frame; never fails, anything unrecognised is [`Request::Malformed`].
    pub fn parse(v: &Value) -> Request {
        let id = v.get("id").cloned();
        if v.get("v").and_then(Value::as_u64) != Some(PROTOCOL) {
            return Request::Malformed {
                id,
                why: "protocol version is not 1".into(),
            };
        }
        match v.get("op").and_then(Value::as_str) {
            None => match (
                id,
                v.get("primitive").and_then(Value::as_str),
                v.get("params"),
            ) {
                (Some(id), Some(p), Some(params)) => Request::Call {
                    id,
                    primitive: p.to_string(),
                    params: params.clone(),
                },
                (id, _, _) => Request::Malformed {
                    id,
                    why: "a call needs id, primitive and params".into(),
                },
            },
            Some("refuse") => match id {
                Some(id) => Request::Refuse {
                    id,
                    facility: v
                        .get("facility")
                        .and_then(Value::as_str)
                        .unwrap_or("sandbox.interpreter")
                        .to_string(),
                    detail: v
                        .get("detail")
                        .and_then(Value::as_str)
                        .unwrap_or("")
                        .chars()
                        .take(200)
                        .collect(),
                },
                None => Request::Malformed {
                    id: None,
                    why: "refuse needs an id".into(),
                },
            },
            Some("limit") => Request::Limit {
                limit: v
                    .get("limit")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .to_string(),
            },
            Some("end") => Request::End {
                outcome: v
                    .get("outcome")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .to_string(),
                returned: v.get("returned").cloned().unwrap_or(Value::Null),
                failure: v.get("failure").cloned().unwrap_or(Value::Null),
                exception: v
                    .get("exception")
                    .and_then(Value::as_str)
                    .map(|s| s.chars().take(500).collect()),
            },
            Some(other) => Request::Malformed {
                id,
                why: format!("unknown op {other:?}"),
            },
        }
    }
}

/// Encodes one frame: length header plus canonical JSON.
pub fn encode_frame(v: &Value) -> Vec<u8> {
    let payload = canonical_json(v);
    let mut out = Vec::with_capacity(payload.len() + 4);
    out.extend_from_slice(&(payload.len() as u32).to_be_bytes());
    out.extend_from_slice(payload.as_bytes());
    out
}

/// Writes one frame. The stream's write timeout bounds how long a script that stops reading can
/// hold the kernel.
pub fn write_frame(s: &mut UnixStream, v: &Value) -> std::io::Result<()> {
    s.write_all(&encode_frame(v))?;
    s.flush()
}

/// Result of one poll.
#[derive(Debug)]
pub enum Poll {
    /// One complete decoded frame.
    Frame(Value),
    /// No complete frame yet (read timed out).
    Pending,
    /// The peer closed the channel.
    Closed,
    /// The peer violated the framing (oversized length, invalid UTF-8 or JSON).
    Broken(String),
}

/// Incremental frame reader.
#[derive(Debug, Default)]
pub struct FrameReader {
    buf: Vec<u8>,
}

impl FrameReader {
    /// A reader with an empty buffer.
    pub fn new() -> Self {
        FrameReader { buf: Vec::new() }
    }

    fn take_frame(&mut self) -> Option<Poll> {
        if self.buf.len() < 4 {
            return None;
        }
        let n = u32::from_be_bytes([self.buf[0], self.buf[1], self.buf[2], self.buf[3]]) as usize;
        if n > MAX_FRAME {
            return Some(Poll::Broken(format!(
                "frame of {n} bytes exceeds {MAX_FRAME}"
            )));
        }
        if self.buf.len() < 4 + n {
            return None;
        }
        let payload: Vec<u8> = self.buf.drain(..4 + n).skip(4).collect();
        Some(match serde_json::from_slice::<Value>(&payload) {
            Ok(v) => Poll::Frame(v),
            Err(e) => Poll::Broken(format!("frame is not JSON: {e}")),
        })
    }

    /// Returns a complete frame if one is buffered or arrives before the read timeout.
    pub fn poll(&mut self, s: &mut UnixStream) -> Poll {
        if let Some(p) = self.take_frame() {
            return p;
        }
        let mut chunk = [0u8; 65536];
        loop {
            match s.read(&mut chunk) {
                Ok(0) => return Poll::Closed,
                Ok(n) => {
                    self.buf.extend_from_slice(&chunk[..n]);
                    if let Some(p) = self.take_frame() {
                        return p;
                    }
                    if self.buf.len() > MAX_FRAME + 4 {
                        return Poll::Broken("frame exceeds the maximum size".into());
                    }
                }
                Err(e) if matches!(e.kind(), ErrorKind::WouldBlock | ErrorKind::TimedOut) => {
                    return Poll::Pending
                }
                Err(e) if e.kind() == ErrorKind::Interrupted => continue,
                Err(_) => return Poll::Closed,
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use std::time::Duration;

    #[test]
    fn frames_round_trip_incrementally() {
        let (mut a, mut b) = UnixStream::pair().unwrap();
        b.set_read_timeout(Some(Duration::from_millis(20))).unwrap();
        let mut r = FrameReader::new();
        assert!(matches!(r.poll(&mut b), Poll::Pending));
        let f = encode_frame(
            &json!({"v": 1, "id": 1, "primitive": "fs.exists", "params": {"path": "/a"}}),
        );
        a.write_all(&f[..3]).unwrap();
        assert!(
            matches!(r.poll(&mut b), Poll::Pending),
            "half a header is pending, not an error"
        );
        a.write_all(&f[3..]).unwrap();
        match r.poll(&mut b) {
            Poll::Frame(v) => assert_eq!(
                Request::parse(&v),
                Request::Call {
                    id: json!(1),
                    primitive: "fs.exists".into(),
                    params: json!({"path": "/a"})
                }
            ),
            other => panic!("{other:?}"),
        }
        a.write_all(&(MAX_FRAME as u32 + 1).to_be_bytes()).unwrap();
        assert!(matches!(r.poll(&mut b), Poll::Broken(_)));
        drop(a);
    }

    #[test]
    fn request_kinds() {
        assert!(matches!(
            Request::parse(&json!({"v": 1, "op": "end", "outcome": "ok", "returned": 3})),
            Request::End { .. }
        ));
        assert!(matches!(
            Request::parse(&json!({"v": 2, "id": 1})),
            Request::Malformed { .. }
        ));
        assert!(matches!(
            Request::parse(
                &json!({"v": 1, "op": "refuse", "id": 4, "facility": "sandbox.import", "detail": "time"})
            ),
            Request::Refuse { .. }
        ));
    }
}
