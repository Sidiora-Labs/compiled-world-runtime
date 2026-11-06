// Copyright Sidiora Labs Inc. CWR® 2026-2027
// SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0

//! Instance randomness (kernel.md section 6, clause 6.6).
//!
//! `world.random()` is the only source of randomness inside a world. It is a PCG32 stream
//! (PCG-XSH-RR, 64-bit state, 32-bit output; O'Neill 2014) seeded once from the Instance seed
//! at creation with a fixed stream constant and advanced only by draws. Its state is part of
//! the instance metadata and of every snapshot. The Instance id is deliberately not an input,
//! so two Instances with equal seed and equal ledger prefix produce equal draws.

use serde_json::{json, Value};

use crate::{KernelError, Result};

const MULTIPLIER: u64 = 6_364_136_223_846_793_005;
/// Fixed stream selector shared by every Instance (`initseq` of pcg32_srandom_r).
pub const STREAM: u64 = 0xda3e_39cb_94b9_5bdb;

/// Serialisable RNG state: PCG state, increment and the number of draws taken.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct RngState {
    /// PCG32 internal state.
    pub state: u64,
    /// PCG32 stream increment (always odd).
    pub inc: u64,
    /// Number of draws taken since seeding.
    pub draws: u64,
}

impl RngState {
    /// JSON form. u64 values are written as decimal strings so no JSON reader can round them.
    pub fn to_json(&self) -> Value {
        json!({"state": self.state.to_string(), "inc": self.inc.to_string(), "draws": self.draws})
    }

    /// Parses the JSON form; a missing or non-numeric field is [`KernelError::Refused`].
    pub fn from_json(v: &Value) -> Result<Self> {
        let num = |k: &str| -> Result<u64> {
            match v.get(k) {
                Some(Value::String(s)) => s
                    .parse::<u64>()
                    .map_err(|_| KernelError::Refused(format!("rng.{k} is not a u64"))),
                Some(Value::Number(n)) => n
                    .as_u64()
                    .ok_or_else(|| KernelError::Refused(format!("rng.{k} is not a u64"))),
                _ => Err(KernelError::Refused(format!("rng.{k} missing"))),
            }
        };
        let st = RngState {
            state: num("state")?,
            inc: num("inc")?,
            draws: num("draws")?,
        };
        if st.inc & 1 == 0 {
            return Err(KernelError::Refused("rng.inc must be odd".into()));
        }
        Ok(st)
    }
}

/// PCG32 generator.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Pcg32 {
    st: RngState,
}

impl Pcg32 {
    /// pcg32_srandom_r(initstate, initseq).
    pub fn with_stream(initstate: u64, initseq: u64) -> Self {
        let mut g = Pcg32 {
            st: RngState {
                state: 0,
                inc: (initseq << 1) | 1,
                draws: 0,
            },
        };
        g.step();
        g.st.state = g.st.state.wrapping_add(initstate);
        g.step();
        g
    }

    /// The Instance stream for `seed`.
    pub fn seeded(seed: u64) -> Self {
        Self::with_stream(seed, STREAM)
    }

    /// A generator resumed exactly at `st`.
    pub fn from_state(st: RngState) -> Self {
        Pcg32 { st }
    }

    /// The current state, from which [`Pcg32::from_state`] resumes the same sequence.
    pub fn state(&self) -> RngState {
        self.st
    }

    fn step(&mut self) {
        self.st.state = self
            .st
            .state
            .wrapping_mul(MULTIPLIER)
            .wrapping_add(self.st.inc);
    }

    /// One draw: an integer in 0..=4294967295. The only way the stream advances.
    pub fn next_u32(&mut self) -> u32 {
        let old = self.st.state;
        self.step();
        self.st.draws += 1;
        let xorshifted = (((old >> 18) ^ old) >> 27) as u32;
        let rot = (old >> 59) as u32;
        xorshifted.rotate_right(rot)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reference_vector() {
        // pcg32-demo: pcg32_srandom_r(&rng, 42u, 54u) -> first six outputs.
        let mut g = Pcg32::with_stream(42, 54);
        let got: Vec<u32> = (0..6).map(|_| g.next_u32()).collect();
        assert_eq!(
            got,
            vec![0xa15c02b7, 0x7b47f409, 0xba1d3330, 0x83d2f293, 0xbfa4784b, 0xcbed606e]
        );
    }

    #[test]
    fn equal_seeds_equal_draws_and_state_roundtrip() {
        let mut a = Pcg32::seeded(7);
        let mut b = Pcg32::seeded(7);
        let mut c = Pcg32::seeded(8);
        let da: Vec<u32> = (0..10).map(|_| a.next_u32()).collect();
        let db: Vec<u32> = (0..10).map(|_| b.next_u32()).collect();
        let dc: Vec<u32> = (0..10).map(|_| c.next_u32()).collect();
        assert_eq!(da, db);
        assert_ne!(da, dc);
        let saved = RngState::from_json(&a.state().to_json()).unwrap();
        let mut resumed = Pcg32::from_state(saved);
        assert_eq!(resumed.next_u32(), a.next_u32());
        assert_eq!(a.state().draws, 11);
    }
}
