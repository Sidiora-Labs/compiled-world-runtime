// Copyright Sidiora Labs Inc. CWR® 2026-2027
// SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0

//! Failure values, per-call results and the turn result envelope (kernel.md section 5,
//! model-surface.md section 4, world-source.md section 6; clauses 2.2, 2.3, 2.5 and 6.4).
//!
//! The shapes are those pinned by the SDK codec (`sdk/python/cwr/_codec.py`): a failure is
//! `{code, primitive, precondition_index, retry_after, limit, detail}`; a per-call result is
//! `{status, value | error, content_guaranteed}`; a trace item is
//! `{primitive, status, code, world_time, content_guaranteed}`; the turn envelope is
//! `{status, returned, error, trace, world, resources}`. Every constructor here takes a
//! [`Code`], so no value outside the closed eleven-code envelope can be produced.

use cwr_contract::Code;
use serde_json::{json, Value};

/// Pseudo-primitive names the kernel reports for things that are not Build primitives.
pub const TURN_SCRIPT: &str = "turn.script";
/// Reported for a turn whose return value is not a valid envelope value.
pub const TURN_RETURN: &str = "turn.return";
/// Reported for failures of the sandbox process itself (exit, signal, process limit).
pub const SANDBOX_PROCESS: &str = "sandbox.process";
/// Reported for an import the sandbox closure refuses.
pub const SANDBOX_IMPORT: &str = "sandbox.import";

/// Closure facilities the runner may report (sdk/python/cwr/_runner.py); anything else in a
/// refusal frame is reported as `sandbox.interpreter`.
pub const CLOSURE_FACILITIES: [&str; 9] = [
    "sandbox.import",
    "sandbox.filesystem",
    "sandbox.network",
    "sandbox.process",
    "sandbox.clock",
    "sandbox.interpreter",
    "sandbox.os",
    "sandbox.thread",
    "sandbox.rpc",
];

/// A failure value of the envelope.
#[derive(Debug, Clone, PartialEq)]
pub struct Failure {
    /// Envelope code; one of the closed eleven.
    pub code: Code,
    /// Primitive, adapter or pseudo-primitive the failure is reported under.
    pub primitive: String,
    /// 1-based index of the failed precondition, when a precondition failed.
    pub precondition_index: Option<usize>,
    /// World ticks to wait before retrying (RATE_LIMITED only).
    pub retry_after: Option<u64>,
    /// Name of the exceeded limit (LIMIT only).
    pub limit: Option<String>,
    /// Model-readable explanation of the failure.
    pub detail: String,
}

impl Failure {
    /// A failure with no precondition index, retry delay or limit.
    pub fn new(code: Code, primitive: &str, detail: impl Into<String>) -> Failure {
        Failure {
            code,
            primitive: primitive.to_string(),
            precondition_index: None,
            retry_after: None,
            limit: None,
            detail: detail.into(),
        }
    }

    /// A failure of precondition `index` (1-based) of `primitive`.
    pub fn precondition(
        code: Code,
        primitive: &str,
        index: usize,
        detail: impl Into<String>,
    ) -> Failure {
        Failure {
            precondition_index: Some(index),
            ..Failure::new(code, primitive, detail)
        }
    }

    /// A `LIMIT` failure naming the exceeded `limit`.
    pub fn limit(primitive: &str, limit: &str, detail: impl Into<String>) -> Failure {
        Failure {
            limit: Some(limit.to_string()),
            ..Failure::new(Code::Limit, primitive, detail)
        }
    }

    /// The JSON value; `retry_after` only for RATE_LIMITED and `limit` only for LIMIT, as the
    /// codec requires.
    pub fn to_json(&self) -> Value {
        let retry_after = if self.code == Code::RateLimited {
            Some(self.retry_after.unwrap_or(0))
        } else {
            None
        };
        let limit = if self.code == Code::Limit {
            self.limit.clone()
        } else {
            None
        };
        json!({
            "code": self.code.as_str(),
            "primitive": self.primitive,
            "precondition_index": self.precondition_index,
            "retry_after": retry_after,
            "limit": limit,
            "detail": self.detail,
        })
    }
}

/// `{"status": "ok", "value": v, "content_guaranteed": cg}`.
pub fn call_ok(value: Value, content_guaranteed: bool) -> Value {
    json!({"status": "ok", "value": value, "content_guaranteed": content_guaranteed})
}

/// `{"status": "error", "error": failure, "content_guaranteed": cg}`.
pub fn call_error(failure: &Failure, content_guaranteed: bool) -> Value {
    json!({"status": "error", "error": failure.to_json(), "content_guaranteed": content_guaranteed})
}

