// Copyright Sidiora Labs Inc. CWR® 2026-2027
// SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0

//! Version derivation (compiler.md section 6, world-source.md sections 11 and 13, clauses 4.3,
//! 4.4 and 5.5, decision P15).
//!
//! * The *derived* bump is the maximum over ContractDiff entries (major beats minor beats patch
//!   beats none); it is computed in [`crate::diff`].
//! * The *declared* bump is what the version change between the previous Build and
//!   `world.version` literally is: major when the major number grew, minor when the minor grew,
//!   patch when only the patch grew, and none when the new version is not numerically greater.
//! * The declared bump satisfies the derived bump when it is at least as large. Pre-1.0 rule
//!   P15: while the previous version's major is 0, a minor increment satisfies a derived major
//!   and a patch increment satisfies a derived minor; from 1.0.0 on the derived bump applies
//!   literally. When the rule is what made the version acceptable, [`Verdict::pre_1_0_rule_applied`]
//!   is true and the ContractDiff output says so.
//!
//! Versions are compared numerically, never lexically ([`cwr_contract::Version`] orders by
//! `(major, minor, patch)`).

use cwr_contract::{Bump, Version};

/// The text of the pre-1.0 rule, printed in the diff output whenever it is applied.
pub const PRE_1_0_RULE: &str = "pre-1.0 rule (decision P15): while the major version is 0, a minor increment satisfies a derived major bump and a patch increment satisfies a derived minor bump; from 1.0.0 the derived bump applies literally";

/// Lowercase name of a bump.
pub fn bump_str(b: Bump) -> &'static str {
    match b {
        Bump::None => "none",
        Bump::Patch => "patch",
        Bump::Minor => "minor",
        Bump::Major => "major",
    }
}

/// Parses a lowercase bump name.
pub fn parse_bump(s: &str) -> Option<Bump> {
    match s {
        "none" => Some(Bump::None),
        "patch" => Some(Bump::Patch),
        "minor" => Some(Bump::Minor),
        "major" => Some(Bump::Major),
        _ => None,
    }
}

/// The literal bump from `old` to `new`; `Bump::None` when `new <= old`.
pub fn declared_bump(old: Version, new: Version) -> Bump {
    if new <= old {
        Bump::None
    } else if new.major > old.major {
        Bump::Major
    } else if new.minor > old.minor {
        Bump::Minor
    } else {
        Bump::Patch
    }
}

/// The outcome of checking a declared version against a derived bump.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Verdict {
    /// Version of the previous Build.
    pub previous: Version,
    /// Declared version of this source.
    pub version: Version,
    /// Minimum bump derived from the ContractDiff.
    pub derived: Bump,
    /// Bump actually declared between the two versions.
    pub declared: Bump,
    /// The new version is numerically greater than the previous one.
    pub greater: bool,
    /// The declared bump satisfies the derived bump (possibly through the pre-1.0 rule).
    pub satisfied: bool,
    /// True when the version is accepted only because of the pre-1.0 rule.
    pub pre_1_0_rule_applied: bool,
}

impl Verdict {
    /// One-line human statement of the verdict, naming the rule that was applied.
    pub fn statement(&self) -> String {
        let base = format!(
            "{} -> {}: declared bump {}, derived minimum bump {}",
            self.previous,
            self.version,
            bump_str(self.declared),
            bump_str(self.derived)
        );
        if !self.greater {
            format!("{base}; refused: world.version must be numerically greater than the previous Build's version {}", self.previous)
        } else if self.pre_1_0_rule_applied {
            format!("{base}; accepted by the {PRE_1_0_RULE}")
        } else if self.satisfied {
            format!("{base}; accepted")
        } else if self.previous.major == 0 {
            format!("{base}; refused: below the derived bump even under the {PRE_1_0_RULE}")
        } else {
            format!("{base}; refused: from 1.0.0 the derived bump applies literally")
        }
    }
}

