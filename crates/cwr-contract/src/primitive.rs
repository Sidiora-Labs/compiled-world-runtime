// Copyright Sidiora Labs Inc. CWR® 2026-2027
// SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0

//! Primitive contracts, `contracts/<module>.kvx` (world-source.md section 4, clause 3.2).
//!
//! Shape rules: `side_effect` is `read_only` or `mutating` (`external` is reserved for
//! adapters); a mutating primitive declares at least one effect and a read_only primitive
//! declares none; every precondition is an `[expression, code]` pair whose expression is
//! in the precondition language and whose code is in the envelope and is not
//! `INVARIANT_VIOLATION` (the kernel raises that code only after the preconditions passed);
//! `doc` is non-empty.

use serde::{Deserialize, Serialize};

use crate::envelope::Code;
use crate::error::{ErrorKind, Errors};
use crate::kvx::{Document, Entry, Json};
use crate::precondition::Expression;
use crate::read::{self, Ctx, Keys};
use crate::source::FileClass;
use crate::types::{Params, SideEffect, TypeExpr};

/// Required `meta.format` value of a `contracts/<module>.kvx` file.
pub const CONTRACT_FORMAT: &str = "cwr.contract";

/// One `[expression, failure code]` pair. Serialised as a two-element JSON array.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Precondition(pub Expression, pub Code);

impl Precondition {
    /// The precondition expression.
    pub fn expr(&self) -> &Expression {
        &self.0
    }

    /// The envelope code raised when the expression is false.
    pub fn code(&self) -> Code {
        self.1
    }
}

/// One primitive contract.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Primitive {
    /// `<module>.<name>`.
    pub name: String,
    /// `read_only` or `mutating`; never `external`.
    pub side_effect: SideEffect,
    /// Named, typed parameters in declared order.
    pub params: Params,
    /// Return type.
    pub returns: TypeExpr,
    /// Preconditions checked in declared order, each with its failure code.
    pub preconditions: Vec<Precondition>,
    /// Postconditions asserted after an ok call; nonempty exactly when mutating.
    pub effects: Vec<Expression>,
    /// Manual text for the primitive; nonempty.
    pub doc: String,
}

/// Typed `contracts/<module>.kvx`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ContractFile {
    /// Source-relative path.
    pub path: String,
    /// Module named by `meta.module`; declared in `world.modules`.
    pub module: String,
    /// `meta.example`: the file is illustrative.
    pub example: bool,
    /// In declared order; each primitive's section line is not part of the model.
    pub primitives: Vec<Primitive>,
}

impl ContractFile {
    /// Parses one `contracts/<module>.kvx` file on its own with exactly the rules
    /// [`WorldSource::load_tree`](crate::WorldSource::load_tree) applies to it, apart from the
    /// source-level ones (the module is declared in `world.modules`, names are unique across
    /// files). `path` is the source-relative path; every error is located in it.
    pub fn parse(path: &str, text: &str) -> Result<ContractFile, Errors> {
        crate::source::parse_file(path, text, FileClass::Contract, read_contract)
    }
}

pub(crate) fn read_params(ctx: &mut Ctx, entry: &Entry) -> Option<Params> {
    let members = read::object(ctx, entry)?;
    let mut out = Vec::new();
    let mut ok = true;
    for (name, node) in members {
        if !read::is_member(name) {
            ctx.bad(
                entry,
                node,
                format!("parameter name {name:?} must match [a-z_][a-z0-9_]*"),
            );
            ok = false;
            continue;
        }
        match &node.value {
            Json::Str(s) => match s.parse::<TypeExpr>() {
                Ok(t) => out.push((name.clone(), t)),
                Err(m) => {
                    ctx.bad(entry, node, format!("parameter {name}: {m}"));
                    ok = false;
                }
            },
            other => {
                ctx.bad(
                    entry,
                    node,
                    format!(
                        "parameter {name} type must be a string, got {}",
                        other.type_name()
                    ),
                );
                ok = false;
            }
        }
    }
    ok.then_some(Params(out))
}

pub(crate) fn read_type(ctx: &mut Ctx, entry: &Entry) -> Option<TypeExpr> {
    let s = read::string(ctx, entry)?;
    match s.parse() {
        Ok(t) => Some(t),
        Err(m) => {
            ctx.bad(entry, &entry.value, m.to_string());
            None
        }
    }
}

pub(crate) fn read_code(
    ctx: &mut Ctx,
    entry: &Entry,
    node: &crate::kvx::Node,
    what: &str,
) -> Option<Code> {
    match &node.value {
        Json::Str(s) => match s.parse::<Code>() {
            Ok(c) => Some(c),
            Err(e) => {
                ctx.at(
                    entry,
                    node,
                    ErrorKind::Envelope,
                    format!("{what}: {e}; the envelope is closed"),
                );
                None
            }
        },
        other => {
            ctx.bad(
                entry,
                node,
                format!("{what} must be a code string, got {}", other.type_name()),
            );
            None
        }
    }
}

