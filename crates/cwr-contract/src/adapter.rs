// Copyright Sidiora Labs Inc. CWR® 2026-2027
// SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0

//! Adapter contracts, `adapters/<module>.kvx` (world-source.md section 7, clause 3.3).
//!
//! Shape rules: `side_effect` is `external`; `idempotency` is `none`, `natural` or `key`;
//! `timeout_ms` is a positive integer; `endpoint` is an absolute `stub://`, `http://` or
//! `https://` URI with a host and no whitespace or control characters (see
//! [`endpoint_problem`]); `failures` is a non-empty, duplicate-free subset of the envelope
//! that contains every code the gateway produces itself (kernel.md section 7): `UNAVAILABLE`
//! and `TIMEOUT` always, `DUPLICATE_KEY` for a `key` adapter (CWR-F002, unknown codes
//! CWR-F001); every precondition's code is one of `failures`, because the interface promises
//! a valid result or a declared failure (CWR-F002); `content_guaranteed` is present and
//! `false` (CWR-F002); `effects` is forbidden (CWR-C001): an adapter claims no effects over
//! world state. This crate is the single owner of these rules. Two optional keys
//! steer the gateway's normalisation (kernel.md section 7): `undeclared_status`, the code a
//! service status the adapter does not map falls back to (`UNAVAILABLE`, the default, or
//! `INVALID`, and it must be in `failures`), and `retry_after_ratio`, the positive integer
//! number of world ticks per unit of a service's `retry_after` (default 1).

use std::num::NonZeroU64;

use serde::{Deserialize, Serialize};

use crate::envelope::Code;
use crate::error::{ErrorKind, Errors};
use crate::kvx::{Document, Json};
use crate::source::FileClass;

use crate::primitive::{self, Precondition};
use crate::read::{self, Ctx, Keys};
use crate::types::{Params, SideEffect, TypeExpr};

/// Required `meta.format` value of an `adapters/<module>.kvx` file.
pub const ADAPTER_FORMAT: &str = "cwr.adapter";

/// Adapter idempotency class (world-source.md section 7); decides how the gateway treats
/// repeated calls.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Idempotency {
    /// `none`: every call is sent and nothing is stored; a `TIMEOUT` may have had an effect.
    None,
    /// `natural`: every call is sent; request and response hashes are recorded in the ledger.
    Natural,
    /// `key`: the first outcome per `(adapter, key)` is stored and replayed for identical payloads.
    Key,
}

impl Idempotency {
    /// The KVX spelling: `"none"`, `"natural"` or `"key"`.
    pub fn as_str(self) -> &'static str {
        match self {
            Idempotency::None => "none",
            Idempotency::Natural => "natural",
            Idempotency::Key => "key",
        }
    }

    /// Parses the exact KVX spelling; `None` for anything else.
    pub fn parse(s: &str) -> Option<Self> {
        match s {
            "none" => Some(Idempotency::None),
            "natural" => Some(Idempotency::Natural),
            "key" => Some(Idempotency::Key),
            _ => None,
        }
    }
}

/// One adapter contract.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Adapter {
    /// `<module>.<name>`.
    pub name: String,
    /// Always [`SideEffect::External`].
    pub side_effect: SideEffect,
    /// Named, typed parameters in declared order.
    pub params: Params,
    /// Return record shape; the gateway validates the shape, not the values.
    pub returns: TypeExpr,
    /// Preconditions evaluated before any network call, each with its failure code.
    pub preconditions: Vec<Precondition>,
    /// How the gateway treats repeated calls.
    pub idempotency: Idempotency,
    /// Host-time budget in milliseconds; exceeding it is `TIMEOUT`.
    pub timeout_ms: NonZeroU64,
    /// The only destination the gateway may contact for this adapter.
    pub endpoint: String,
    /// Declared failure codes, a nonempty subset of the envelope that includes `UNAVAILABLE`.
    pub failures: Vec<Code>,
    /// Always `false`.
    pub content_guaranteed: bool,
    /// Manual text for the adapter.
    pub doc: String,
    /// Fallback for a service status the adapter does not map: `UNAVAILABLE` (default) or
    /// `INVALID`; always one of `failures`.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub undeclared_status: Option<Code>,
    /// World ticks per unit of the service's `retry_after` (default 1).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub retry_after_ratio: Option<NonZeroU64>,
}

