// Copyright Sidiora Labs Inc. CWR® 2026-2027
// SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0

//! Shared value types: semantic versions, the closed parameter type language, ordered
//! parameter lists and side-effect classes.

use std::cmp::Ordering;
use std::fmt;
use std::str::FromStr;

use serde::de::{Error as _, MapAccess, Visitor};
use serde::ser::SerializeMap;
use serde::{Deserialize, Deserializer, Serialize, Serializer};

/// A semantic version `MAJOR.MINOR.PATCH`, compared numerically, never lexically.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub struct Version {
    /// Major component.
    pub major: u64,
    /// Minor component.
    pub minor: u64,
    /// Patch component.
    pub patch: u64,
}

impl Ord for Version {
    fn cmp(&self, other: &Self) -> Ordering {
        (self.major, self.minor, self.patch).cmp(&(other.major, other.minor, other.patch))
    }
}

impl PartialOrd for Version {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}

/// A string that is not a semantic version `MAJOR.MINOR.PATCH`; carries the refused text.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct VersionError(pub String);

impl fmt::Display for VersionError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            f,
            "{:?} is not a semantic version MAJOR.MINOR.PATCH",
            self.0
        )
    }
}

impl std::error::Error for VersionError {}

impl FromStr for Version {
    type Err = VersionError;

    fn from_str(s: &str) -> Result<Self, Self::Err> {
        let parts: Vec<&str> = s.split('.').collect();
        let bad = || VersionError(s.to_string());
        if parts.len() != 3 {
            return Err(bad());
        }
        let mut numbers = [0u64; 3];
        for (slot, part) in numbers.iter_mut().zip(&parts) {
            if part.is_empty()
                || !part.chars().all(|c| c.is_ascii_digit())
                || (part.len() > 1 && part.starts_with('0'))
            {
                return Err(bad());
            }
            *slot = part.parse().map_err(|_| bad())?;
        }
        Ok(Version {
            major: numbers[0],
            minor: numbers[1],
            patch: numbers[2],
        })
    }
}

impl fmt::Display for Version {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}.{}.{}", self.major, self.minor, self.patch)
    }
}

impl Serialize for Version {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        serializer.collect_str(self)
    }
}

impl<'de> Deserialize<'de> for Version {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        String::deserialize(deserializer)?
            .parse()
            .map_err(D::Error::custom)
    }
}

/// The closed parameter and return type language of world-source.md section 4:
/// `str`, `int`, `bool`, `bytes`, `Path`, `None`, `list[T]`, `dict[str, T]` and record
/// names (`CamelCase`) declared in `manual/types.md`. Whether a record name is declared is
/// a resolve-stage question; [`TypeExpr::records`] lists them.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub enum TypeExpr {
    /// `str`.
    Str,
    /// `int`.
    Int,
    /// `bool`.
    Bool,
    /// `bytes`.
    Bytes,
    /// `Path`.
    Path,
    /// `None`.
    None,
    /// `list[T]`.
    List(Box<TypeExpr>),
    /// `dict[str, T]`; keys are always strings.
    Dict(Box<TypeExpr>),
    /// A `CamelCase` record name; whether `manual/types.md` declares it is a resolve-stage check.
    Record(String),
}

impl TypeExpr {
    /// Every record name the type mentions; empty for a type built only from builtins.
    pub fn records(&self) -> Vec<&str> {
        match self {
            TypeExpr::List(t) | TypeExpr::Dict(t) => t.records(),
            TypeExpr::Record(r) => vec![r.as_str()],
            _ => Vec::new(),
        }
    }
}

/// Deepest nesting of `list[` and `dict[str, ` a type may use; the same bound as the KVX
/// reader's JSON nesting limit, so no type text can exhaust the stack.
pub const MAX_TYPE_DEPTH: usize = 64;

/// Why a string is not a type of the closed type language.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum TypeError {
    /// The text (the innermost refused part of a nested type) is not a builtin, a
    /// `list[T]`, a `dict[str, T]` or a `CamelCase` record name.
    NotAType(String),
    /// The type nests `list[` and `dict[str, ` deeper than [`MAX_TYPE_DEPTH`] levels.
    TooDeep(String),
}