pub(crate) fn read_expression(
    ctx: &mut Ctx,
    entry: &Entry,
    node: &crate::kvx::Node,
    what: &str,
) -> Option<Expression> {
    match &node.value {
        Json::Str(s) => match s.parse::<Expression>() {
            Ok(e) => Some(e),
            Err(e) => {
                ctx.at(
                    entry,
                    node,
                    ErrorKind::Expression,
                    format!("{what} {s:?} is outside the precondition language: {e}"),
                );
                None
            }
        },
        other => {
            ctx.bad(
                entry,
                node,
                format!(
                    "{what} must be an expression string, got {}",
                    other.type_name()
                ),
            );
            None
        }
    }
}

pub(crate) fn read_preconditions(ctx: &mut Ctx, entry: &Entry) -> Option<Vec<Precondition>> {
    let items = read::array(ctx, entry)?;
    let mut out = Vec::new();
    let mut ok = true;
    for (index, node) in items.iter().enumerate() {
        let n = index + 1;
        let Json::Array(pair) = &node.value else {
            ctx.bad(
                entry,
                node,
                format!("precondition {n} must be an [expression, failure code] pair"),
            );
            ok = false;
            continue;
        };
        if pair.len() != 2 {
            ctx.bad(entry, node, format!("precondition {n} must be an [expression, failure code] pair; each precondition declares exactly one failure code"));
            ok = false;
            continue;
        }
        let expr = read_expression(
            ctx,
            entry,
            &pair[0],
            &format!("precondition {n} expression"),
        );
        let code = read_code(ctx, entry, &pair[1], &format!("precondition {n} failure"));
        let code = match code {
            Some(Code::InvariantViolation) => {
                ctx.at(
                    entry,
                    &pair[1],
                    ErrorKind::Shape,
                    format!(
                        "precondition {n} failure: INVARIANT_VIOLATION is raised by the kernel \
                         after the preconditions passed; a precondition cannot declare it"
                    ),
                );
                None
            }
            other => other,
        };
        match (expr, code) {
            (Some(e), Some(c)) => out.push(Precondition(e, c)),
            _ => ok = false,
        }
    }
    ok.then_some(out)
}

pub(crate) fn read_effects(ctx: &mut Ctx, entry: &Entry) -> Option<Vec<Expression>> {
    let items = read::array(ctx, entry)?;
    let mut out = Vec::new();
    let mut ok = true;
    for (index, node) in items.iter().enumerate() {
        match read_expression(ctx, entry, node, &format!("effect {}", index + 1)) {
            Some(e) => out.push(e),
            None => ok = false,
        }
    }
    ok.then_some(out)
}

pub(crate) fn read_doc(ctx: &mut Ctx, entry: &Entry) -> Option<String> {
    let s = read::string(ctx, entry)?;
    if s.trim().is_empty() {
        ctx.at(
            entry,
            &entry.value,
            ErrorKind::Doc,
            "doc must be a non-empty sentence rendered in the manual",
        );
        return None;
    }
    Some(s)
}

/// Splits `<prefix>.<module>.<name>` and checks the module and the member name.
pub(crate) fn split_member(
    ctx: &mut Ctx,
    line: usize,
    section: &str,
    prefix: &str,
    module: &str,
) -> Option<String> {
    let rest = &section[prefix.len() + 1..];
    let parts: Vec<&str> = rest.split('.').collect();
    if parts.len() != 2 || !read::is_ident(parts[0]) || !read::is_member(parts[1]) {
        ctx.push(
            line,
            1,
            ErrorKind::UnknownSection,
            format!(
                "section [{section}] must be [{prefix}.<module>.<name>] with lowercase identifiers"
            ),
        );
        return None;
    }
    if parts[0] != module {
        ctx.push(
            line,
            1,
            ErrorKind::Shape,
            format!("[{section}] must belong to module {module} declared in meta.module"),
        );
        return None;
    }
    Some(rest.to_string())
}

/// Checks `meta.module` against the file stem; returns the module.
pub(crate) fn check_module(
    ctx: &mut Ctx,
    module: Option<(String, &Entry)>,
    stem: &str,
) -> Option<String> {
    let (module, entry) = module?;
    if !read::is_ident(&module) {
        ctx.bad(
            entry,
            &entry.value,
            format!("meta.module {module:?} must be a lowercase identifier"),
        );
        return None;
    }
    if module != stem {
        ctx.at(
            entry,
            &entry.value,
            ErrorKind::Shape,
            format!("meta.module {module:?} must equal the file name {stem:?}"),
        );
        return None;
    }
    Some(module)
}

const PRIMITIVE_KEYS: &str = "a primitive contract";

