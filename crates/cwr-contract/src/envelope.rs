// Copyright Sidiora Labs Inc. CWR® 2026-2027
// SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0

//! The closed failure envelope (spec/cwr/world-source.md section 6, clause 2.3).
//!
//! Exactly eleven codes exist. [`Code`] is a fieldless enum with no catch-all variant,
//! so no other code can be constructed in Rust, and its parser and deserialiser accept
//! only the eleven exact upper-case spellings, so no other code can be parsed.
//! `retry_after` exists only on [`Failure::RateLimited`], so a `retry_after` on any other
//! code cannot be constructed, serialised or deserialised. `retry_after` is a non-negative
//! tick count; `0` is a service saying the call may be repeated at once and stays valid.
//!
//! A `LIMIT` envelope names what was reached: either a [`LimitName`] (a `[limits]` key of
//! `world.kvx`, which also names the per-turn call budgets) or the index of the declared
//! size-rule precondition that failed. An `INVARIANT_VIOLATION` is raised after the
//! preconditions passed, so it never carries a precondition index. [`Envelope::new`] and the
//! deserialiser refuse every other combination with a typed [`EnvelopeError`].
//!
//! ```
//! // The eleven codes and a RATE_LIMITED failure with retry_after are constructible.
//! use cwr_contract::{Code, Failure, Ticks};
//! assert_eq!(Code::ALL.len(), 11);
//! let f = Failure::RateLimited { retry_after: Ticks(5) };
//! assert_eq!(f.code(), Code::RateLimited);
//! assert_eq!(f.retry_after(), Some(Ticks(5)));
//! assert_eq!(Failure::Timeout.retry_after(), None);
//! ```
//!
//! ```compile_fail,E0599
//! // There is no twelfth code.
//! let _ = cwr_contract::Code::Ok;
//! ```
//!
//! ```compile_fail,E0559
//! // retry_after cannot be attached to any code but RATE_LIMITED.
//! let _ = cwr_contract::Failure::Timeout { retry_after: cwr_contract::Ticks(5) };
//! ```
//!
//! ```compile_fail,E0533
//! // RATE_LIMITED cannot be constructed without retry_after.
//! let _ = cwr_contract::Failure::RateLimited;
//! ```

use std::fmt;
use std::num::NonZeroU32;
use std::str::FromStr;

use serde::de::Error as _;
use serde::ser::SerializeMap;
use serde::{Deserialize, Deserializer, Serialize, Serializer};

/// One of the eleven envelope codes.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub enum Code {
    /// `NOT_FOUND`: the target the parameters name does not exist in the world.
    NotFound,
    /// `LOCKED`: the target is held by a lock the caller does not hold.
    Locked,
    /// `CONFLICT`: a visible condition changed since the caller's check, or the action would duplicate a world object.
    Conflict,
    /// `INVALID`: parameters fail the contract's types, ranges or declared validity preconditions.
    Invalid,
    /// `LIMIT`: a declared world limit, size rule or per-turn budget was reached.
    Limit,
    /// `UNAVAILABLE`: an external endpoint was unreachable, returned an undeclared status, or a replay cassette has no entry.
    Unavailable,
    /// `RATE_LIMITED`: an external endpoint throttled the call; always carries `retry_after`.
    RateLimited,
    /// `TIMEOUT`: an external call exceeded the contract's `timeout_ms`.
    Timeout,
    /// `REJECTED`: an external endpoint refused the content for a reason it reported.
    Rejected,
    /// `DUPLICATE_KEY`: an idempotency key was reused with a different payload.
    DuplicateKey,
    /// `INVARIANT_VIOLATION`: the kernel observed an outcome the contract does not allow.
    InvariantViolation,
}

impl Code {
    /// Every code, in the order of world-source.md section 6.
    pub const ALL: [Code; 11] = [
        Code::NotFound,
        Code::Locked,
        Code::Conflict,
        Code::Invalid,
        Code::Limit,
        Code::Unavailable,
        Code::RateLimited,
        Code::Timeout,
        Code::Rejected,
        Code::DuplicateKey,
        Code::InvariantViolation,
    ];