/// One trace item.
pub fn trace_item(
    primitive: &str,
    code: Option<Code>,
    world_time: u64,
    content_guaranteed: bool,
) -> Value {
    json!({
        "primitive": primitive,
        "status": if code.is_none() { "ok" } else { "error" },
        "code": code.map(|c| c.as_str()),
        "world_time": world_time,
        "content_guaranteed": content_guaranteed,
    })
}

/// Turn status by the rules of kernel.md section 5.
pub fn turn_status(failed: bool, mutated: bool) -> &'static str {
    match (failed, mutated) {
        (false, _) => "ok",
        (true, true) => "partial",
        (true, false) => "error",
    }
}

/// Measured resource use of one turn.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct Resources {
    /// CPU time used by the sandbox process (host measurement).
    pub cpu_ms: u64,
    /// Wall time of the turn (host measurement).
    pub wall_ms: u64,
    /// Primitive calls made this turn, refused ones included.
    pub primitive_calls: u64,
    /// External calls sent to the gateway this turn.
    pub adapter_calls: u64,
    /// Primitive calls still allowed this turn.
    pub remaining_primitive_calls: u64,
    /// External calls still allowed this turn.
    pub remaining_adapter_calls: u64,
}

impl Resources {
    /// The `resources` value of the result envelope, host measurements included.
    pub fn to_json(&self) -> Value {
        json!({
            "cpu_ms": self.cpu_ms,
            "wall_ms": self.wall_ms,
            "primitive_calls": self.primitive_calls,
            "adapter_calls": self.adapter_calls,
            "remaining": {"primitive_calls": self.remaining_primitive_calls, "adapter_calls": self.remaining_adapter_calls},
        })
    }

    /// The deterministic part recorded in the ledger's `turn` record: counters only. Host
    /// measurements (cpu_ms, wall_ms) stay out of the ledger so a replay can reproduce it.
    pub fn ledger_json(&self) -> Value {
        json!({
            "primitive_calls": self.primitive_calls,
            "adapter_calls": self.adapter_calls,
            "remaining": {"primitive_calls": self.remaining_primitive_calls, "adapter_calls": self.remaining_adapter_calls},
        })
    }
}

/// The turn result envelope.
pub fn turn_envelope(
    status: &str,
    returned: Value,
    error: Option<&Failure>,
    trace: Vec<Value>,
    world: Value,
    resources: &Resources,
) -> Value {
    json!({
        "status": status,
        "returned": returned,
        "error": error.map(Failure::to_json),
        "trace": trace,
        "world": world,
        "resources": resources.to_json(),
    })
}

/// Parses a failure value the runner reports back (the script's uncaught exception). Returns
/// None for anything that is not a well-formed failure from the closed envelope.
pub fn parse_failure(v: &Value) -> Option<Failure> {
    let o = v.as_object()?;
    let code: Code = o.get("code")?.as_str()?.parse().ok()?;
    Some(Failure {
        code,
        primitive: o.get("primitive")?.as_str()?.to_string(),
        precondition_index: match o.get("precondition_index") {
            None | Some(Value::Null) => None,
            Some(n) => Some(n.as_u64()? as usize),
        },
        retry_after: match o.get("retry_after") {
            None | Some(Value::Null) => None,
            Some(n) => Some(n.as_u64()?),
        },
        limit: match o.get("limit") {
            None | Some(Value::Null) => None,
            Some(s) => Some(s.as_str()?.to_string()),
        },
        detail: o
            .get("detail")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string(),
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn failure_shapes() {
        let f = Failure::precondition(
            Code::Locked,
            "fs.delete",
            2,
            "not fs.locked(path) was false",
        );
        let v = f.to_json();
        assert_eq!(v["precondition_index"], json!(2));
        assert_eq!(v["retry_after"], Value::Null);
        assert_eq!(v["limit"], Value::Null);
        assert_eq!(parse_failure(&v), Some(f));
        let l = Failure::limit("world.now", "primitive_calls", "");
        assert_eq!(l.to_json()["limit"], json!("primitive_calls"));
        assert!(
            parse_failure(&json!({"code": "OOPS", "primitive": "x"})).is_none(),
            "codes outside the envelope never parse"
        );
    }

    #[test]
    fn statuses() {
        assert_eq!(turn_status(false, true), "ok");
        assert_eq!(turn_status(true, true), "partial");
        assert_eq!(turn_status(true, false), "error");
    }

    #[test]
    fn envelope_keys() {
        let e = turn_envelope(
            "ok",
            json!(1),
            None,
            vec![trace_item("fs.exists", None, 1, true)],
            json!({"version": "0", "build": "b", "time": 11, "turn": 1}),
            &Resources::default(),
        );
        let keys: Vec<&String> = e.as_object().unwrap().keys().collect();
        assert_eq!(keys.len(), 6);
        assert_eq!(
            e["resources"]["remaining"],
            json!({"primitive_calls": 0, "adapter_calls": 0})
        );
    }
}
