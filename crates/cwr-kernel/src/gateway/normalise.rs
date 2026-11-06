// Copyright Sidiora Labs Inc. CWR® 2026-2027
// SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0

//! Normalisation of service responses into the declared return shape or the failure envelope
//! (kernel.md section 7 gateway table; clauses 7.2 and 1.6).
//!
//! | Service response | Gateway result |
//! |---|---|
//! | Declared success status with a body that decodes | the body, handed to the adapter's `live.py` for reshaping, then checked against the declared return shape |
//! | Declared failure status mapped in the contract | the mapped code |
//! | Status the contract does not map | `undeclared_status` (`UNAVAILABLE` by default, or `INVALID`) |
//! | Transport error, connection refused, reset, DNS failure | `UNAVAILABLE` |
//! | No response within `timeout_ms` of host time | `TIMEOUT` |
//! | Body that does not decode or does not match the declared shape | `INVALID` |
//! | Rate limit signalled by the service | `RATE_LIMITED` with `retry_after` converted to ticks by the declared ratio |
//! | Replay cassette has no entry | `UNAVAILABLE` (no socket is opened) |
//!
//! **Declared codes only.** Every row lands on a code the adapter declares in `failures`. The
//! codes the gateway needs whatever the service says are required by the compiler (CWR-F002):
//! `UNAVAILABLE` (transport), `TIMEOUT` (every adapter has `timeout_ms`), `DUPLICATE_KEY` (every
//! `key` adapter), the `undeclared_status`, and every target of live.py's `STATUS_CODES` (checked
//! by the contract tests); none of these is patched at runtime, an undeclared one is a defect
//! (`INVARIANT_VIOLATION`). What the contract selects is applied as declared (clause 7.2): a
//! status the adapter does not map, including a wire-protocol code it does not declare, is its
//! `undeclared_status`; a malformed body is `INVALID` where the adapter declares `INVALID` and
//! its `undeclared_status` (then `UNAVAILABLE`) where it does not.
//!
//! Where the mapping comes from. The undeclared-status fallback and the retry ratio are rules of
//! the adapter contract: `undeclared_status` (`UNAVAILABLE` or `INVALID`, default `UNAVAILABLE`)
//! and `retry_after_ratio` (positive integer, default 1) in `adapters/<module>.kvx`, compiled
//! into contracts.json and applied identically by the compiler's contract tests. The adapter
//! grammar has no keys for the per-status mapping, so a Build's `adapters/<module>/live.py`
//! carries it as module constants (examples/refworld/README.md, convention 2), read by the
//! gateway's adapter-code worker and by the compiler alike: `STATUS_CODES` (adapter to
//! {status: "ok" | code}) and `RETRY_AFTER_FIELD`.
//! An adapter without `STATUS_CODES` speaks the stub wire protocol of benchmark/stubs/README.md,
//! mirrored by `cassette_tool.normalise`: 2xx with `{"result": <value>}`; 400 INVALID, 404
//! NOT_FOUND, 409 DUPLICATE_KEY, 413 LIMIT, 422 REJECTED, 429 RATE_LIMITED, 503 UNAVAILABLE, each
//! with a body whose `code` names the same code.

use std::collections::{BTreeMap, BTreeSet};

use cwr_contract::Code;
use serde_json::Value;

use super::cassette::RawResponse;
use crate::canonical_json;

/// The stub wire protocol's status table (benchmark/stubs/stubcore.py).
pub const WIRE_CODES: [(u16, Code); 7] = [
    (400, Code::Invalid),
    (404, Code::NotFound),
    (409, Code::DuplicateKey),
    (413, Code::Limit),
    (422, Code::Rejected),
    (429, Code::RateLimited),
    (503, Code::Unavailable),
];

/// The retry header the stubs send, in world ticks.
pub const RETRY_HEADER: &str = "x-cwr-retry-after-ticks";

/// How one adapter's responses are normalised.
#[derive(Debug, Clone)]
pub struct Rules {
    /// Qualified adapter name.
    pub adapter: String,
    /// Failure codes the adapter contract declares.
    pub failures: BTreeSet<Code>,
    /// `STATUS_CODES[adapter]` from live.py: status to "ok" or a code name. `None`: wire protocol.
    pub status_codes: Option<BTreeMap<u16, String>>,
    /// Body field that carries the service's retry delay, if declared.
    pub retry_after_field: Option<String>,
    /// Service retry units per world tick numerator: ticks = ceil(service value * ratio).
    pub retry_after_ratio: f64,
    /// The selected fallback for undeclared statuses, already reduced to a declared code.
    pub undeclared: Code,
}