    /// The exact upper-case wire spelling, e.g. `"RATE_LIMITED"`; the only spellings the parser accepts.
    pub const fn as_str(self) -> &'static str {
        match self {
            Code::NotFound => "NOT_FOUND",
            Code::Locked => "LOCKED",
            Code::Conflict => "CONFLICT",
            Code::Invalid => "INVALID",
            Code::Limit => "LIMIT",
            Code::Unavailable => "UNAVAILABLE",
            Code::RateLimited => "RATE_LIMITED",
            Code::Timeout => "TIMEOUT",
            Code::Rejected => "REJECTED",
            Code::DuplicateKey => "DUPLICATE_KEY",
            Code::InvariantViolation => "INVARIANT_VIOLATION",
        }
    }

    /// Manual guidance from world-source.md section 6.
    pub const fn guidance(self) -> &'static str {
        match self {
            Code::NotFound => "Query the visible existence primitive first.",
            Code::Locked => "Unlock, or wait for the holder.",
            Code::Conflict => "Re-check the named conditions and retry once.",
            Code::Invalid => "Fix the call; retrying unchanged cannot succeed.",
            Code::Limit => "Split the work across turns or reduce the size.",
            Code::Unavailable => "The world does not retry; the script may, on a later turn.",
            Code::RateLimited => "Wait retry_after ticks before calling the same adapter.",
            Code::Timeout => "The effect may or may not have happened; use the adapter's status primitive if it has one.",
            Code::Rejected => "Change the content; detail carries the service's reason as content, not guaranteed.",
            Code::DuplicateKey => "Reuse the original receipt or choose a new key.",
            Code::InvariantViolation => {
                "Not an ordinary outcome. The ledger carries the trace and the Build is considered defective."
            }
        }
    }
}

/// An unknown code string.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct UnknownCode(pub String);

impl fmt::Display for UnknownCode {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{:?} is not one of the eleven envelope codes", self.0)
    }
}

impl std::error::Error for UnknownCode {}

impl FromStr for Code {
    type Err = UnknownCode;

    fn from_str(s: &str) -> Result<Self, Self::Err> {
        Code::ALL
            .into_iter()
            .find(|c| c.as_str() == s)
            .ok_or_else(|| UnknownCode(s.to_string()))
    }
}

impl fmt::Display for Code {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

impl Serialize for Code {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        serializer.serialize_str(self.as_str())
    }
}

impl<'de> Deserialize<'de> for Code {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        let s = String::deserialize(deserializer)?;
        s.parse().map_err(D::Error::custom)
    }
}

/// The name of a declared limit a `LIMIT` failure reports: the `[limits]` keys of `world.kvx`
/// (world-source.md section 3). `primitive_calls` and `adapter_calls` are the per-turn call
/// budgets. No other name can be constructed or parsed.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub enum LimitName {
    /// `cpu_ms`: CPU budget per turn script.
    CpuMs,
    /// `wall_ms`: wall budget per turn script.
    WallMs,
    /// `memory_mb`: memory budget of the sandbox process.
    MemoryMb,
    /// `primitive_calls`: primitive calls per turn.
    PrimitiveCalls,
    /// `adapter_calls`: external calls per turn.
    AdapterCalls,
}

impl LimitName {
    /// Every limit name, in the declared order of `[limits]` (equal to `Limits::NAMES`).
    pub const ALL: [LimitName; 5] = [
        LimitName::CpuMs,
        LimitName::WallMs,
        LimitName::MemoryMb,
        LimitName::PrimitiveCalls,
        LimitName::AdapterCalls,
    ];

    /// The exact wire spelling, e.g. `"memory_mb"`; the only spellings the parser accepts.
    pub const fn as_str(self) -> &'static str {
        match self {
            LimitName::CpuMs => "cpu_ms",
            LimitName::WallMs => "wall_ms",
            LimitName::MemoryMb => "memory_mb",
            LimitName::PrimitiveCalls => "primitive_calls",
            LimitName::AdapterCalls => "adapter_calls",
        }
    }
}

impl FromStr for LimitName {
    type Err = EnvelopeError;

    fn from_str(s: &str) -> Result<Self, Self::Err> {
        LimitName::ALL
            .into_iter()
            .find(|l| l.as_str() == s)
            .ok_or_else(|| EnvelopeError::UnknownLimit(s.to_string()))
    }
}

impl fmt::Display for LimitName {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

impl Serialize for LimitName {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        serializer.serialize_str(self.as_str())
    }
}