impl Adapter {
    /// The selected undeclared-status fallback (default `UNAVAILABLE`).
    pub fn undeclared_code(&self) -> Code {
        self.undeclared_status.unwrap_or(Code::Unavailable)
    }

    /// World ticks per service `retry_after` unit (default 1).
    pub fn retry_ratio(&self) -> u64 {
        self.retry_after_ratio.map(NonZeroU64::get).unwrap_or(1)
    }
}

/// Typed `adapters/<module>.kvx`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AdapterFile {
    /// Source-relative path, `adapters/<module>.kvx`.
    pub path: String,
    /// Module named by `meta.module`; declared in `world.modules`.
    pub module: String,
    /// `meta.example`: the file is illustrative.
    pub example: bool,
    /// Adapter contracts in declared order.
    pub adapters: Vec<Adapter>,
}

impl AdapterFile {
    /// Parses one `adapters/<module>.kvx` file on its own with exactly the rules
    /// [`WorldSource::load_tree`](crate::WorldSource::load_tree) applies to it, apart from the
    /// source-level ones (the module is declared in `world.modules`, names are unique across
    /// files). `path` is the source-relative path; every error is located in it.
    pub fn parse(path: &str, text: &str) -> Result<AdapterFile, Errors> {
        crate::source::parse_file(path, text, FileClass::Adapter, read_adapter_file)
    }
}

/// URI schemes an adapter endpoint may use: `stub` (resolved through the operator's stub
/// map), `http` and `https`.
pub const ENDPOINT_SCHEMES: [&str; 3] = ["stub", "http", "https"];

/// Why `endpoint` is not a destination the adapter gateway may contact, or `None` when it is
/// one: an absolute URI `<scheme>://<host>[:<port>][/<path>]` with a scheme from
/// [`ENDPOINT_SCHEMES`], a DNS name or IPv4 host outside the link-local range 169.254.0.0/16,
/// a port from 1 to 65535 when present, no user information and no whitespace or control
/// characters anywhere.
pub fn endpoint_problem(endpoint: &str) -> Option<String> {
    if let Some(c) = endpoint
        .chars()
        .find(|c| c.is_whitespace() || c.is_control())
    {
        return Some(format!(
            "endpoint must not contain whitespace or control characters, found U+{:04X}",
            u32::from(c)
        ));
    }
    let Some((scheme, rest)) = endpoint.split_once("://") else {
        return Some(format!(
            "endpoint {endpoint:?} must be an absolute URI <scheme>://<host>[/<path>]"
        ));
    };
    if !ENDPOINT_SCHEMES.contains(&scheme) {
        return Some(format!(
            "endpoint scheme {scheme:?} is not allowed; use stub, http or https"
        ));
    }
    let authority = rest.split(['/', '?', '#']).next().unwrap_or_default();
    if authority.contains('@') {
        return Some("endpoint must not carry user information".to_string());
    }
    let (host, port) = match authority.rsplit_once(':') {
        Some((host, port)) => (host, Some(port)),
        None => (authority, None),
    };
    if host.is_empty()
        || !host
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || c == '.' || c == '-')
    {
        return Some(format!(
            "endpoint host {host:?} must be a non-empty DNS name or IPv4 address"
        ));
    }
    if host.starts_with("169.254.") {
        return Some(format!(
            "endpoint host {host} is a link-local address, which an adapter may not contact"
        ));
    }
    if let Some(port) = port {
        let valid = !port.is_empty()
            && port.chars().all(|c| c.is_ascii_digit())
            && port.parse::<u16>().is_ok_and(|p| p > 0);
        if !valid {
            return Some(format!("endpoint port {port:?} must be from 1 to 65535"));
        }
    }
    None
}

