// Copyright Sidiora Labs Inc. CWR® 2026-2027
// SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0

//! Located errors for the World Source reader.
//!
//! Every error carries the source-relative file, a one-based line and a one-based
//! column (in characters), an [`ErrorKind`] and, where the error is one of the
//! compiler's lint classes in spec/cwr/compiler.md section 3, the lint code.

use std::fmt;

use serde::Serialize;

/// What kind of defect was found. The set is closed; the compiler maps each kind to
/// its pipeline stage and lint code.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ErrorKind {
    /// A file could not be read or is not valid UTF-8.
    Io,
    /// A line is not a section header, a `key = value` line, a comment or blank.
    Syntax,
    /// A value is not exactly one strict JSON literal.
    Json,
    /// A file the grammar does not define (CWR-C001).
    UnknownFile,
    /// A section the grammar does not define for this file (CWR-C001).
    UnknownSection,
    /// A key the grammar does not define for this section (CWR-C001).
    UnknownKey,
    /// The same section header appears twice in one file.
    DuplicateSection,
    /// The same key appears twice in one section.
    DuplicateKey,
    /// A required section or key is absent. There are no defaults.
    MissingKey,
    /// A value has the wrong JSON type or is outside its allowed set.
    BadValue,
    /// A contract shape rule is violated (CWR-C001).
    Shape,
    /// A failure code outside the eleven-code envelope (CWR-F001).
    Envelope,
    /// An adapter failure-set or content rule is violated (CWR-F002).
    Adapter,
    /// A primitive or adapter is declared more than once (CWR-C002).
    DuplicatePrimitive,
    /// A contract or adapter file for a module absent from `world.modules` (CWR-C003).
    UndeclaredModule,
    /// An expression outside the precondition language (CWR-V002).
    Expression,
    /// An empty `doc` string (CWR-D001).
    Doc,
    /// A `state_shape` change without an existing migration, or a malformed migration (CWR-M001).
    Migration,
    /// A changelog consistency rule is violated.
    Changelog,
    /// A fixture file is malformed.
    Fixture,
}

impl ErrorKind {
    /// The lint code from spec/cwr/compiler.md section 3 that this kind is reported under, if any.
    pub fn lint_code(self) -> Option<&'static str> {
        match self {
            ErrorKind::UnknownFile
            | ErrorKind::UnknownSection
            | ErrorKind::UnknownKey
            | ErrorKind::Shape => Some("CWR-C001"),
            ErrorKind::DuplicatePrimitive => Some("CWR-C002"),
            ErrorKind::UndeclaredModule => Some("CWR-C003"),
            ErrorKind::Envelope => Some("CWR-F001"),
            ErrorKind::Adapter => Some("CWR-F002"),
            ErrorKind::Expression => Some("CWR-V002"),
            ErrorKind::Doc => Some("CWR-D001"),
            ErrorKind::Migration => Some("CWR-M001"),
            _ => None,
        }
    }

    /// Stable lowercase label used in rendered messages.
    pub fn label(self) -> &'static str {
        match self {
            ErrorKind::Io => "io",
            ErrorKind::Syntax => "syntax",
            ErrorKind::Json => "json",
            ErrorKind::UnknownFile => "unknown-file",
            ErrorKind::UnknownSection => "unknown-section",
            ErrorKind::UnknownKey => "unknown-key",
            ErrorKind::DuplicateSection => "duplicate-section",
            ErrorKind::DuplicateKey => "duplicate-key",
            ErrorKind::MissingKey => "missing-key",
            ErrorKind::BadValue => "bad-value",
            ErrorKind::Shape => "shape",
            ErrorKind::Envelope => "envelope",
            ErrorKind::Adapter => "adapter",
            ErrorKind::DuplicatePrimitive => "duplicate-primitive",
            ErrorKind::UndeclaredModule => "undeclared-module",
            ErrorKind::Expression => "expression",
            ErrorKind::Doc => "doc",
            ErrorKind::Migration => "migration",
            ErrorKind::Changelog => "changelog",
            ErrorKind::Fixture => "fixture",
        }
    }
}

/// One located error.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct Error {
    /// Path relative to the World Source root, `/`-separated.
    pub file: String,
    /// One-based line; 1 when the error concerns a whole file.
    pub line: usize,
    /// One-based column in characters; 1 when the error concerns a whole line or file.
    pub col: usize,
    /// Error class; determines [`Error::code`] and the label in the rendered message.
    pub kind: ErrorKind,
    /// Lint code, when the kind has one.
    pub code: Option<&'static str>,
    /// Human-readable explanation; deterministic for a given input.
    pub message: String,
}

impl Error {
    /// Builds a located error and derives its lint code from `kind`.
    pub fn new(
        file: impl Into<String>,
        line: usize,
        col: usize,
        kind: ErrorKind,
        message: impl Into<String>,
    ) -> Self {
        Error {
            file: file.into(),
            line,
            col,
            kind,
            code: kind.lint_code(),
            message: message.into(),
        }
    }
}

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}:{}:{}: ", self.file, self.line, self.col)?;
        match self.code {
            Some(code) => write!(f, "{} {}: {}", code, self.kind.label(), self.message),
            None => write!(f, "{}: {}", self.kind.label(), self.message),
        }
    }
}

impl std::error::Error for Error {}

/// A non-empty list of errors, sorted by file, line and column.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Errors(pub Vec<Error>);

impl Errors {
    /// Sorts errors by file, line, column and message and removes exact duplicates, so the
    /// list is deterministic regardless of discovery order.
    pub fn sorted(mut errors: Vec<Error>) -> Self {
        errors.sort_by(|a, b| {
            (&a.file, a.line, a.col, &a.message).cmp(&(&b.file, b.line, b.col, &b.message))
        });
        errors.dedup();
        Errors(errors)
    }
}

impl fmt::Display for Errors {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        for (index, error) in self.0.iter().enumerate() {
            if index > 0 {
                writeln!(f)?;
            }
            write!(f, "{error}")?;
        }
        Ok(())
    }
}

impl std::error::Error for Errors {}