impl<'de> Deserialize<'de> for LimitName {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        let s = String::deserialize(deserializer)?;
        s.parse().map_err(D::Error::custom)
    }
}

/// Why a combination of code and payloads is not a failure envelope.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum EnvelopeError {
    /// `retry_after` on a code other than `RATE_LIMITED`.
    RetryAfterNotAllowed(Code),
    /// `RATE_LIMITED` without `retry_after`.
    RetryAfterRequired,
    /// `retry_after` that is not a non-negative integer; carries the JSON text.
    RetryAfterNotTicks(String),
    /// `limit` on a code other than `LIMIT`.
    LimitNotAllowed(Code),
    /// A `limit` name that is not a [`LimitName`].
    UnknownLimit(String),
    /// `LIMIT` naming neither a limit nor a failed precondition.
    LimitUnnamed,
    /// A precondition index on a code that is never raised by a precondition.
    PreconditionNotAllowed(Code),
}

impl fmt::Display for EnvelopeError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            EnvelopeError::RetryAfterNotAllowed(code) => {
                write!(f, "retry_after is allowed only on RATE_LIMITED, not on {code}")
            }
            EnvelopeError::RetryAfterRequired => {
                f.write_str("RATE_LIMITED requires retry_after in ticks")
            }
            EnvelopeError::RetryAfterNotTicks(value) => write!(
                f,
                "retry_after must be a non-negative integer number of ticks, got {value}"
            ),
            EnvelopeError::LimitNotAllowed(code) => {
                write!(f, "limit is allowed only on LIMIT, not on {code}")
            }
            EnvelopeError::UnknownLimit(name) => write!(
                f,
                "{name:?} is not a declared limit; LIMIT names one of cpu_ms, wall_ms, memory_mb, primitive_calls, adapter_calls"
            ),
            EnvelopeError::LimitUnnamed => f.write_str(
                "LIMIT must name the limit or the failed size-rule precondition; both are null",
            ),
            EnvelopeError::PreconditionNotAllowed(code) => write!(
                f,
                "{code} is raised after the preconditions passed and carries no precondition index"
            ),
        }
    }
}

impl std::error::Error for EnvelopeError {}

/// World time in kernel-counted ticks.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(transparent)]
pub struct Ticks(pub u64);

/// A failure with exactly the payload its code allows.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Failure {
    /// `NOT_FOUND`; carries no payload.
    NotFound,
    /// `LOCKED`; carries no payload.
    Locked,
    /// `CONFLICT`; carries no payload.
    Conflict,
    /// `INVALID`; carries no payload.
    Invalid,
    /// `limit` names the limit when the failure comes from a declared world limit or per-turn
    /// budget; a size-rule precondition names itself through the envelope's precondition.
    Limit {
        /// The limit that was reached, or `None` when a declared precondition failed instead.
        limit: Option<LimitName>,
    },
    /// `UNAVAILABLE`; carries no payload.
    Unavailable,
    /// `retry_after` is mandatory and counted in world ticks.
    RateLimited {
        /// World ticks to wait before calling the same adapter again; `0` allows an
        /// immediate repeat.
        retry_after: Ticks,
    },
    /// `TIMEOUT`; carries no payload.
    Timeout,
    /// `REJECTED`; carries no payload.
    Rejected,
    /// `DUPLICATE_KEY`; carries no payload.
    DuplicateKey,
    /// `INVARIANT_VIOLATION`; carries no payload.
    InvariantViolation,
}

impl Failure {
    /// The envelope code of this failure; total over every variant.
    pub fn code(&self) -> Code {
        match self {
            Failure::NotFound => Code::NotFound,
            Failure::Locked => Code::Locked,
            Failure::Conflict => Code::Conflict,
            Failure::Invalid => Code::Invalid,
            Failure::Limit { .. } => Code::Limit,
            Failure::Unavailable => Code::Unavailable,
            Failure::RateLimited { .. } => Code::RateLimited,
            Failure::Timeout => Code::Timeout,
            Failure::Rejected => Code::Rejected,
            Failure::DuplicateKey => Code::DuplicateKey,
            Failure::InvariantViolation => Code::InvariantViolation,
        }
    }

    /// `Some` only for [`Failure::RateLimited`], `None` for every other code.
    pub fn retry_after(&self) -> Option<Ticks> {
        match self {
            Failure::RateLimited { retry_after } => Some(*retry_after),
            _ => None,
        }
    }

