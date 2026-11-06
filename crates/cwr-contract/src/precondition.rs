// Copyright Sidiora Labs Inc. CWR® 2026-2027
// SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0

//! Precondition expression language (spec/cwr/world-source.md section 5).
//!
//! Grammar, in order of increasing binding strength:
//!
//! ```text
//! expr    := and_expr ("or" and_expr)*
//! and_expr:= not_expr ("and" not_expr)*
//! not_expr:= "not" not_expr | compare
//! compare := sum (CMP sum)?            CMP is one of == != < <= > >=; no chaining
//! sum     := primary ("+" primary)*    integer addition (reference-world.md kv.set)
//! primary := INT | STRING | NAME | "len" "(" sum ")"
//!          | NAME "." NAME "(" [arg ("," arg)*] ")"     a primitive call
//!          | "(" expr ")"
//! arg     := NAME | INT | STRING      parameters or literals only
//! ```
//!
//! Literals are JSON integer and JSON string literals only. Anything else, including
//! attribute access without a call, indexing, unqualified calls other than `len`,
//! floats, `true`/`false`/`null`, subtraction and other operators, is outside the
//! language and is reported as CWR-V002 with the character offset. Whether a name is a
//! parameter and whether a call targets a read_only primitive of the same Build are
//! symbol-table questions answered by the compiler's resolve and lint stages using
//! [`Expr::calls`] and [`Expr::names`].

use std::fmt;
use std::str::FromStr;

use serde::{Deserialize, Deserializer, Serialize, Serializer};

/// Comparison operators.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CmpOp {
    /// `==`.
    Eq,
    /// `!=`.
    Ne,
    /// `<`.
    Lt,
    /// `<=`.
    Le,
    /// `>`.
    Gt,
    /// `>=`.
    Ge,
}

impl CmpOp {
    /// The operator's source spelling, e.g. `"<="`.
    pub fn as_str(self) -> &'static str {
        match self {
            CmpOp::Eq => "==",
            CmpOp::Ne => "!=",
            CmpOp::Lt => "<",
            CmpOp::Le => "<=",
            CmpOp::Gt => ">",
            CmpOp::Ge => ">=",
        }
    }
}

/// A boolean-level expression.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Expr {
    /// `left or right`.
    Or(Box<Expr>, Box<Expr>),
    /// `left and right`.
    And(Box<Expr>, Box<Expr>),
    /// `not inner`.
    Not(Box<Expr>),
    /// `left CMP right`; comparisons do not chain.
    Compare(CmpOp, Term, Term),
    /// A bare term used as a truth value.
    Term(Term),
}

/// A value-level term.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Term {
    /// A parameter name.
    Name(String),
    /// A JSON integer literal.
    Int(i64),
    /// A JSON string literal, escapes resolved.
    Str(String),
    /// `module.primitive(args)`; `target` is the dotted primitive name.
    Call {
        /// Dotted primitive name, `module.primitive`.
        target: String,
        /// Arguments in call order.
        args: Vec<Arg>,
    },
    /// `len(inner)`.
    Len(Box<Term>),
    /// `left + right`, integer addition.
    Add(Box<Term>, Box<Term>),
    /// A parenthesised expression.
    Group(Box<Expr>),
}

/// A primitive-call argument: a parameter name or a literal.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Arg {
    /// A parameter name.
    Name(String),
    /// A JSON integer literal.
    Int(i64),
    /// A JSON string literal, escapes resolved.
    Str(String),
}

/// A grammar error at a zero-based character offset into the expression.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ExprError {
    /// Zero-based character offset of the error in the expression.
    pub offset: usize,
    /// What the grammar expected or refused.
    pub message: String,
}

impl fmt::Display for ExprError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{} (at offset {})", self.message, self.offset)
    }
}

impl std::error::Error for ExprError {}

#[derive(Debug, Clone, PartialEq)]
enum Tok {
    Name(String),
    Int(i64),
    Str(String),
    LParen,
    RParen,
    Comma,
    Dot,
    Plus,
    Cmp(CmpOp),
    And,
    Or,
    Not,
    End,
}

fn err<T>(offset: usize, message: impl Into<String>) -> Result<T, ExprError> {
    Err(ExprError {
        offset,
        message: message.into(),
    })
}

