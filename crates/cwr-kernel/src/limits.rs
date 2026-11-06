// Copyright Sidiora Labs Inc. CWR® 2026-2027
// SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0

//! Per-turn limits (kernel.md section 3; clauses 6.1 and 6.5; decisions.md P13).
//!
//! The five limits come from the Build's compiled `limits` (world.kvx has no defaults, so a Build
//! always names all five). The three process limits (`cpu_ms`, `wall_ms`, `memory_mb`) are enforced
//! on the sandbox process by [`crate::sandbox`]; the two counters (`primitive_calls`,
//! `adapter_calls`) are enforced here, at the RPC boundary and just before the gateway, and the
//! exceeding call is not executed.

use serde_json::{json, Value};

use crate::state::Build;
use crate::{KernelError, Result};

/// The configured limits of one Build.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Limits {
    /// CPU time of the sandbox process per turn, in milliseconds.
    pub cpu_ms: u64,
    /// Wall time of a turn, in milliseconds.
    pub wall_ms: u64,
    /// Memory of the sandbox process, in megabytes.
    pub memory_mb: u64,
    /// Primitive calls allowed per turn.
    pub primitive_calls: u64,
    /// External calls allowed per turn.
    pub adapter_calls: u64,
}

/// Names in the order of world-source.md section 3.
pub const NAMES: [&str; 5] = [
    "cpu_ms",
    "wall_ms",
    "memory_mb",
    "primitive_calls",
    "adapter_calls",
];

impl Limits {
    /// The Build's five limits; a missing or zero limit is [`KernelError::Refused`].
    pub fn of(build: &Build) -> Result<Limits> {
        let g = |k: &str| {
            build
                .limits
                .get(k)
                .copied()
                .filter(|v| *v > 0)
                .ok_or_else(|| {
                    KernelError::Refused(format!("Build {} has no positive limit {k}", build.hash))
                })
        };
        Ok(Limits {
            cpu_ms: g("cpu_ms")?,
            wall_ms: g("wall_ms")?,
            memory_mb: g("memory_mb")?,
            primitive_calls: g("primitive_calls")?,
            adapter_calls: g("adapter_calls")?,
        })
    }
}

/// The per-turn counters. `primitive_calls` counts every RPC that crosses the sandbox boundary
/// (including refused and closure-refused calls); `adapter_calls` counts external calls that
/// reached the gateway.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct Budget {
    /// Primitive calls taken this turn.
    pub primitive_calls: u64,
    /// External calls sent this turn.
    pub adapter_calls: u64,
}

impl Budget {
    /// Counts one RPC; false when it exceeds `primitive_calls` (the call must not execute).
    pub fn take_call(&mut self, limits: &Limits) -> bool {
        self.primitive_calls += 1;
        self.primitive_calls <= limits.primitive_calls
    }

    /// Whether one more external call fits; the caller counts it with [`Budget::take_adapter`]
    /// only when the call is actually sent to the gateway.
    pub fn adapter_fits(&self, limits: &Limits) -> bool {
        self.adapter_calls < limits.adapter_calls
    }

    /// Counts one external call sent to the gateway.
    pub fn take_adapter(&mut self) {
        self.adapter_calls += 1;
    }

    /// Primitive calls still allowed this turn, never negative.
    pub fn remaining_primitive_calls(&self, limits: &Limits) -> u64 {
        limits.primitive_calls.saturating_sub(self.primitive_calls)
    }

    /// External calls still allowed this turn, never negative.
    pub fn remaining_adapter_calls(&self, limits: &Limits) -> u64 {
        limits.adapter_calls.saturating_sub(self.adapter_calls)
    }

    /// `remaining` of the result envelope.
    pub fn remaining(&self, limits: &Limits) -> Value {
        json!({"primitive_calls": self.remaining_primitive_calls(limits), "adapter_calls": self.remaining_adapter_calls(limits)})
    }

    /// The `Limits` record `world.limits()` returns (manual section 5, record Limits): the
    /// configured values, the remaining counters of this turn and the clock ticks.
    pub fn limits_record(
        &self,
        limits: &Limits,
        tick_per_primitive: u64,
        tick_per_turn: u64,
    ) -> Value {
        json!({
            "cpu_ms": limits.cpu_ms,
            "wall_ms": limits.wall_ms,
            "memory_mb": limits.memory_mb,
            "primitive_calls": limits.primitive_calls,
            "adapter_calls": limits.adapter_calls,
            "remaining_primitive_calls": self.remaining_primitive_calls(limits),
            "remaining_adapter_calls": self.remaining_adapter_calls(limits),
            "tick_per_primitive": tick_per_primitive,
            "tick_per_turn": tick_per_turn,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn counters() {
        let l = Limits {
            cpu_ms: 1,
            wall_ms: 1,
            memory_mb: 1,
            primitive_calls: 2,
            adapter_calls: 1,
        };
        let mut b = Budget::default();
        assert!(b.take_call(&l));
        assert!(b.take_call(&l));
        assert!(
            !b.take_call(&l),
            "the third call exceeds primitive_calls = 2"
        );
        assert_eq!(b.remaining_primitive_calls(&l), 0);
        assert!(b.adapter_fits(&l));
        b.take_adapter();
        assert!(!b.adapter_fits(&l));
        assert_eq!(
            b.remaining(&l),
            json!({"primitive_calls": 0, "adapter_calls": 0})
        );
        assert_eq!(
            b.limits_record(&l, 1, 10)["remaining_adapter_calls"],
            json!(0)
        );
    }
}