    /// The limit name of a [`Failure::Limit`] that names one, `None` otherwise.
    pub fn limit(&self) -> Option<LimitName> {
        match self {
            Failure::Limit { limit } => *limit,
            _ => None,
        }
    }

    /// Builds a failure from a code and the optional payloads, refusing any payload the code
    /// does not carry and a RATE_LIMITED without retry_after.
    pub fn from_parts(
        code: Code,
        retry_after: Option<Ticks>,
        limit: Option<LimitName>,
    ) -> Result<Failure, EnvelopeError> {
        if code != Code::RateLimited && retry_after.is_some() {
            return Err(EnvelopeError::RetryAfterNotAllowed(code));
        }
        if code != Code::Limit && limit.is_some() {
            return Err(EnvelopeError::LimitNotAllowed(code));
        }
        Ok(match code {
            Code::NotFound => Failure::NotFound,
            Code::Locked => Failure::Locked,
            Code::Conflict => Failure::Conflict,
            Code::Invalid => Failure::Invalid,
            Code::Limit => Failure::Limit { limit },
            Code::Unavailable => Failure::Unavailable,
            Code::RateLimited => match retry_after {
                Some(retry_after) => Failure::RateLimited { retry_after },
                None => return Err(EnvelopeError::RetryAfterRequired),
            },
            Code::Timeout => Failure::Timeout,
            Code::Rejected => Failure::Rejected,
            Code::DuplicateKey => Failure::DuplicateKey,
            Code::InvariantViolation => Failure::InvariantViolation,
        })
    }
}

/// The failure envelope value:
/// `{"code", "primitive", "precondition", ["retry_after",] "limit", "detail"}`.
/// `retry_after` is present only for RATE_LIMITED; every other key is always present.
/// Build one with [`Envelope::new`], which refuses the combinations the module documentation
/// lists; the deserialiser applies the same rules.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Envelope {
    /// The code together with exactly the payload that code allows.
    pub failure: Failure,
    /// The primitive or adapter the failure belongs to; `None` for turn-level failures.
    pub primitive: Option<String>,
    /// One-based index of the failing declared precondition, `None` otherwise.
    pub precondition: Option<NonZeroU32>,
    /// Free text from the world or the service; content, not interface.
    pub detail: Option<String>,
}

impl Envelope {
    /// Builds an envelope, refusing a `LIMIT` that names neither a limit nor a precondition
    /// and a precondition index on `INVARIANT_VIOLATION`.
    pub fn new(
        failure: Failure,
        primitive: Option<String>,
        precondition: Option<NonZeroU32>,
        detail: Option<String>,
    ) -> Result<Envelope, EnvelopeError> {
        match (&failure, precondition) {
            (Failure::Limit { limit: None }, None) => return Err(EnvelopeError::LimitUnnamed),
            (Failure::InvariantViolation, Some(_)) => {
                return Err(EnvelopeError::PreconditionNotAllowed(
                    Code::InvariantViolation,
                ))
            }
            _ => {}
        }
        Ok(Envelope {
            failure,
            primitive,
            precondition,
            detail,
        })
    }

    /// The envelope code of [`Envelope::failure`].
    pub fn code(&self) -> Code {
        self.failure.code()
    }
}

impl Serialize for Envelope {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        let rate_limited = self.failure.retry_after();
        let mut map = serializer.serialize_map(Some(if rate_limited.is_some() { 6 } else { 5 }))?;
        map.serialize_entry("code", &self.failure.code())?;
        map.serialize_entry("primitive", &self.primitive)?;
        map.serialize_entry("precondition", &self.precondition)?;
        if let Some(ticks) = rate_limited {
            map.serialize_entry("retry_after", &ticks)?;
        }
        map.serialize_entry("limit", &self.failure.limit())?;
        map.serialize_entry("detail", &self.detail)?;
        map.end()
    }
}

fn required<'de, D: Deserializer<'de>, T: Deserialize<'de>>(d: D) -> Result<T, D::Error> {
    T::deserialize(d)
}