fn tokenize(source: &str) -> Result<Vec<(Tok, usize)>, ExprError> {
    let chars: Vec<char> = source.chars().collect();
    let mut out = Vec::new();
    let mut i = 0;
    while i < chars.len() {
        let c = chars[i];
        let start = i;
        match c {
            ' ' => {
                i += 1;
                continue;
            }
            '(' => out.push((Tok::LParen, start)),
            ')' => out.push((Tok::RParen, start)),
            ',' => out.push((Tok::Comma, start)),
            '.' => out.push((Tok::Dot, start)),
            '+' => out.push((Tok::Plus, start)),
            '=' | '!' | '<' | '>' => {
                let next = chars.get(i + 1).copied();
                let op = match (c, next) {
                    ('=', Some('=')) => CmpOp::Eq,
                    ('!', Some('=')) => CmpOp::Ne,
                    ('<', Some('=')) => CmpOp::Le,
                    ('>', Some('=')) => CmpOp::Ge,
                    ('<', _) => CmpOp::Lt,
                    ('>', _) => CmpOp::Gt,
                    _ => {
                        return err(
                            start,
                            format!("operator {c:?} is not part of the precondition language"),
                        )
                    }
                };
                if matches!(op, CmpOp::Eq | CmpOp::Ne | CmpOp::Le | CmpOp::Ge) {
                    i += 1;
                }
                out.push((Tok::Cmp(op), start));
            }
            '"' => {
                let mut j = i + 1;
                loop {
                    match chars.get(j) {
                        None => return err(start, "unterminated string literal"),
                        Some('\\') => j += 2,
                        Some('"') => break,
                        Some(_) => j += 1,
                    }
                }
                let literal = &chars[i..=j];
                match crate::kvx::parse_json(literal, 0) {
                    Ok(node) => match node.value {
                        crate::kvx::Json::Str(s) => out.push((Tok::Str(s), start)),
                        _ => return err(start, "invalid string literal"),
                    },
                    Err((col, message)) => {
                        return err(start + col, format!("invalid string literal: {message}"))
                    }
                }
                i = j;
            }
            '-' | '0'..='9' => {
                let mut j = i;
                if c == '-' {
                    j += 1;
                    if !chars.get(j).is_some_and(|d| d.is_ascii_digit()) {
                        return err(
                            start,
                            "subtraction is not part of the precondition language",
                        );
                    }
                }
                while chars.get(j).is_some_and(|d| d.is_ascii_digit()) {
                    j += 1;
                }
                if chars.get(j).is_some_and(|d| {
                    *d == '.' && chars.get(j + 1).is_some_and(|e| e.is_ascii_digit())
                }) || chars.get(j).is_some_and(|d| *d == 'e' || *d == 'E')
                {
                    return err(start, "only integer and string literals are allowed");
                }
                if chars
                    .get(j)
                    .is_some_and(|d| d.is_ascii_alphabetic() || *d == '_')
                {
                    return err(j, "malformed number");
                }
                let text: String = chars[i..j].iter().collect();
                let digits = text.trim_start_matches('-');
                if digits.len() > 1 && digits.starts_with('0') {
                    return err(start, "leading zeros are not allowed in integer literals");
                }
                match text.parse::<i64>() {
                    Ok(n) => out.push((Tok::Int(n), start)),
                    Err(_) => return err(start, "integer literal out of range"),
                }
                i = j;
                continue;
            }
            c if c.is_ascii_alphabetic() || c == '_' => {
                let mut j = i;
                while chars
                    .get(j)
                    .is_some_and(|d| d.is_ascii_alphanumeric() || *d == '_')
                {
                    j += 1;
                }
                let word: String = chars[i..j].iter().collect();
                let tok = match word.as_str() {
                    "and" => Tok::And,
                    "or" => Tok::Or,
                    "not" => Tok::Not,
                    "true" | "false" | "null" | "True" | "False" | "None" => {
                        return err(
                            start,
                            format!("{word} is not allowed; only integer and string literals are"),
                        )
                    }
                    _ => Tok::Name(word),
                };
                out.push((tok, start));
                i = j;
                continue;
            }
            other => {
                return err(
                    start,
                    format!("character {other:?} is not part of the precondition language"),
                )
            }
        }
        i += 1;
    }
    out.push((Tok::End, chars.len()));
    Ok(out)
}

struct Parser {
    toks: Vec<(Tok, usize)>,
    pos: usize,
}

impl Parser {
    fn peek(&self) -> &Tok {
        &self.toks[self.pos].0
    }

    fn offset(&self) -> usize {
        self.toks[self.pos].1
    }

    fn bump(&mut self) -> Tok {
        let tok = self.toks[self.pos].0.clone();
        if self.pos + 1 < self.toks.len() {
            self.pos += 1;
        }
        tok
    }

    fn expect(&mut self, tok: Tok, what: &str) -> Result<(), ExprError> {
        if *self.peek() == tok {
            self.bump();
            Ok(())
        } else {
            err(self.offset(), format!("expected {what}"))
        }
    }

    fn expr(&mut self) -> Result<Expr, ExprError> {
        let mut left = self.and_expr()?;
        while *self.peek() == Tok::Or {
            self.bump();
            let right = self.and_expr()?;
            left = Expr::Or(Box::new(left), Box::new(right));
        }
        Ok(left)
    }