impl fmt::Display for TypeError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            TypeError::NotAType(s) => write!(
                f,
                "{s:?} is not a type: use str, int, bool, bytes, Path, None, list[T], dict[str, T] or a CamelCase record name"
            ),
            TypeError::TooDeep(s) => write!(
                f,
                "type {s:?} nests list[ and dict[ deeper than {MAX_TYPE_DEPTH} levels"
            ),
        }
    }
}

impl std::error::Error for TypeError {}

fn parse_type(whole: &str, s: &str, depth: usize) -> Result<TypeExpr, TypeError> {
    Ok(match s {
        "str" => TypeExpr::Str,
        "int" => TypeExpr::Int,
        "bool" => TypeExpr::Bool,
        "bytes" => TypeExpr::Bytes,
        "Path" => TypeExpr::Path,
        "None" => TypeExpr::None,
        _ => {
            let inner = s
                .strip_prefix("list[")
                .and_then(|r| r.strip_suffix(']'))
                .map(|r| (r, true))
                .or_else(|| {
                    s.strip_prefix("dict[str, ")
                        .and_then(|r| r.strip_suffix(']'))
                        .map(|r| (r, false))
                });
            if let Some((inner, list)) = inner {
                if depth >= MAX_TYPE_DEPTH {
                    let shown: String = whole.chars().take(80).collect();
                    return Err(TypeError::TooDeep(shown));
                }
                let t = Box::new(parse_type(whole, inner, depth + 1)?);
                if list {
                    TypeExpr::List(t)
                } else {
                    TypeExpr::Dict(t)
                }
            } else {
                let mut chars = s.chars();
                let record = matches!(chars.next(), Some(c) if c.is_ascii_uppercase())
                    && chars.all(|c| c.is_ascii_alphanumeric());
                if !record {
                    return Err(TypeError::NotAType(s.to_string()));
                }
                TypeExpr::Record(s.to_string())
            }
        }
    })
}

impl FromStr for TypeExpr {
    type Err = TypeError;

    fn from_str(s: &str) -> Result<Self, Self::Err> {
        parse_type(s, s, 0)
    }
}

impl fmt::Display for TypeExpr {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            TypeExpr::Str => f.write_str("str"),
            TypeExpr::Int => f.write_str("int"),
            TypeExpr::Bool => f.write_str("bool"),
            TypeExpr::Bytes => f.write_str("bytes"),
            TypeExpr::Path => f.write_str("Path"),
            TypeExpr::None => f.write_str("None"),
            TypeExpr::List(t) => write!(f, "list[{t}]"),
            TypeExpr::Dict(t) => write!(f, "dict[str, {t}]"),
            TypeExpr::Record(r) => f.write_str(r),
        }
    }
}

impl Serialize for TypeExpr {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        serializer.collect_str(self)
    }
}

impl<'de> Deserialize<'de> for TypeExpr {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        String::deserialize(deserializer)?
            .parse()
            .map_err(D::Error::custom)
    }
}

/// Ordered parameter list; serialised as a JSON object in declared order. The SDK stub uses
/// the same order.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct Params(pub Vec<(String, TypeExpr)>);

impl Params {
    /// Parameter names in declared order.
    pub fn names(&self) -> impl Iterator<Item = &str> {
        self.0.iter().map(|(n, _)| n.as_str())
    }

    /// The type of parameter `name`, if declared.
    pub fn get(&self, name: &str) -> Option<&TypeExpr> {
        self.0.iter().find(|(n, _)| n == name).map(|(_, t)| t)
    }
}

impl Serialize for Params {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        let mut map = serializer.serialize_map(Some(self.0.len()))?;
        for (name, ty) in &self.0 {
            map.serialize_entry(name, ty)?;
        }
        map.end()
    }
}