fn present<'de, D: Deserializer<'de>>(d: D) -> Result<Option<serde_json::Value>, D::Error> {
    serde_json::Value::deserialize(d).map(Some)
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct RawEnvelope {
    #[serde(deserialize_with = "required")]
    code: Code,
    #[serde(deserialize_with = "required")]
    primitive: Option<String>,
    #[serde(deserialize_with = "required")]
    precondition: Option<NonZeroU32>,
    #[serde(default, deserialize_with = "present")]
    retry_after: Option<serde_json::Value>,
    #[serde(deserialize_with = "required")]
    limit: Option<LimitName>,
    #[serde(deserialize_with = "required")]
    detail: Option<String>,
}

impl<'de> Deserialize<'de> for Envelope {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        let raw = RawEnvelope::deserialize(deserializer)?;
        let retry_after = match raw.retry_after {
            None => None,
            Some(value) => match value.as_u64() {
                Some(ticks) if value.is_u64() => Some(Ticks(ticks)),
                _ => {
                    return Err(D::Error::custom(EnvelopeError::RetryAfterNotTicks(
                        value.to_string(),
                    )))
                }
            },
        };
        let failure =
            Failure::from_parts(raw.code, retry_after, raw.limit).map_err(D::Error::custom)?;
        Envelope::new(failure, raw.primitive, raw.precondition, raw.detail)
            .map_err(D::Error::custom)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const SPEC_CODES: [&str; 11] = [
        "NOT_FOUND",
        "LOCKED",
        "CONFLICT",
        "INVALID",
        "LIMIT",
        "UNAVAILABLE",
        "RATE_LIMITED",
        "TIMEOUT",
        "REJECTED",
        "DUPLICATE_KEY",
        "INVARIANT_VIOLATION",
    ];

    #[test]
    fn exactly_eleven_codes_round_trip() {
        assert_eq!(Code::ALL.len(), 11);
        let names: Vec<&str> = Code::ALL.iter().map(|c| c.as_str()).collect();
        assert_eq!(names, SPEC_CODES);
        for code in Code::ALL {
            let json = serde_json::to_string(&code).unwrap();
            assert_eq!(serde_json::from_str::<Code>(&json).unwrap(), code);
            assert_eq!(code.as_str().parse::<Code>().unwrap(), code);
        }
    }

    #[test]
    fn unknown_codes_never_parse() {
        for bad in [
            "OK",
            "ERROR",
            "not_found",
            "Not_Found",
            "NOTFOUND",
            " LOCKED",
            "LOCKED ",
            "",
            "RETRY",
            "FORBIDDEN",
        ] {
            assert!(bad.parse::<Code>().is_err(), "{bad}");
            assert!(
                serde_json::from_str::<Code>(&serde_json::to_string(bad).unwrap()).is_err(),
                "{bad}"
            );
        }
        assert!(serde_json::from_str::<Code>("3").is_err());
    }

    #[test]
    fn retry_after_only_on_rate_limited() {
        let ok = r#"{"code":"RATE_LIMITED","primitive":"publish.post","precondition":null,"retry_after":30,"limit":null,"detail":"slow down"}"#;
        let env: Envelope = serde_json::from_str(ok).unwrap();
        assert_eq!(
            env.failure,
            Failure::RateLimited {
                retry_after: Ticks(30)
            }
        );
        assert_eq!(serde_json::to_string(&env).unwrap(), ok);
        for bad in [
            r#"{"code":"RATE_LIMITED","primitive":"p.q","precondition":null,"limit":null,"detail":null}"#,
            r#"{"code":"RATE_LIMITED","primitive":"p.q","precondition":null,"retry_after":null,"limit":null,"detail":null}"#,
            r#"{"code":"RATE_LIMITED","primitive":"p.q","precondition":null,"retry_after":1.5,"limit":null,"detail":null}"#,
            r#"{"code":"RATE_LIMITED","primitive":"p.q","precondition":null,"retry_after":-1,"limit":null,"detail":null}"#,
            r#"{"code":"TIMEOUT","primitive":"p.q","precondition":null,"retry_after":3,"limit":null,"detail":null}"#,
            r#"{"code":"TIMEOUT","primitive":"p.q","precondition":null,"retry_after":null,"limit":null,"detail":null}"#,
            r#"{"code":"LOCKED","primitive":"p.q","precondition":null,"limit":"cpu_ms","detail":null}"#,
            r#"{"code":"LOCKED","primitive":"p.q","precondition":0,"limit":null,"detail":null}"#,
            r#"{"code":"LOCKED","primitive":"p.q","precondition":1,"limit":null,"detail":null,"extra":1}"#,
            r#"{"code":"LOCKED","primitive":"p.q","precondition":1,"limit":null}"#,
            r#"{"code":"GONE","primitive":"p.q","precondition":1,"limit":null,"detail":null}"#,
            r#"{"code":"LIMIT","primitive":"p.q","precondition":null,"limit":null,"detail":null}"#,
            r#"{"code":"LIMIT","primitive":"p.q","precondition":null,"limit":"bogus_limit","detail":null}"#,
            r#"{"code":"LIMIT","primitive":"p.q","precondition":1,"limit":"CPU_MS","detail":null}"#,
            r#"{"code":"INVARIANT_VIOLATION","primitive":"p.q","precondition":1,"limit":null,"detail":null}"#,
        ] {
            assert!(serde_json::from_str::<Envelope>(bad).is_err(), "{bad}");
        }
        // retry_after 0 is a valid immediate-retry signal; LIMIT may name a size-rule
        // precondition instead of a limit.
        for good in [
            r#"{"code":"RATE_LIMITED","primitive":"p.q","precondition":null,"retry_after":0,"limit":null,"detail":null}"#,
            r#"{"code":"LIMIT","primitive":"kv.set","precondition":2,"limit":null,"detail":null}"#,
            r#"{"code":"LIMIT","primitive":null,"precondition":null,"limit":"adapter_calls","detail":null}"#,
            r#"{"code":"INVARIANT_VIOLATION","primitive":"p.q","precondition":null,"limit":null,"detail":null}"#,
        ] {
            let env: Envelope = serde_json::from_str(good).unwrap();
            assert_eq!(serde_json::to_string(&env).unwrap(), good);
        }
    }

    #[test]
    fn envelope_errors_are_typed() {
        assert_eq!(
            Failure::from_parts(Code::Timeout, Some(Ticks(1)), None),
            Err(EnvelopeError::RetryAfterNotAllowed(Code::Timeout))
        );
        assert_eq!(
            Failure::from_parts(Code::Locked, None, Some(LimitName::CpuMs)),
            Err(EnvelopeError::LimitNotAllowed(Code::Locked))
        );
        assert_eq!(
            Failure::from_parts(Code::RateLimited, None, None),
            Err(EnvelopeError::RetryAfterRequired)
        );
        assert_eq!(
            Envelope::new(Failure::Limit { limit: None }, None, None, None),
            Err(EnvelopeError::LimitUnnamed)
        );
        assert_eq!(
            Envelope::new(Failure::InvariantViolation, None, NonZeroU32::new(1), None),
            Err(EnvelopeError::PreconditionNotAllowed(
                Code::InvariantViolation
            ))
        );
        assert_eq!(
            "bogus".parse::<LimitName>(),
            Err(EnvelopeError::UnknownLimit("bogus".into()))
        );
        let names: Vec<&str> = LimitName::ALL.iter().map(|l| l.as_str()).collect();
        assert_eq!(names, crate::world::Limits::NAMES);
        assert_eq!(
            EnvelopeError::RetryAfterNotAllowed(Code::Timeout).to_string(),
            "retry_after is allowed only on RATE_LIMITED, not on TIMEOUT"
        );
    }

    #[test]
    fn every_code_has_a_constructible_envelope_and_round_trips() {
        for code in Code::ALL {
            let retry = (code == Code::RateLimited).then_some(Ticks(7));
            let limit = (code == Code::Limit).then_some(LimitName::PrimitiveCalls);
            let failure = Failure::from_parts(code, retry, limit).unwrap();
            let precondition = if code == Code::InvariantViolation {
                None
            } else {
                NonZeroU32::new(2)
            };
            let env = Envelope::new(failure, Some("fs.delete".into()), precondition, None).unwrap();
            let text = serde_json::to_string(&env).unwrap();
            assert_eq!(text.contains("retry_after"), code == Code::RateLimited);
            assert_eq!(serde_json::from_str::<Envelope>(&text).unwrap(), env);
            assert_eq!(env.code(), code);
        }
        assert!(Failure::from_parts(Code::RateLimited, None, None).is_err());
        assert!(Failure::from_parts(Code::Timeout, Some(Ticks(1)), None).is_err());
    }
}
