// Copyright Sidiora Labs Inc. CWR® 2026-2027
// SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0

//! World clock (kernel.md section 6, clause 1.5).
//!
//! World time is an integer tick counter starting at 0 for each Instance. It advances by
//! `tick_per_primitive` for every executed primitive and by `tick_per_turn` at the end of
//! every executed turn, both taken from the Build's compiled clock. It has no other input:
//! the host clock is never consulted, so world time is a pure function of the ledger.

use crate::{KernelError, Result};

/// The Build's `clock` values. Both are positive by construction of the World Source grammar.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct TickConfig {
    /// Ticks added by every executed primitive; always positive.
    pub tick_per_primitive: u64,
    /// Ticks added when an executed turn ends; always positive.
    pub tick_per_turn: u64,
}

impl TickConfig {
    /// The configuration, refused with [`KernelError::Refused`] when either tick is zero.
    pub fn new(tick_per_primitive: u64, tick_per_turn: u64) -> Result<Self> {
        if tick_per_primitive == 0 || tick_per_turn == 0 {
            return Err(KernelError::Refused("clock ticks must be positive".into()));
        }
        Ok(TickConfig {
            tick_per_primitive,
            tick_per_turn,
        })
    }
}

/// The tick counter of one Instance.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct WorldClock {
    now: u64,
    config: TickConfig,
}

impl WorldClock {
    /// A clock at tick 0.
    pub fn new(config: TickConfig) -> Self {
        WorldClock { now: 0, config }
    }

    /// A clock resumed at a recorded tick (from instance metadata or a snapshot).
    pub fn at(config: TickConfig, now: u64) -> Self {
        WorldClock { now, config }
    }

    /// Current world time in ticks: what `world.now()` returns.
    pub fn now(&self) -> u64 {
        self.now
    }

    /// The tick configuration this clock advances by.
    pub fn config(&self) -> TickConfig {
        self.config
    }

    /// Step 5 of the atomic contract check: an executed primitive advances the clock.
    pub fn advance_primitive(&mut self) -> Result<u64> {
        self.advance(self.config.tick_per_primitive)
    }

    /// End of an executed turn.
    pub fn advance_turn(&mut self) -> Result<u64> {
        self.advance(self.config.tick_per_turn)
    }

    fn advance(&mut self, ticks: u64) -> Result<u64> {
        self.now = self
            .now
            .checked_add(ticks)
            .ok_or_else(|| KernelError::Refused("world clock overflow".into()))?;
        Ok(self.now)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn advances_only_by_configured_ticks() {
        let mut c = WorldClock::new(TickConfig::new(3, 7).unwrap());
        assert_eq!(c.now(), 0);
        assert_eq!(c.advance_primitive().unwrap(), 3);
        assert_eq!(c.advance_primitive().unwrap(), 6);
        assert_eq!(c.advance_turn().unwrap(), 13);
        std::thread::sleep(std::time::Duration::from_millis(20));
        assert_eq!(c.now(), 13, "host time passing does not move world time");
        let resumed = WorldClock::at(c.config(), c.now());
        assert_eq!(resumed, c);
    }

    #[test]
    fn zero_ticks_refused() {
        assert!(TickConfig::new(0, 10).is_err());
        assert!(TickConfig::new(1, 0).is_err());
    }
}