/// Checks `version` against the previous Build's version and the derived bump.
pub fn check(previous: Version, version: Version, derived: Bump) -> Verdict {
    let declared = declared_bump(previous, version);
    let greater = version > previous;
    let literal = greater && declared >= derived;
    let pre = greater
        && !literal
        && previous.major == 0
        && matches!(
            (declared, derived),
            (Bump::Minor, Bump::Major) | (Bump::Patch, Bump::Minor)
        );
    Verdict {
        previous,
        version,
        derived,
        declared,
        greater,
        satisfied: literal || pre,
        pre_1_0_rule_applied: pre,
    }
}

/// The version half of the kernel's upgrade gate (clause 5.5): an Instance may move from a
/// Build named `from_name` at `from` to one named `to_name` at `to` only when the names are
/// equal and the version is strictly greater, unless the kernel runs with
/// `--allow-experimental`. The kernel (task 2.5) owns the refusal; this is the shared rule.
pub fn upgrade_allowed(
    from_name: &str,
    from: Version,
    to_name: &str,
    to: Version,
    allow_experimental: bool,
) -> Result<(), String> {
    if allow_experimental {
        return Ok(());
    }
    if from_name != to_name {
        return Err(format!(
            "world name {to_name:?} differs from the running world {from_name:?}"
        ));
    }
    if to <= from {
        return Err(format!(
            "version {to} is not greater than the running version {from}"
        ));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn v(s: &str) -> Version {
        s.parse().unwrap()
    }

    #[test]
    fn declared_bumps() {
        assert_eq!(declared_bump(v("0.2.0"), v("0.3.0")), Bump::Minor);
        assert_eq!(declared_bump(v("0.2.0"), v("0.2.1")), Bump::Patch);
        assert_eq!(declared_bump(v("0.2.9"), v("1.0.0")), Bump::Major);
        assert_eq!(declared_bump(v("0.2.0"), v("0.2.0")), Bump::None);
        assert_eq!(declared_bump(v("0.10.0"), v("0.9.0")), Bump::None);
        // numeric, not lexical
        assert_eq!(declared_bump(v("0.9.0"), v("0.10.0")), Bump::Minor);
    }

    #[test]
    fn pre_1_0_rule() {
        let a = check(v("0.2.0"), v("0.3.0"), Bump::Major);
        assert!(a.satisfied && a.pre_1_0_rule_applied);
        let b = check(v("0.2.0"), v("0.2.1"), Bump::Minor);
        assert!(b.satisfied && b.pre_1_0_rule_applied);
        let c = check(v("0.2.0"), v("0.2.1"), Bump::Major);
        assert!(!c.satisfied);
        let d = check(v("0.2.0"), v("1.0.0"), Bump::Major);
        assert!(d.satisfied && !d.pre_1_0_rule_applied);
        let e = check(v("0.2.0"), v("0.2.1"), Bump::Patch);
        assert!(e.satisfied && !e.pre_1_0_rule_applied);
    }

    #[test]
    fn literal_from_1_0() {
        assert!(!check(v("1.0.0"), v("1.1.0"), Bump::Major).satisfied);
        assert!(!check(v("1.2.0"), v("1.2.1"), Bump::Minor).satisfied);
        assert!(check(v("1.0.0"), v("2.0.0"), Bump::Major).satisfied);
        assert!(check(v("1.0.0"), v("1.0.1"), Bump::Patch).satisfied);
        assert!(check(v("1.0.0"), v("1.0.1"), Bump::None).satisfied);
    }

    #[test]
    fn not_greater_is_refused() {
        let r = check(v("0.2.0"), v("0.2.0"), Bump::None);
        assert!(!r.satisfied && !r.greater);
        assert!(r.statement().contains("numerically greater"));
    }

    #[test]
    fn upgrade_rule() {
        assert!(upgrade_allowed("w", v("0.2.0"), "w", v("0.3.0"), false).is_ok());
        assert!(upgrade_allowed("w", v("0.2.0"), "w", v("0.2.0"), false).is_err());
        assert!(upgrade_allowed("w", v("0.2.0"), "x", v("0.3.0"), false).is_err());
        assert!(upgrade_allowed("w", v("0.3.0"), "w", v("0.2.0"), true).is_ok());
    }
}