/// The codes `failures` must contain for an adapter of class `idempotency`, each with the
/// reason the gateway needs it (kernel.md section 7).
fn required_failures(idempotency: Option<Idempotency>) -> Vec<(Code, &'static str)> {
    let mut out = vec![
        (
            Code::Unavailable,
            "UNAVAILABLE is the mapping target for undeclared transport outcomes",
        ),
        (
            Code::Timeout,
            "TIMEOUT is what an expired timeout_ms becomes",
        ),
    ];
    if idempotency == Some(Idempotency::Key) {
        out.push((
            Code::DuplicateKey,
            "DUPLICATE_KEY is what a key adapter's store answers to a reused key with a \
             different payload",
        ));
    }
    out
}

pub(crate) fn read_adapter_file(ctx: &mut Ctx, doc: &Document, stem: &str) -> Option<AdapterFile> {
    let (example, meta) = read::meta(ctx, doc, ADAPTER_FORMAT, &["module"])?;
    let module = primitive::check_module(ctx, meta.into_iter().next().flatten(), stem)?;
    let mut adapters = Vec::new();
    let mut ok = true;
    for section in &doc.sections {
        if section.name == "meta" {
            continue;
        }
        if !section.name.starts_with("adapter.") {
            ctx.push(section.line, 1, ErrorKind::UnknownSection, format!("unknown section [{}]; an adapter file defines [meta] and [adapter.<module>.<name>]", section.name));
            ok = false;
            continue;
        }
        let Some(name) =
            primitive::split_member(ctx, section.line, &section.name, "adapter", &module)
        else {
            ok = false;
            continue;
        };
        let mut keys = Keys::new(section);
        let side_effect = keys.req(ctx, "side_effect").and_then(|e| {
            let s = read::string(ctx, e)?;
            if s != "external" {
                let kind = if SideEffect::parse(&s).is_some() {
                    ErrorKind::Shape
                } else {
                    ErrorKind::BadValue
                };
                ctx.at(
                    e,
                    &e.value,
                    kind,
                    format!("{name}: an adapter's side_effect must be external, got {s:?}"),
                );
                return None;
            }
            Some(SideEffect::External)
        });
        let params = keys
            .req(ctx, "params")
            .and_then(|e| primitive::read_params(ctx, e));
        let returns = keys
            .req(ctx, "returns")
            .and_then(|e| primitive::read_type(ctx, e));
        let preconditions = keys
            .req(ctx, "preconditions")
            .and_then(|e| primitive::read_preconditions(ctx, e));
        let idempotency = keys.req(ctx, "idempotency").and_then(|e| {
            let s = read::string(ctx, e)?;
            let v = Idempotency::parse(&s);
            if v.is_none() {
                ctx.bad(
                    e,
                    &e.value,
                    format!("{name}: idempotency must be none, natural or key, got {s:?}"),
                );
            }
            v
        });
        let timeout_ms = keys
            .req(ctx, "timeout_ms")
            .and_then(|e| read::positive(ctx, e));
        let endpoint = keys.req(ctx, "endpoint").and_then(|e| {
            let s = read::nonempty_string(ctx, e)?;
            if let Some(problem) = endpoint_problem(&s) {
                ctx.bad(e, &e.value, format!("{name}: {problem}"));
                return None;
            }
            Some(s)
        });
        let failures = keys.req(ctx, "failures").and_then(|e| {
            let items = read::array(ctx, e)?;
            let mut codes: Vec<Code> = Vec::new();
            let mut good = true;
            for (i, node) in items.iter().enumerate() {
                match primitive::read_code(ctx, e, node, &format!("{name}: failures[{i}]")) {
                    Some(c) if codes.contains(&c) => {
                        ctx.bad(e, node, format!("{name}: failure {c} is listed twice"));
                        good = false;
                    }
                    Some(c) => codes.push(c),
                    None => good = false,
                }
            }
            if !good {
                return None;
            }
            if codes.is_empty() {
                ctx.at(
                    e,
                    &e.value,
                    ErrorKind::Adapter,
                    format!("{name}: failures must be a non-empty subset of the envelope"),
                );
                return None;
            }
            let missing: Vec<(Code, &str)> = required_failures(idempotency)
                .into_iter()
                .filter(|(c, _)| !codes.contains(c))
                .collect();
            if !missing.is_empty() {
                let list: Vec<&str> = missing.iter().map(|(c, _)| c.as_str()).collect();
                let why: Vec<&str> = missing.iter().map(|(_, w)| *w).collect();
                ctx.at(
                    e,
                    &e.value,
                    ErrorKind::Adapter,
                    format!(
                        "{name}: failures must include {}: {}",
                        list.join(", "),
                        why.join("; ")
                    ),
                );
                return None;
            }
            Some(codes)
        });
        if let (Some(pre), Some(declared), Some(e)) =
            (&preconditions, &failures, section.get("preconditions"))
        {
            if let Json::Array(items) = &e.value.value {
                for (i, (p, node)) in pre.iter().zip(items).enumerate() {
                    if declared.contains(&p.code()) {
                        continue;
                    }
                    let at = match &node.value {
                        Json::Array(pair) => pair.get(1).unwrap_or(node),
                        _ => node,
                    };
                    ctx.at(
                        e,
                        at,
                        ErrorKind::Adapter,
                        format!(
                            "{name}: precondition {} raises {}, which is not among the declared \
                             failures; an adapter returns a valid result or a declared failure",
                            i + 1,
                            p.code()
                        ),
                    );
                    ok = false;
                }
            }
        }
        let content_guaranteed = match keys.opt("content_guaranteed") {
            None => {
                ctx.push(
                    section.line,
                    1,
                    ErrorKind::Adapter,
                    format!("{name}: content_guaranteed = false must be declared"),
                );
                None
            }
            Some(e) => match e.value.value {
                Json::Bool(false) => Some(false),
                _ => {
                    ctx.at(e, &e.value, ErrorKind::Adapter, format!("{name}: content_guaranteed must be false; adapter content is never guaranteed"));
                    None
                }
            },
        };
        let doc_text = keys
            .req(ctx, "doc")
            .and_then(|e| primitive::read_doc(ctx, e));
        let mut optional_ok = true;
        let undeclared_status = match keys.opt("undeclared_status") {
            None => None,
            Some(e) => {
                match primitive::read_code(ctx, e, &e.value, &format!("{name}: undeclared_status"))
                {
                    Some(c @ (Code::Unavailable | Code::Invalid)) => {
                        if failures.as_ref().map(|f| !f.contains(&c)).unwrap_or(false) {
                            ctx.at(e, &e.value, ErrorKind::Adapter, format!("{name}: undeclared_status {c} must be one of the declared failures"));
                            optional_ok = false;
                        }
                        Some(c)
                    }
                    Some(c) => {
                        ctx.at(
                            e,
                            &e.value,
                            ErrorKind::Adapter,
                            format!(
                                "{name}: undeclared_status must be UNAVAILABLE or INVALID, got {c}"
                            ),
                        );
                        optional_ok = false;
                        None
                    }
                    None => {
                        optional_ok = false;
                        None
                    }
                }
            }
        };
        let retry_after_ratio = match keys.opt("retry_after_ratio") {
            None => None,
            Some(e) => {
                let r = read::positive(ctx, e);
                if r.is_none() {
                    optional_ok = false;
                }
                r
            }
        };
        if !optional_ok {
            ok = false;
        }
        if let Some(e) = keys.opt("effects") {
            ctx.push(
                e.line,
                e.key_col,
                ErrorKind::Shape,
                format!("{name}: an adapter may not declare effects over world state"),
            );
            ok = false;
        }
        keys.finish(ctx, "an adapter contract");
        match (
            side_effect,
            params,
            returns,
            preconditions,
            idempotency,
            timeout_ms,
            endpoint,
            failures,
            content_guaranteed,
            doc_text,
        ) {
            (
                Some(side_effect),
                Some(params),
                Some(returns),
                Some(preconditions),
                Some(idempotency),
                Some(timeout_ms),
                Some(endpoint),
                Some(failures),
                Some(content_guaranteed),
                Some(doc),
            ) => adapters.push(Adapter {
                name,
                side_effect,
                params,
                returns,
                preconditions,
                idempotency,
                timeout_ms,
                endpoint,
                failures,
                content_guaranteed,
                doc,
                undeclared_status,
                retry_after_ratio,
            }),
            _ => ok = false,
        }
    }
    ok.then(|| AdapterFile {
        path: ctx.file.clone(),
        module,
        example,
        adapters,
    })
}