/// Reads `contracts/<stem>.kvx`.
pub(crate) fn read_contract(ctx: &mut Ctx, doc: &Document, stem: &str) -> Option<ContractFile> {
    let (example, meta) = read::meta(ctx, doc, CONTRACT_FORMAT, &["module"])?;
    let module = check_module(ctx, meta.into_iter().next().flatten(), stem)?;
    let mut primitives = Vec::new();
    let mut ok = true;
    for section in &doc.sections {
        if section.name == "meta" {
            continue;
        }
        if !section.name.starts_with("primitive.") {
            ctx.push(section.line, 1, ErrorKind::UnknownSection, format!("unknown section [{}]; a contract file defines [meta] and [primitive.<module>.<name>]", section.name));
            ok = false;
            continue;
        }
        let Some(name) = split_member(ctx, section.line, &section.name, "primitive", &module)
        else {
            ok = false;
            continue;
        };
        let mut keys = Keys::new(section);
        let side_effect = keys.req(ctx, "side_effect").and_then(|e| {
            let s = read::string(ctx, e)?;
            match SideEffect::parse(&s) {
                Some(SideEffect::External) => {
                    ctx.at(e, &e.value, ErrorKind::Shape, format!("{name}: side_effect external is reserved for adapter contracts under adapters/"));
                    None
                }
                Some(se) => Some((se, e)),
                None => {
                    ctx.bad(e, &e.value, format!("{name}: side_effect must be read_only or mutating, got {s:?}"));
                    None
                }
            }
        });
        let params = keys.req(ctx, "params").and_then(|e| read_params(ctx, e));
        let returns = keys.req(ctx, "returns").and_then(|e| read_type(ctx, e));
        let preconditions = keys
            .req(ctx, "preconditions")
            .and_then(|e| read_preconditions(ctx, e));
        let effects_entry = keys.req(ctx, "effects");
        let effects = effects_entry.and_then(|e| read_effects(ctx, e));
        let doc_text = keys.req(ctx, "doc").and_then(|e| read_doc(ctx, e));
        keys.finish(ctx, PRIMITIVE_KEYS);
        if let (Some((se, _)), Some(effects), Some(entry)) = (&side_effect, &effects, effects_entry)
        {
            match se {
                SideEffect::ReadOnly if !effects.is_empty() => {
                    ctx.at(
                        entry,
                        &entry.value,
                        ErrorKind::Shape,
                        format!("{name}: a read_only primitive must declare no effects"),
                    );
                    ok = false;
                    continue;
                }
                SideEffect::Mutating if effects.is_empty() => {
                    ctx.at(
                        entry,
                        &entry.value,
                        ErrorKind::Shape,
                        format!("{name}: a mutating primitive must declare at least one effect"),
                    );
                    ok = false;
                    continue;
                }
                _ => {}
            }
        }
        match (
            side_effect,
            params,
            returns,
            preconditions,
            effects,
            doc_text,
        ) {
            (
                Some((side_effect, _)),
                Some(params),
                Some(returns),
                Some(preconditions),
                Some(effects),
                Some(doc),
            ) => primitives.push(Primitive {
                name,
                side_effect,
                params,
                returns,
                preconditions,
                effects,
                doc,
            }),
            _ => ok = false,
        }
    }
    ok.then(|| ContractFile {
        path: ctx.file.clone(),
        module,
        example,
        primitives,
    })
}

pub(crate) fn emit_preconditions(pre: &[Precondition]) -> String {
    let pairs: Vec<String> = pre
        .iter()
        .map(|p| crate::kvx::quote_list([p.0.source(), p.1.as_str()]))
        .collect();
    format!("[{}]", pairs.join(","))
}

pub(crate) fn emit_params(params: &Params) -> String {
    let body: Vec<String> = params
        .0
        .iter()
        .map(|(n, t)| {
            format!(
                "{}: {}",
                crate::kvx::quote(n),
                crate::kvx::quote(&t.to_string())
            )
        })
        .collect();
    format!("{{{}}}", body.join(", "))
}

pub(crate) fn emit(file: &ContractFile) -> String {
    let q = crate::kvx::quote;
    let mut out = format!(
        "[meta]\nformat = {}\nschema_version = 1\n",
        q(CONTRACT_FORMAT)
    );
    if file.example {
        out.push_str("example = true\n");
    }
    out.push_str(&format!("module = {}\n", q(&file.module)));
    for p in &file.primitives {
        out.push_str(&format!("\n[primitive.{}]\n", p.name));
        out.push_str(&format!("side_effect = {}\n", q(p.side_effect.as_str())));
        out.push_str(&format!("params = {}\n", emit_params(&p.params)));
        out.push_str(&format!("returns = {}\n", q(&p.returns.to_string())));
        out.push_str(&format!(
            "preconditions = {}\n",
            emit_preconditions(&p.preconditions)
        ));
        let effects: Vec<&str> = p.effects.iter().map(|e| e.source()).collect();
        out.push_str(&format!("effects = {}\n", crate::kvx::quote_list(&effects)));
        out.push_str(&format!("doc = {}\n", q(&p.doc)));
    }
    out
}
