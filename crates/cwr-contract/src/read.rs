// Copyright Sidiora Labs Inc. CWR® 2026-2027
// SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0

//! Typed extraction from KVX documents with error accumulation.
//!
//! Every accessor records a located error and returns `None` instead of failing fast, so
//! one load reports every defect of the parse stage.

use std::num::NonZeroU64;

use crate::error::{Error, ErrorKind};
use crate::kvx::{Entry, Json, Node, Section};

/// Error sink for one file.
pub(crate) struct Ctx<'e> {
    pub file: String,
    pub errors: &'e mut Vec<Error>,
}

impl<'e> Ctx<'e> {
    pub fn new(file: &str, errors: &'e mut Vec<Error>) -> Self {
        Ctx {
            file: file.to_string(),
            errors,
        }
    }

    pub fn push(&mut self, line: usize, col: usize, kind: ErrorKind, message: impl Into<String>) {
        self.errors
            .push(Error::new(self.file.clone(), line, col, kind, message));
    }

    pub fn at(&mut self, entry: &Entry, node: &Node, kind: ErrorKind, message: impl Into<String>) {
        self.push(entry.line, node.col, kind, message);
    }

    pub fn bad(&mut self, entry: &Entry, node: &Node, message: impl Into<String>) {
        self.at(entry, node, ErrorKind::BadValue, message);
    }
}

/// Key accounting for one section: every key must be consumed or it is reported as unknown.
pub(crate) struct Keys<'a> {
    pub section: &'a Section,
    used: Vec<&'a str>,
}

impl<'a> Keys<'a> {
    pub fn new(section: &'a Section) -> Self {
        Keys {
            section,
            used: Vec::new(),
        }
    }

    /// A key that must be present.
    pub fn req(&mut self, ctx: &mut Ctx, key: &'a str) -> Option<&'a Entry> {
        self.used.push(key);
        let entry = self.section.get(key);
        if entry.is_none() {
            ctx.push(
                self.section.line,
                1,
                ErrorKind::MissingKey,
                format!(
                    "[{}] is missing required key {key}; there are no defaults",
                    self.section.name
                ),
            );
        }
        entry
    }

    /// A key that may be absent.
    pub fn opt(&mut self, key: &'a str) -> Option<&'a Entry> {
        self.used.push(key);
        self.section.get(key)
    }

    /// Reports every key that was not consumed.
    pub fn finish(self, ctx: &mut Ctx, what: &str) {
        for entry in &self.section.entries {
            if !self.used.contains(&entry.key.as_str()) {
                ctx.push(
                    entry.line,
                    entry.key_col,
                    ErrorKind::UnknownKey,
                    format!(
                        "unknown key {} in [{}]; {what} defines only: {}",
                        entry.key,
                        self.section.name,
                        self.used.join(", ")
                    ),
                );
            }
        }
    }
}

pub(crate) fn string(ctx: &mut Ctx, entry: &Entry) -> Option<String> {
    match &entry.value.value {
        Json::Str(s) => Some(s.clone()),
        other => {
            ctx.bad(
                entry,
                &entry.value,
                format!("{} must be a string, got {}", entry.key, other.type_name()),
            );
            None
        }
    }
}

pub(crate) fn nonempty_string(ctx: &mut Ctx, entry: &Entry) -> Option<String> {
    let s = string(ctx, entry)?;
    if s.is_empty() {
        ctx.bad(
            entry,
            &entry.value,
            format!("{} must be a non-empty string", entry.key),
        );
        return None;
    }
    Some(s)
}

pub(crate) fn exact_string(ctx: &mut Ctx, entry: &Entry, expected: &str) -> Option<()> {
    let s = string(ctx, entry)?;
    if s != expected {
        ctx.bad(
            entry,
            &entry.value,
            format!("{} must be {expected:?}, got {s:?}", entry.key),
        );
        return None;
    }
    Some(())
}

pub(crate) fn boolean(ctx: &mut Ctx, entry: &Entry) -> Option<bool> {
    match entry.value.value {
        Json::Bool(b) => Some(b),
        ref other => {
            ctx.bad(
                entry,
                &entry.value,
                format!("{} must be a boolean, got {}", entry.key, other.type_name()),
            );
            None
        }
    }
}

pub(crate) fn int_node(ctx: &mut Ctx, entry: &Entry, node: &Node) -> Option<i64> {
    match node.value {
        Json::Int(i) => Some(i),
        ref other => {
            ctx.bad(
                entry,
                node,
                format!(
                    "{} must be an integer, got {}",
                    entry.key,
                    other.type_name()
                ),
            );
            None
        }
    }
}

pub(crate) fn positive(ctx: &mut Ctx, entry: &Entry) -> Option<NonZeroU64> {
    let i = int_node(ctx, entry, &entry.value)?;
    match u64::try_from(i).ok().and_then(NonZeroU64::new) {
        Some(n) => Some(n),
        None => {
            ctx.bad(
                entry,
                &entry.value,
                format!("{} must be a positive integer, got {i}", entry.key),
            );
            None
        }
    }
}

pub(crate) fn non_negative(ctx: &mut Ctx, entry: &Entry) -> Option<u64> {
    let i = int_node(ctx, entry, &entry.value)?;
    match u64::try_from(i) {
        Ok(n) => Some(n),
        Err(_) => {
            ctx.bad(
                entry,
                &entry.value,
                format!("{} must be a non-negative integer, got {i}", entry.key),
            );
            None
        }
    }
}

pub(crate) fn array<'a>(ctx: &mut Ctx, entry: &'a Entry) -> Option<&'a [Node]> {
    match &entry.value.value {
        Json::Array(items) => Some(items),
        other => {
            ctx.bad(
                entry,
                &entry.value,
                format!("{} must be a list, got {}", entry.key, other.type_name()),
            );
            None
        }
    }
}