pub(crate) fn emit(file: &AdapterFile) -> String {
    let q = crate::kvx::quote;
    let mut out = format!(
        "[meta]\nformat = {}\nschema_version = 1\n",
        q(ADAPTER_FORMAT)
    );
    if file.example {
        out.push_str("example = true\n");
    }
    out.push_str(&format!("module = {}\n", q(&file.module)));
    for a in &file.adapters {
        out.push_str(&format!("\n[adapter.{}]\n", a.name));
        out.push_str("side_effect = \"external\"\n");
        out.push_str(&format!("params = {}\n", primitive::emit_params(&a.params)));
        out.push_str(&format!("returns = {}\n", q(&a.returns.to_string())));
        out.push_str(&format!(
            "preconditions = {}\n",
            primitive::emit_preconditions(&a.preconditions)
        ));
        out.push_str(&format!("idempotency = {}\n", q(a.idempotency.as_str())));
        out.push_str(&format!("timeout_ms = {}\n", a.timeout_ms));
        out.push_str(&format!(
            "failures = {}\n",
            crate::kvx::quote_list(a.failures.iter().map(|c| c.as_str()))
        ));
        out.push_str(&format!("content_guaranteed = {}\n", a.content_guaranteed));
        out.push_str(&format!("endpoint = {}\n", q(&a.endpoint)));
        if let Some(code) = a.undeclared_status {
            out.push_str(&format!("undeclared_status = {}\n", q(code.as_str())));
        }
        if let Some(ratio) = a.retry_after_ratio {
            out.push_str(&format!("retry_after_ratio = {ratio}\n"));
        }
        out.push_str(&format!("doc = {}\n", q(&a.doc)));
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn endpoints_must_be_absolute_uris_the_gateway_may_contact() {
        for good in [
            "stub://search/v1/query",
            "stub://publish",
            "http://127.0.0.1:8001/v1/query",
            "https://api.example.org/v2?x=1",
        ] {
            assert_eq!(endpoint_problem(good), None, "{good}");
        }
        for bad in [
            " ",
            "",
            "file:///etc/passwd",
            "http://169.254.169.254/latest",
            "stub://a\nb",
            "stub://search/v1 query",
            "stub:///path",
            "ftp://host/x",
            "search/v1/query",
            "http://user@host/x",
            "http://host:0/x",
            "http://host:99999/x",
            "http://host:/x",
            "http://[::1]/x",
            "stub://a\u{7f}b",
        ] {
            assert!(endpoint_problem(bad).is_some(), "{bad:?}");
        }
    }

    #[test]
    fn key_adapters_need_duplicate_key_and_every_adapter_needs_timeout() {
        let codes = |i| -> Vec<Code> { required_failures(i).into_iter().map(|(c, _)| c).collect() };
        assert_eq!(
            codes(Some(Idempotency::None)),
            [Code::Unavailable, Code::Timeout]
        );
        assert_eq!(
            codes(Some(Idempotency::Key)),
            [Code::Unavailable, Code::Timeout, Code::DuplicateKey]
        );
    }
}