impl Rules {
    /// Builds the rules; `undeclared_status` is honoured only when it is `UNAVAILABLE` or
    /// `INVALID` and the adapter declares it, else the fallback is `UNAVAILABLE`.
    pub fn new(
        adapter: &str,
        failures: BTreeSet<Code>,
        status_codes: Option<BTreeMap<u16, String>>,
        retry_after_field: Option<String>,
        retry_after_ratio: Option<f64>,
        undeclared_status: Option<&str>,
    ) -> Rules {
        let undeclared = match undeclared_status.and_then(|s| s.parse::<Code>().ok()) {
            Some(c @ (Code::Invalid | Code::Unavailable)) if failures.contains(&c) => c,
            _ => Code::Unavailable,
        };
        Rules {
            adapter: adapter.to_string(),
            failures,
            status_codes,
            retry_after_field,
            retry_after_ratio: retry_after_ratio
                .filter(|r| r.is_finite() && *r > 0.0)
                .unwrap_or(1.0),
            undeclared,
        }
    }

    /// `code` if the adapter declares it, else the undeclared fallback (always declared).
    pub fn declared_or_fallback(&self, code: Code) -> Code {
        if self.failures.contains(&code) {
            code
        } else {
            self.undeclared
        }
    }

    /// A code the gateway produces itself, whatever the service said: `TIMEOUT` (every adapter has
    /// a `timeout_ms`) and `DUPLICATE_KEY` from the key store (every `key` adapter). The compiler
    /// refuses an adapter that omits the code (CWR-F002), so it is never patched to a fallback: an
    /// undeclared one is a defect of the Build (kernel.md section 7).
    pub fn own(&self, code: Code, detail: impl Into<String>) -> Normalised {
        let detail = detail.into();
        if self.failures.contains(&code) {
            Normalised::Fail {
                code,
                retry_after: None,
                detail,
            }
        } else {
            Normalised::Fail {
                code: Code::InvariantViolation,
                retry_after: None,
                detail: format!(
                    "{detail} ({} is not declared by {}; the compiler refuses such an adapter)",
                    code.as_str(),
                    self.adapter
                ),
            }
        }
    }

    /// A code derived from the service's answer: `code` if the adapter declares it, else the
    /// contract's `undeclared_status` (clause 7.2).
    pub fn fail(
        &self,
        code: Code,
        retry_after: Option<u64>,
        detail: impl Into<String>,
    ) -> Normalised {
        let detail = detail.into();
        let mapped = self.declared_or_fallback(code);
        let detail = if mapped == code {
            detail
        } else {
            format!(
                "{detail} ({} is not declared by {}; mapped to its undeclared_status {})",
                code.as_str(),
                self.adapter,
                mapped.as_str()
            )
        };
        Normalised::Fail {
            code: mapped,
            retry_after: if mapped == Code::RateLimited {
                retry_after
            } else {
                None
            },
            detail,
        }
    }
}

/// The normaliser's answer.
#[derive(Debug, Clone, PartialEq)]
pub enum Normalised {
    /// A success: the decoded body (live.py mapping) or the unwrapped `result` (wire protocol).
    Body(Value),
    /// A declared failure.
    Fail {
        /// Envelope code, always one the contract declares.
        code: Code,
        /// World ticks to wait before retrying (RATE_LIMITED only).
        retry_after: Option<u64>,
        /// Explanation of the failure.
        detail: String,
    },
}

fn positive_int(v: &Value) -> Option<f64> {
    match v {
        Value::Number(n) if n.as_u64().map(|x| x > 0).unwrap_or(false) => n.as_f64(),
        Value::String(s) => s
            .trim()
            .parse::<u64>()
            .ok()
            .filter(|x| *x > 0)
            .map(|x| x as f64),
        _ => None,
    }
}

fn retry_after(
    rules: &Rules,
    body: Option<&Value>,
    headers: &BTreeMap<String, String>,
) -> Option<u64> {
    let mut candidates: Vec<Option<&Value>> = Vec::new();
    if let Some(f) = &rules.retry_after_field {
        candidates.push(body.and_then(|b| b.get(f)));
    }
    let header = headers.get(RETRY_HEADER).map(|h| Value::String(h.clone()));
    candidates.push(header.as_ref());
    candidates.push(body.and_then(|b| b.get("retry_after")));
    let raw = candidates.into_iter().flatten().find_map(positive_int)?;
    Some((raw * rules.retry_after_ratio).ceil().max(1.0) as u64)
}

fn receipt_detail(base: &str, body: Option<&Value>) -> String {
    match body.and_then(|b| b.get("receipt")) {
        Some(r) => format!("{base}; original receipt: {}", canonical_json(r)),
        None => base.to_string(),
    }
}