    fn and_expr(&mut self) -> Result<Expr, ExprError> {
        let mut left = self.not_expr()?;
        while *self.peek() == Tok::And {
            self.bump();
            let right = self.not_expr()?;
            left = Expr::And(Box::new(left), Box::new(right));
        }
        Ok(left)
    }

    fn not_expr(&mut self) -> Result<Expr, ExprError> {
        if *self.peek() == Tok::Not {
            self.bump();
            return Ok(Expr::Not(Box::new(self.not_expr()?)));
        }
        self.compare()
    }

    fn compare(&mut self) -> Result<Expr, ExprError> {
        let left = self.sum()?;
        if let Tok::Cmp(op) = *self.peek() {
            self.bump();
            let right = self.sum()?;
            if matches!(self.peek(), Tok::Cmp(_)) {
                return err(self.offset(), "chained comparisons are not allowed");
            }
            return Ok(Expr::Compare(op, left, right));
        }
        Ok(Expr::Term(left))
    }

    fn sum(&mut self) -> Result<Term, ExprError> {
        let mut left = self.primary()?;
        while *self.peek() == Tok::Plus {
            self.bump();
            let right = self.primary()?;
            left = Term::Add(Box::new(left), Box::new(right));
        }
        Ok(left)
    }

    fn arg(&mut self) -> Result<Arg, ExprError> {
        let offset = self.offset();
        match self.bump() {
            Tok::Name(n) => {
                if matches!(self.peek(), Tok::Dot | Tok::LParen) {
                    return err(
                        self.offset(),
                        "primitive arguments must be parameters or literals",
                    );
                }
                Ok(Arg::Name(n))
            }
            Tok::Int(i) => Ok(Arg::Int(i)),
            Tok::Str(s) => Ok(Arg::Str(s)),
            _ => err(offset, "primitive arguments must be parameters or literals"),
        }
    }

    fn primary(&mut self) -> Result<Term, ExprError> {
        let offset = self.offset();
        match self.bump() {
            Tok::Int(i) => Ok(Term::Int(i)),
            Tok::Str(s) => Ok(Term::Str(s)),
            Tok::LParen => {
                let inner = self.expr()?;
                self.expect(Tok::RParen, "')'")?;
                Ok(Term::Group(Box::new(inner)))
            }
            Tok::Name(name) => match self.peek() {
                Tok::LParen if name == "len" => {
                    self.bump();
                    let inner = self.sum()?;
                    self.expect(Tok::RParen, "')' closing len(")?;
                    Ok(Term::Len(Box::new(inner)))
                }
                Tok::LParen => err(offset, format!("call to {name}() is not allowed; only len() and module.primitive() calls are")),
                Tok::Dot => {
                    self.bump();
                    let member_offset = self.offset();
                    let Tok::Name(member) = self.bump() else {
                        return err(member_offset, "expected a primitive name after '.'");
                    };
                    if *self.peek() != Tok::LParen {
                        return err(offset, format!("attribute access {name}.{member} is not allowed; only primitive calls are"));
                    }
                    if name == "len" || member == "len" {
                        return err(offset, "len is not a module or primitive name");
                    }
                    self.bump();
                    let mut args = Vec::new();
                    if *self.peek() != Tok::RParen {
                        loop {
                            args.push(self.arg()?);
                            if *self.peek() == Tok::Comma {
                                self.bump();
                                continue;
                            }
                            break;
                        }
                    }
                    self.expect(Tok::RParen, "')' closing the primitive call")?;
                    if *self.peek() == Tok::Dot {
                        return err(self.offset(), "attribute access on a call result is not allowed");
                    }
                    Ok(Term::Call { target: format!("{name}.{member}"), args })
                }
                _ => {
                    if name == "len" {
                        return err(offset, "len must be called");
                    }
                    Ok(Term::Name(name))
                }
            },
            Tok::End => err(offset, "unexpected end of expression"),
            other => err(offset, format!("unexpected {}", describe(&other))),
        }
    }
}

fn describe(tok: &Tok) -> &'static str {
    match tok {
        Tok::RParen => "')'",
        Tok::Comma => "','",
        Tok::Dot => "'.'",
        Tok::Plus => "'+'",
        Tok::Cmp(_) => "comparison operator",
        Tok::And => "'and'",
        Tok::Or => "'or'",
        Tok::Not => "'not'",
        _ => "token",
    }
}

/// Parses one expression of the precondition language.
pub fn parse(source: &str) -> Result<Expr, ExprError> {
    if source.trim().is_empty() {
        return err(0, "empty expression");
    }
    if let Some(offset) = source.chars().position(|c| c != ' ' && c.is_whitespace()) {
        return err(offset, "only plain spaces may separate tokens");
    }
    let mut parser = Parser {
        toks: tokenize(source)?,
        pos: 0,
    };
    let expr = parser.expr()?;
    if *parser.peek() != Tok::End {
        return err(
            parser.offset(),
            format!(
                "unexpected {} after a complete expression",
                describe(parser.peek())
            ),
        );
    }
    Ok(expr)
}

