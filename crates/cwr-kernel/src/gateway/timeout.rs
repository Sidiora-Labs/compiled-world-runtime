// Copyright Sidiora Labs Inc. CWR® 2026-2027
// SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0

//! Host-time deadlines for adapter calls (kernel.md sections 6 and 7; clause 7.6).
//!
//! `timeout_ms` is the only place host time enters a rule. The gateway measures it on the host
//! clock as one total deadline over connect, send and receive, so a slow service cannot stretch a
//! call by answering byte by byte. However long the host waits, the call still advances
//! `world_time` by exactly `tick_per_primitive`: the tick is applied by the atomic contract check
//! (step 5), never here, and nothing in this module is visible to the model.

use std::time::{Duration, Instant};

/// A total host-time budget for one adapter call.
#[derive(Debug, Clone, Copy)]
pub struct Deadline {
    at: Instant,
    budget_ms: u64,
}

impl Deadline {
    /// A deadline `timeout_ms` from now.
    pub fn after_ms(timeout_ms: u64) -> Deadline {
        Deadline {
            at: Instant::now() + Duration::from_millis(timeout_ms),
            budget_ms: timeout_ms,
        }
    }

    /// The contract's `timeout_ms` this deadline was made from.
    pub fn budget_ms(&self) -> u64 {
        self.budget_ms
    }

    /// Time left, or `None` once the deadline has passed. Never zero: socket timeouts of zero
    /// mean "block forever" on some platforms.
    pub fn remaining(&self) -> Option<Duration> {
        let left = self.at.saturating_duration_since(Instant::now());
        if left.is_zero() {
            None
        } else {
            Some(left.max(Duration::from_millis(1)))
        }
    }

    /// Whether the deadline has passed.
    pub fn expired(&self) -> bool {
        self.remaining().is_none()
    }
}

/// The contract's `timeout_ms`, if it is a positive integer. world-source.md section 7 makes it
/// mandatory and positive; the compiler refuses anything else, so a missing value here is a
/// defect the gateway reports instead of inventing a default.
pub fn contract_timeout_ms(raw: &serde_json::Value) -> Option<u64> {
    raw.get("timeout_ms")
        .and_then(serde_json::Value::as_u64)
        .filter(|t| *t > 0)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn deadline_expires() {
        let d = Deadline::after_ms(30);
        assert!(d.remaining().is_some());
        assert_eq!(d.budget_ms(), 30);
        std::thread::sleep(Duration::from_millis(40));
        assert!(d.expired());
    }

    #[test]
    fn timeout_from_contract() {
        assert_eq!(
            contract_timeout_ms(&serde_json::json!({"timeout_ms": 3000})),
            Some(3000)
        );
        assert_eq!(
            contract_timeout_ms(&serde_json::json!({"timeout_ms": 0})),
            None
        );
        assert_eq!(contract_timeout_ms(&serde_json::json!({})), None);
    }
}