fn mapped_failure(
    rules: &Rules,
    code: Code,
    status: u16,
    body: Option<&Value>,
    headers: &BTreeMap<String, String>,
) -> Normalised {
    match code {
        Code::RateLimited => match retry_after(rules, body, headers) {
            Some(t) => rules.fail(Code::RateLimited, Some(t), format!("the service rate-limited the call (HTTP {status}); wait {t} ticks")),
            None => rules.fail(Code::Invalid, None, format!("HTTP {status} rate limit without a positive retry_after: malformed response")),
        },
        Code::DuplicateKey => rules.fail(code, None, receipt_detail(&format!("the service reports the idempotency key was reused with a different payload (HTTP {status})"), body)),
        _ => {
            let reason = body.and_then(|b| b.get("detail")).and_then(Value::as_str).map(|d| format!(": {d}")).unwrap_or_default();
            rules.fail(code, None, format!("the service answered HTTP {status}{reason}"))
        }
    }
}

/// Normalises one raw response (`None`: replay cassette miss).
pub fn normalise(rules: &Rules, raw: Option<&RawResponse>) -> Normalised {
    let (status, headers, text) = match raw {
        None => {
            return rules.fail(
                Code::Unavailable,
                None,
                "replay cassette has no entry for this request; no socket was opened",
            )
        }
        Some(RawResponse::Timeout) => {
            return rules.own(
                Code::Timeout,
                "no response within the contract's timeout_ms of host time",
            )
        }
        Some(RawResponse::TransportError { error }) if error == super::live::RESPONSE_TOO_LARGE => {
            return rules.fail(
                Code::Invalid,
                None,
                format!(
                    "the response exceeded {} bytes and was abandoned: malformed response",
                    super::live::MAX_RESPONSE_BYTES
                ),
            )
        }
        Some(RawResponse::TransportError { error }) => {
            return rules.fail(Code::Unavailable, None, format!("transport error: {error}"))
        }

        Some(RawResponse::Http {
            status,
            headers,
            body,
        }) => (*status, headers, body),
    };
    let body: Option<Value> = serde_json::from_str(text).ok();
    if let Some(map) = &rules.status_codes {
        return match map.get(&status).map(String::as_str) {
            None => Normalised::Fail { code: rules.undeclared, retry_after: None, detail: format!("the service answered HTTP {status}, a status {} does not declare; mapped to its undeclared_status {}", rules.adapter, rules.undeclared.as_str()) },
            Some("ok") => match body {
                Some(b) => Normalised::Body(b),
                None => rules.fail(Code::Invalid, None, format!("HTTP {status} body is not JSON: malformed response")),
            },
            // live.py's STATUS_CODES is checked against `failures` by the compiler's contract tests;
            // a target outside them is a defect of the Build, never patched to a fallback here.
            Some(name) => match name.parse::<Code>() {
                Ok(code) if rules.failures.contains(&code) => mapped_failure(rules, code, status, body.as_ref(), headers),
                _ => Normalised::Fail { code: Code::InvariantViolation, retry_after: None, detail: format!("STATUS_CODES maps HTTP {status} to {name:?}, which {} does not declare in failures", rules.adapter) },
            },
        };
    }
    if (200..300).contains(&status) {
        return match body {
            Some(Value::Object(o)) if o.len() == 1 && o.contains_key("result") => {
                Normalised::Body(o["result"].clone())
            }
            _ => rules.fail(
                Code::Invalid,
                None,
                format!("HTTP {status} body is not {{\"result\": <value>}}: malformed response"),
            ),
        };
    }
    let Some(code) = WIRE_CODES
        .iter()
        .find(|(s, _)| *s == status)
        .map(|(_, c)| *c)
    else {
        return Normalised::Fail { code: rules.undeclared, retry_after: None, detail: format!("the service answered HTTP {status}, a status {} does not declare; mapped to its undeclared_status {}", rules.adapter, rules.undeclared.as_str()) };
    };
    if body
        .as_ref()
        .and_then(|b| b.get("code"))
        .and_then(Value::as_str)
        != Some(code.as_str())
    {
        return rules.fail(
            Code::Invalid,
            None,
            format!(
                "HTTP {status} body does not carry code {}: malformed response",
                code.as_str()
            ),
        );
    }
    mapped_failure(rules, code, status, body.as_ref(), headers)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn http(status: u16, body: &str) -> RawResponse {
        RawResponse::Http {
            status,
            headers: BTreeMap::new(),
            body: body.into(),
        }
    }

    fn search() -> Rules {
        let f = [
            Code::Invalid,
            Code::Limit,
            Code::RateLimited,
            Code::Unavailable,
            Code::Timeout,
        ]
        .into_iter()
        .collect();
        Rules::new("search.query", f, None, None, None, None)
    }

    fn status_rules() -> Rules {
        let f = [Code::NotFound, Code::Unavailable, Code::Timeout]
            .into_iter()
            .collect();
        Rules::new("publish.status", f, None, None, None, Some("INVALID"))
    }

    fn code(n: &Normalised) -> Option<Code> {
        match n {
            Normalised::Fail { code, .. } => Some(*code),
            Normalised::Body(_) => None,
        }
    }

    #[test]
    fn wire_protocol_table() {
        let r = search();
        assert_eq!(
            normalise(&r, Some(&http(200, r#"{"result":[]}"#))),
            Normalised::Body(serde_json::json!([]))
        );
        assert_eq!(
            code(&normalise(&r, Some(&http(200, r#"{"malformed":true}"#)))),
            Some(Code::Invalid)
        );
        assert_eq!(
            code(&normalise(
                &r,
                Some(&http(503, r#"{"code":"UNAVAILABLE"}"#))
            )),
            Some(Code::Unavailable)
        );
        assert_eq!(
            code(&normalise(&r, Some(&http(422, r#"{"code":"REJECTED"}"#)))),
            Some(Code::Unavailable)
        );
        assert_eq!(
            code(&normalise(&r, Some(&http(418, "")))),
            Some(Code::Unavailable)
        );
        assert_eq!(
            code(&normalise(&r, Some(&RawResponse::Timeout))),
            Some(Code::Timeout)
        );
        assert_eq!(
            code(&normalise(
                &r,
                Some(&RawResponse::TransportError {
                    error: "ConnectionResetError".into()
                })
            )),
            Some(Code::Unavailable)
        );
        assert_eq!(code(&normalise(&r, None)), Some(Code::Unavailable));
        match normalise(
            &r,
            Some(&http(429, r#"{"code":"RATE_LIMITED","retry_after":7}"#)),
        ) {
            Normalised::Fail {
                code: Code::RateLimited,
                retry_after: Some(7),
                ..
            } => {}
            other => panic!("{other:?}"),
        }
        assert_eq!(
            code(&normalise(
                &r,
                Some(&http(429, r#"{"code":"RATE_LIMITED"}"#))
            )),
            Some(Code::Invalid)
        );
    }

    #[test]
    fn never_an_undeclared_code() {
        // publish.status declares neither INVALID nor RATE_LIMITED nor REJECTED: an undeclared
        // undeclared_status is ignored and everything falls back to UNAVAILABLE.
        let r = status_rules();
        assert_eq!(r.undeclared, Code::Unavailable);
        for resp in [
            http(200, "{}"),
            http(400, r#"{"code":"INVALID"}"#),
            http(422, r#"{"code":"REJECTED"}"#),
            http(429, r#"{"code":"RATE_LIMITED","retry_after":3}"#),
            http(500, ""),
        ] {
            let n = normalise(&r, Some(&resp));
            let c = code(&n).unwrap();
            assert!(r.failures.contains(&c), "{resp:?} -> {n:?}");
        }
        assert_eq!(
            code(&normalise(&r, Some(&http(404, r#"{"code":"NOT_FOUND"}"#)))),
            Some(Code::NotFound)
        );
    }

    #[test]
    fn live_status_codes_and_ratio() {
        let f = [
            Code::Invalid,
            Code::DuplicateKey,
            Code::RateLimited,
            Code::Unavailable,
            Code::Timeout,
        ]
        .into_iter()
        .collect();
        let map: BTreeMap<u16, String> =
            [(201, "ok"), (409, "DUPLICATE_KEY"), (429, "RATE_LIMITED")]
                .into_iter()
                .map(|(s, c)| (s, c.to_string()))
                .collect();
        let r = Rules::new(
            "publish.post",
            f,
            Some(map),
            Some("retry_after_ticks".into()),
            Some(0.5),
            Some("INVALID"),
        );
        assert_eq!(r.undeclared, Code::Invalid);
        assert_eq!(
            normalise(&r, Some(&http(201, r#"{"id":"p"}"#))),
            Normalised::Body(serde_json::json!({"id": "p"}))
        );
        assert_eq!(
            code(&normalise(&r, Some(&http(200, "{}")))),
            Some(Code::Invalid)
        );
        match normalise(&r, Some(&http(429, r#"{"retry_after_ticks":9}"#))) {
            Normalised::Fail {
                code: Code::RateLimited,
                retry_after: Some(5),
                ..
            } => {}
            other => panic!("{other:?}"),
        }
        match normalise(&r, Some(&http(409, r#"{"receipt":{"id":"p"}}"#))) {
            Normalised::Fail {
                code: Code::DuplicateKey,
                detail,
                ..
            } => assert!(detail.contains(r#"{"id":"p"}"#)),
            other => panic!("{other:?}"),
        }
    }
}