impl Expr {
    /// Every primitive-call target, in source order (duplicates kept).
    pub fn calls(&self) -> Vec<&str> {
        let mut out = Vec::new();
        self.visit(&mut |t| {
            if let Term::Call { target, .. } = t {
                out.push(target.as_str());
            }
        });
        out
    }

    /// Every bare name: term names and call-argument names, in source order.
    pub fn names(&self) -> Vec<&str> {
        let mut out = Vec::new();
        self.visit(&mut |t| match t {
            Term::Name(n) => out.push(n.as_str()),
            Term::Call { args, .. } => {
                for a in args {
                    if let Arg::Name(n) = a {
                        out.push(n.as_str());
                    }
                }
            }
            _ => {}
        });
        out
    }

    fn visit<'a>(&'a self, f: &mut dyn FnMut(&'a Term)) {
        match self {
            Expr::Or(a, b) | Expr::And(a, b) => {
                a.visit(f);
                b.visit(f);
            }
            Expr::Not(a) => a.visit(f),
            Expr::Compare(_, a, b) => {
                a.visit(f);
                b.visit(f);
            }
            Expr::Term(t) => t.visit(f),
        }
    }
}

impl Term {
    fn visit<'a>(&'a self, f: &mut dyn FnMut(&'a Term)) {
        f(self);
        match self {
            Term::Len(t) => t.visit(f),
            Term::Add(a, b) => {
                a.visit(f);
                b.visit(f);
            }
            Term::Group(e) => e.visit(f),
            _ => {}
        }
    }
}

/// An expression together with its exact source text. Equality and serialisation use the
/// source text; the parsed tree is always derived from it, so an invalid expression cannot
/// be constructed or deserialised.
#[derive(Debug, Clone)]
pub struct Expression {
    source: String,
    ast: Expr,
}

impl Expression {
    /// The exact source text the expression was parsed from.
    pub fn source(&self) -> &str {
        &self.source
    }

    /// The parse tree derived from [`Expression::source`].
    pub fn ast(&self) -> &Expr {
        &self.ast
    }
}

impl PartialEq for Expression {
    fn eq(&self, other: &Self) -> bool {
        self.source == other.source
    }
}

impl Eq for Expression {}

impl FromStr for Expression {
    type Err = ExprError;

    fn from_str(source: &str) -> Result<Self, Self::Err> {
        Ok(Expression {
            source: source.to_string(),
            ast: parse(source)?,
        })
    }
}

impl fmt::Display for Expression {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.source)
    }
}

impl Serialize for Expression {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        serializer.serialize_str(&self.source)
    }
}

impl<'de> Deserialize<'de> for Expression {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        let source = String::deserialize(deserializer)?;
        source.parse().map_err(|e: ExprError| {
            serde::de::Error::custom(format!("invalid expression {source:?}: {e}"))
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn accepts_the_reference_world_expressions() {
        for source in [
            "fs.exists(path)",
            "not fs.locked(path)",
            "len(content) <= 1048576",
            "kv.used(ns) + len(value) <= 65536",
            "n >= 0 and n <= 500",
            "jobs.status(id) == \"queued\" or jobs.status(id) == \"running\"",
            "jobs.active() < 8",
            "fs.read(path) == content",
            "not fs.locked(path) and fs.size(path) < 4096",
            "(a or b) and not (c == -1)",
            "kv.has(ns, \"k\\\"ey\")",
        ] {
            parse(source).unwrap_or_else(|e| panic!("{source}: {e}"));
        }
    }

    #[test]
    fn rejects_everything_outside_the_language() {
        for (source, offset) in [
            ("fs.exists(path) and", 19),
            ("path.parent", 0),
            ("x[0] == 1", 1),
            ("open(path)", 0),
            ("time.time", 0),
            ("a = 1", 2),
            ("a - 1 > 0", 2),
            ("1.5 > a", 0),
            ("a == true", 5),
            ("a < b < c", 6),
            ("fs.read(fs.list(p))", 10),
            ("fs.read(path).size()", 13),
            ("len x", 0),
            ("'x' == a", 0),
            ("a == 01", 5),
            ("", 0),
            ("a\tb", 1),
            ("a ** 2", 2),
        ] {
            let error = parse(source).expect_err(source);
            assert_eq!(error.offset, offset, "{source}: {error}");
        }
    }

    #[test]
    fn reports_calls_and_names() {
        let expr = parse("kv.used(ns) + len(value) <= 65536 and fs.exists(\"/x\")").unwrap();
        assert_eq!(expr.calls(), vec!["kv.used", "fs.exists"]);
        assert_eq!(expr.names(), vec!["ns", "value"]);
    }
}