impl<'de> Deserialize<'de> for Params {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        struct V;
        impl<'de> Visitor<'de> for V {
            type Value = Params;
            fn expecting(&self, f: &mut fmt::Formatter) -> fmt::Result {
                f.write_str("an object of parameter name to type")
            }
            fn visit_map<A: MapAccess<'de>>(self, mut map: A) -> Result<Params, A::Error> {
                let mut out: Vec<(String, TypeExpr)> = Vec::new();
                while let Some((name, ty)) = map.next_entry::<String, TypeExpr>()? {
                    if !crate::read::is_member(&name) {
                        return Err(A::Error::custom(format!(
                            "parameter name {name:?} must match [a-z_][a-z0-9_]*"
                        )));
                    }
                    if out.iter().any(|(n, _)| *n == name) {
                        return Err(A::Error::custom(format!("duplicate parameter {name}")));
                    }
                    out.push((name, ty));
                }
                Ok(Params(out))
            }
        }
        deserializer.deserialize_map(V)
    }
}

/// Side-effect class of a primitive (clause 3.2). `external` is reserved for adapters.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum SideEffect {
    /// `read_only`: the primitive does not change world state.
    ReadOnly,
    /// `mutating`: the primitive changes world state and declares its effects.
    Mutating,
    /// `external`: an adapter call leaving the world.
    External,
}

impl SideEffect {
    /// The KVX spelling: `"read_only"`, `"mutating"` or `"external"`.
    pub fn as_str(self) -> &'static str {
        match self {
            SideEffect::ReadOnly => "read_only",
            SideEffect::Mutating => "mutating",
            SideEffect::External => "external",
        }
    }

    /// Parses the exact KVX spelling; `None` for anything else.
    pub fn parse(s: &str) -> Option<SideEffect> {
        match s {
            "read_only" => Some(SideEffect::ReadOnly),
            "mutating" => Some(SideEffect::Mutating),
            "external" => Some(SideEffect::External),
            _ => None,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn versions_compare_numerically() {
        let a: Version = "0.10.0".parse().unwrap();
        let b: Version = "0.9.1".parse().unwrap();
        assert!(a > b);
        for bad in [
            "0.2", "0.2.0.1", "01.2.0", "a.b.c", "", "1.2.-3", "1.2.3-rc",
        ] {
            assert!(bad.parse::<Version>().is_err(), "{bad}");
        }
    }

    #[test]
    fn types_are_closed() {
        for good in [
            "str",
            "int",
            "bool",
            "bytes",
            "Path",
            "None",
            "list[SearchHit]",
            "dict[str, list[int]]",
            "PostReceipt",
        ] {
            let t: TypeExpr = good.parse().unwrap();
            assert_eq!(t.to_string(), good);
        }
        for bad in [
            "float",
            "list[float]",
            "dict[int, str]",
            "dict[str,int]",
            "tuple[int]",
            "searchHit",
            "list[]",
            "Path ",
        ] {
            assert!(bad.parse::<TypeExpr>().is_err(), "{bad}");
        }
    }

    #[test]
    fn deeply_nested_types_are_an_error_not_a_crash() {
        let n = 10_000;
        for open in ["list[", "dict[str, "] {
            let text = format!("{}int{}", open.repeat(n), "]".repeat(n));
            assert!(matches!(
                text.parse::<TypeExpr>(),
                Err(TypeError::TooDeep(_))
            ));
            let json = serde_json::to_string(&text).unwrap();
            assert!(serde_json::from_str::<TypeExpr>(&json).is_err());
        }
        let ok = format!(
            "{}int{}",
            "list[".repeat(MAX_TYPE_DEPTH),
            "]".repeat(MAX_TYPE_DEPTH)
        );
        assert_eq!(ok.parse::<TypeExpr>().unwrap().to_string(), ok);
        let deep = format!(
            "{}int{}",
            "list[".repeat(MAX_TYPE_DEPTH + 1),
            "]".repeat(MAX_TYPE_DEPTH + 1)
        );
        assert!(deep.parse::<TypeExpr>().is_err());
    }
}