/// A list of strings with each element's node, for element-located errors.
pub(crate) fn string_list<'a>(ctx: &mut Ctx, entry: &'a Entry) -> Option<Vec<(String, &'a Node)>> {
    let items = array(ctx, entry)?;
    let mut out = Vec::new();
    let mut ok = true;
    for node in items {
        match &node.value {
            Json::Str(s) => out.push((s.clone(), node)),
            other => {
                ctx.bad(
                    entry,
                    node,
                    format!(
                        "{} elements must be strings, got {}",
                        entry.key,
                        other.type_name()
                    ),
                );
                ok = false;
            }
        }
    }
    ok.then_some(out)
}

pub(crate) fn object<'a>(ctx: &mut Ctx, entry: &'a Entry) -> Option<&'a [(String, Node)]> {
    match &entry.value.value {
        Json::Object(members) => Some(members),
        other => {
            ctx.bad(
                entry,
                &entry.value,
                format!("{} must be an object, got {}", entry.key, other.type_name()),
            );
            None
        }
    }
}

/// Additional `[meta]` string values requested by [`meta`], in request order; `None` where the
/// key was missing or invalid (the error is already recorded).
pub(crate) type MetaValues<'a> = Vec<Option<(String, &'a Entry)>>;

/// Reads the `[meta]` section common to every World Source KVX file: `format` must equal
/// `format`, `schema_version` must be 1, `example` is an optional boolean. `extra` names the
/// additional required string keys, returned in order.
pub(crate) fn meta<'a>(
    ctx: &mut Ctx,
    doc: &'a crate::kvx::Document,
    format: &str,
    extra: &[&'a str],
) -> Option<(bool, MetaValues<'a>)> {
    let Some(section) = doc.section("meta") else {
        ctx.push(
            1,
            1,
            ErrorKind::MissingKey,
            format!("missing [meta] section with format = {format:?}"),
        );
        return None;
    };
    let mut keys = Keys::new(section);
    let mut ok = true;
    if let Some(e) = keys.req(ctx, "format") {
        ok &= exact_string(ctx, e, format).is_some();
    } else {
        ok = false;
    }
    if let Some(e) = keys.req(ctx, "schema_version") {
        if e.value.value != Json::Int(1) {
            ctx.bad(e, &e.value, "schema_version must be 1 for this grammar");
            ok = false;
        }
    } else {
        ok = false;
    }
    let example = match keys.opt("example") {
        Some(e) => boolean(ctx, e).unwrap_or(false),
        None => false,
    };
    let mut values = Vec::new();
    for key in extra {
        let value = keys
            .req(ctx, key)
            .and_then(|e| nonempty_string(ctx, e).map(|s| (s, e)));
        values.push(value);
    }
    keys.finish(ctx, "[meta]");
    if !ok {
        // Wrong file format: later sections would only produce noise.
        return None;
    }
    Some((example, values))
}

/// Lowercase identifier `[a-z][a-z0-9_]*`.
pub fn is_ident(s: &str) -> bool {
    let mut chars = s.chars();
    matches!(chars.next(), Some(c) if c.is_ascii_lowercase())
        && chars.all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '_')
}

/// Parameter or primitive member name `[a-z_][a-z0-9_]*`.
pub fn is_member(s: &str) -> bool {
    let mut chars = s.chars();
    matches!(chars.next(), Some(c) if c.is_ascii_lowercase() || c == '_')
        && chars.all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '_')
}
