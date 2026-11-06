// Copyright Sidiora Labs Inc. CWR® 2026-2027
// SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0

//! `world.kvx`: identity, version, language, runtime, modules, limits, clock and RNG
//! visibility (world-source.md section 3, clause 3.4). There are no defaults: an omitted
//! limit, clock or RNG key is an error.

use std::num::NonZeroU64;

use serde::{Deserialize, Serialize};

use crate::error::ErrorKind;
use crate::kvx::Document;
use crate::read::{self, Ctx, Keys};
use crate::types::Version;

/// Required `meta.format` value of `world.kvx`.
pub const WORLD_FORMAT: &str = "cwr.world-source";

/// The only script language this grammar defines.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum Language {
    /// `"python"`.
    #[serde(rename = "python")]
    Python,
}

/// The only runtime this grammar defines.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum Runtime {
    /// `"3.12"`.
    #[serde(rename = "3.12")]
    Python312,
}

/// The `[world]` section of `world.kvx`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct World {
    /// Lowercase identifier; upgrades between Builds with different names are refused.
    pub name: String,
    /// World version; equals the newest `CHANGELOG.kvx` release.
    pub version: Version,
    /// Script language.
    pub language: Language,
    /// Script runtime the kernel must provide.
    pub runtime: Runtime,
    /// Every module that has a contract or adapter file, in declared order.
    pub modules: Vec<String>,
}

/// Per-turn resource limits. Every field is required and positive.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Limits {
    /// CPU budget per turn script, in milliseconds.
    pub cpu_ms: NonZeroU64,
    /// Wall budget per turn script, in host milliseconds.
    pub wall_ms: NonZeroU64,
    /// Resident memory budget for the sandbox process, in MiB.
    pub memory_mb: NonZeroU64,
    /// Primitive calls per turn, counted at the RPC boundary.
    pub primitive_calls: NonZeroU64,
    /// External calls per turn, counted at the gateway.
    pub adapter_calls: NonZeroU64,
}

impl Limits {
    /// The `[limits]` key names, in declared order.
    pub const NAMES: [&'static str; 5] = [
        "cpu_ms",
        "wall_ms",
        "memory_mb",
        "primitive_calls",
        "adapter_calls",
    ];
}

/// The `[clock]` section: how world time advances.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Clock {
    /// Ticks advanced per primitive call, success or failure.
    pub tick_per_primitive: NonZeroU64,
    /// Ticks advanced at the end of each turn.
    pub tick_per_turn: NonZeroU64,
}

/// The `[rng]` section.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Rng {
    /// Whether `world.seed()` is a primitive; the seed is always in the ledger header.
    pub seed_visible: bool,
}

/// Typed `world.kvx`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct WorldFile {
    /// `meta.example`: an illustrative source that `cwr build` refuses without `--allow-example`.
    pub example: bool,
    /// The `[world]` section.
    pub world: World,
    /// The `[limits]` section.
    pub limits: Limits,
    /// The `[clock]` section.
    pub clock: Clock,
    /// The `[rng]` section.
    pub rng: Rng,
}

const SECTIONS: [&str; 5] = ["meta", "world", "limits", "clock", "rng"];

