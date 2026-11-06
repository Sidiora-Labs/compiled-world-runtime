// Copyright Sidiora Labs Inc. CWR® 2026-2027
// SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0

//! CWR contract crate: the parse stage of the World Compiler.
//!
//! * [`kvx`]: the strict KVX reader (sections, one strict JSON literal per key, comments,
//!   duplicate sections and keys rejected, every error located `file:line:col`).
//! * [`envelope`]: the closed eleven-code failure envelope (clause 2.3).
//! * [`precondition`]: the precondition expression language (world-source.md section 5).
//! * [`world`], [`primitive`], [`adapter`], [`changelog`], [`fixture`]: typed models of the
//!   World Source files with their shape rules (clauses 3.1 to 3.4 and 3.6).
//! * [`source`]: the World Source directory, its file grammar and the [`WorldSource`] model,
//!   which round-trips through JSON and through canonical KVX.
//!
//! Spec: spec/cwr/world-source.md; task 0.3 in spec/cwr/spec.kvx; case CWR-001.

#![warn(missing_docs)]

pub mod adapter;
pub mod changelog;
pub mod envelope;
pub mod error;
pub mod fixture;
pub mod kvx;
pub mod precondition;
pub mod primitive;
mod read;
pub mod source;
pub mod types;
pub mod world;

pub use adapter::{Adapter, AdapterFile, Idempotency};
pub use changelog::{Bump, Change, ChangeKind, Changelog, Release};
pub use envelope::{Code, Envelope, EnvelopeError, Failure, LimitName, Ticks};
pub use error::{Error, ErrorKind, Errors};
pub use fixture::{DriftEntry, DriftFile, Expect, FixtureCase, FixtureFile, MigrationCase};
pub use precondition::{Expr, Expression};
pub use primitive::{ContractFile, Precondition, Primitive};
pub use read::{is_ident, is_member};
pub use source::{FileClass, MigrationFile, SourceFile, SourceTree, WorldSource};
pub use types::{Params, SideEffect, TypeError, TypeExpr, Version, VersionError};
pub use world::{Clock, Limits, Rng, World, WorldFile};