fn positives<const N: usize>(
    ctx: &mut Ctx,
    doc: &Document,
    section: &str,
    names: [&'static str; N],
) -> Option<[NonZeroU64; N]> {
    let Some(sec) = doc.section(section) else {
        ctx.push(
            1,
            1,
            ErrorKind::MissingKey,
            format!(
                "missing [{section}] section; keys {} are required and have no defaults",
                names.join(", ")
            ),
        );
        return None;
    };
    let mut keys = Keys::new(sec);
    let mut out = [NonZeroU64::MIN; N];
    let mut ok = true;
    for (slot, name) in out.iter_mut().zip(names) {
        match keys.req(ctx, name).and_then(|e| read::positive(ctx, e)) {
            Some(v) => *slot = v,
            None => ok = false,
        }
    }
    keys.finish(ctx, &format!("[{section}]"));
    ok.then_some(out)
}

/// Reads `world.kvx`.
pub(crate) fn read_world(ctx: &mut Ctx, doc: &Document) -> Option<WorldFile> {
    let (example, _) = read::meta(ctx, doc, WORLD_FORMAT, &[])?;
    for section in &doc.sections {
        if !SECTIONS.contains(&section.name.as_str()) {
            ctx.push(
                section.line,
                1,
                ErrorKind::UnknownSection,
                format!(
                    "unknown section [{}]; world.kvx defines only [{}]",
                    section.name,
                    SECTIONS.join("], [")
                ),
            );
        }
    }
    let world = match doc.section("world") {
        None => {
            ctx.push(1, 1, ErrorKind::MissingKey, "missing [world] section");
            None
        }
        Some(sec) => {
            let mut keys = Keys::new(sec);
            let name = keys.req(ctx, "name").and_then(|e| {
                let s = read::string(ctx, e)?;
                if !read::is_ident(&s) {
                    ctx.bad(
                        e,
                        &e.value,
                        format!("world.name {s:?} must be a lowercase identifier [a-z][a-z0-9_]*"),
                    );
                    return None;
                }
                Some(s)
            });
            let version = keys.req(ctx, "version").and_then(|e| {
                let s = read::string(ctx, e)?;
                match s.parse::<Version>() {
                    Ok(v) => Some(v),
                    Err(m) => {
                        ctx.bad(e, &e.value, m.to_string());
                        None
                    }
                }
            });
            let language = keys
                .req(ctx, "language")
                .and_then(|e| read::exact_string(ctx, e, "python"))
                .map(|_| Language::Python);
            let runtime = keys
                .req(ctx, "runtime")
                .and_then(|e| read::exact_string(ctx, e, "3.12"))
                .map(|_| Runtime::Python312);
            let modules = keys.req(ctx, "modules").and_then(|e| {
                let list = read::string_list(ctx, e)?;
                let mut out: Vec<String> = Vec::new();
                let mut ok = true;
                for (m, node) in list {
                    if !read::is_ident(&m) {
                        ctx.bad(
                            e,
                            node,
                            format!("module {m:?} must be a lowercase identifier [a-z][a-z0-9_]*"),
                        );
                        ok = false;
                    } else if out.contains(&m) {
                        ctx.bad(e, node, format!("module {m} is listed twice"));
                        ok = false;
                    } else {
                        out.push(m);
                    }
                }
                if out.is_empty() && ok {
                    ctx.bad(e, &e.value, "world.modules must list at least one module");
                    ok = false;
                }
                ok.then_some(out)
            });
            keys.finish(ctx, "[world]");
            match (name, version, language, runtime, modules) {
                (Some(name), Some(version), Some(language), Some(runtime), Some(modules)) => {
                    Some(World {
                        name,
                        version,
                        language,
                        runtime,
                        modules,
                    })
                }
                _ => None,
            }
        }
    };
    let limits = positives(ctx, doc, "limits", Limits::NAMES).map(|v| Limits {
        cpu_ms: v[0],
        wall_ms: v[1],
        memory_mb: v[2],
        primitive_calls: v[3],
        adapter_calls: v[4],
    });
    let clock =
        positives(ctx, doc, "clock", ["tick_per_primitive", "tick_per_turn"]).map(|v| Clock {
            tick_per_primitive: v[0],
            tick_per_turn: v[1],
        });
    let rng = match doc.section("rng") {
        None => {
            ctx.push(
                1,
                1,
                ErrorKind::MissingKey,
                "missing [rng] section; seed_visible is required and has no default",
            );
            None
        }
        Some(sec) => {
            let mut keys = Keys::new(sec);
            let seed_visible = keys
                .req(ctx, "seed_visible")
                .and_then(|e| read::boolean(ctx, e));
            keys.finish(ctx, "[rng]");
            seed_visible.map(|seed_visible| Rng { seed_visible })
        }
    };
    Some(WorldFile {
        example,
        world: world?,
        limits: limits?,
        clock: clock?,
        rng: rng?,
    })
}

/// Line of `world.version` in `world.kvx`, for changelog errors located there.
pub(crate) fn version_location(doc: &Document) -> (usize, usize) {
    doc.section("world")
        .and_then(|s| s.get("version"))
        .map(|e| (e.line, e.value.col))
        .unwrap_or((1, 1))
}

pub(crate) fn emit(world: &WorldFile) -> String {
    let q = crate::kvx::quote;
    let mut out = String::new();
    out.push_str("[meta]\n");
    out.push_str(&format!(
        "format = {}\nschema_version = 1\n",
        q(WORLD_FORMAT)
    ));
    if world.example {
        out.push_str("example = true\n");
    }
    out.push_str("\n[world]\n");
    out.push_str(&format!("name = {}\n", q(&world.world.name)));
    out.push_str(&format!(
        "version = {}\n",
        q(&world.world.version.to_string())
    ));
    out.push_str("language = \"python\"\nruntime = \"3.12\"\n");
    out.push_str(&format!(
        "modules = {}\n",
        crate::kvx::quote_list(&world.world.modules)
    ));
    let l = &world.limits;
    out.push_str(&format!(
        "\n[limits]\ncpu_ms = {}\nwall_ms = {}\nmemory_mb = {}\nprimitive_calls = {}\nadapter_calls = {}\n",
        l.cpu_ms, l.wall_ms, l.memory_mb, l.primitive_calls, l.adapter_calls
    ));
    out.push_str(&format!(
        "\n[clock]\ntick_per_primitive = {}\ntick_per_turn = {}\n",
        world.clock.tick_per_primitive, world.clock.tick_per_turn
    ));
    out.push_str(&format!(
        "\n[rng]\nseed_visible = {}\n",
        world.rng.seed_visible
    ));
    out
}
