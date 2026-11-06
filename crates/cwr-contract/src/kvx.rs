// Copyright Sidiora Labs Inc. CWR® 2026-2027
// SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0

//! Strict KVX reader.
//!
//! KVX is the one-JSON-value-per-line section format used by every World Source file
//! (spec/cwr/world-source.md section 1, decisions.md D/P02):
//!
//! ```text
//! # comment
//! [section.name-1]
//! key = <exactly one strict JSON literal>
//! ```
//!
//! Rules enforced here, each a located error (`file:line:col`):
//!
//! * a non-blank line is a `#` comment, a `[name]` header or a `key = value` line;
//! * section names use `[A-Za-z0-9_.-]` with no empty dot-separated segment;
//! * keys match `[A-Za-z_][A-Za-z0-9_-]*` and appear only after a header;
//! * a section header may not repeat in a file and a key may not repeat in a section;
//! * the value is exactly one RFC 8259 JSON literal with nothing after it: no comments,
//!   no trailing commas, no `NaN`/`Infinity`, no leading zeros, no unescaped control
//!   characters, no lone surrogates, and no duplicate keys inside JSON objects;
//! * lines end at `\n` (an optional `\r` before it is dropped). Every other character a
//!   line-splitting reader may treat as a line break (`\r` elsewhere, U+000B, U+000C,
//!   U+001C to U+001E, U+0085, U+2028, U+2029) is refused wherever it appears, so this reader
//!   and the Python reference reader (`core/cwr_core/kvx.py`) accept the same lines and report
//!   the same line numbers.
//!
//! Object member order is preserved so that ordered declarations such as `params`
//! keep their declared order.

use crate::error::{Error, ErrorKind};

/// A JSON value with the column at which it starts.
#[derive(Debug, Clone, PartialEq)]
pub struct Node {
    /// The parsed value.
    pub value: Json,
    /// One-based character column in the source line.
    pub col: usize,
}

/// A strict JSON value. Integers and floats are kept apart so that `5000.0` is never
/// accepted where an integer is required.
#[derive(Debug, Clone, PartialEq)]
pub enum Json {
    /// `null`.
    Null,
    /// `true` or `false`.
    Bool(bool),
    /// An integer literal without fraction or exponent that fits in `i64`.
    Int(i64),
    /// A number literal with a fraction or exponent.
    Float(f64),
    /// A string literal, escapes resolved.
    Str(String),
    /// An array, elements in source order.
    Array(Vec<Node>),
    /// An object, members in source order; keys are unique.
    Object(Vec<(String, Node)>),
}

impl Json {
    /// Human name of the value's type, used in error messages.
    pub fn type_name(&self) -> &'static str {
        match self {
            Json::Null => "null",
            Json::Bool(_) => "boolean",
            Json::Int(_) => "integer",
            Json::Float(_) => "float",
            Json::Str(_) => "string",
            Json::Array(_) => "array",
            Json::Object(_) => "object",
        }
    }

    /// Converts to a `serde_json::Value` (object members become sorted map entries).
    pub fn to_value(&self) -> serde_json::Value {
        match self {
            Json::Null => serde_json::Value::Null,
            Json::Bool(b) => serde_json::Value::Bool(*b),
            Json::Int(i) => serde_json::Value::from(*i),
            Json::Float(f) => serde_json::Number::from_f64(*f)
                .map(serde_json::Value::Number)
                .unwrap_or(serde_json::Value::Null),
            Json::Str(s) => serde_json::Value::String(s.clone()),
            Json::Array(items) => {
                serde_json::Value::Array(items.iter().map(|n| n.value.to_value()).collect())
            }
            Json::Object(members) => {
                let mut map = serde_json::Map::new();
                for (k, v) in members {
                    map.insert(k.clone(), v.value.to_value());
                }
                serde_json::Value::Object(map)
            }
        }
    }
}

/// One `key = value` line.
#[derive(Debug, Clone, PartialEq)]
pub struct Entry {
    /// The key left of `=`.
    pub key: String,
    /// One-based line number of the entry.
    pub line: usize,
    /// One-based character column at which the key starts.
    pub key_col: usize,
    /// The value right of `=`, with its column.
    pub value: Node,
}

/// One `[name]` section and its entries in file order.
#[derive(Debug, Clone, PartialEq)]
pub struct Section {
    /// Dotted section name between the brackets.
    pub name: String,
    /// One-based line number of the section header.
    pub line: usize,
    /// Entries in file order; keys are unique within a section.
    pub entries: Vec<Entry>,
}

impl Section {
    /// The entry with key `key`, if present.
    pub fn get(&self, key: &str) -> Option<&Entry> {
        self.entries.iter().find(|e| e.key == key)
    }
}

/// A parsed KVX file.
#[derive(Debug, Clone, PartialEq)]
pub struct Document {
    /// Source-relative path used in error locations.
    pub file: String,
    /// Sections in file order; names are unique within a file.
    pub sections: Vec<Section>,
}

impl Document {
    /// The section named `name`, if present.
    pub fn section(&self, name: &str) -> Option<&Section> {
        self.sections.iter().find(|s| s.name == name)
    }
}

/// A strict-JSON grammar error: the one-based column of the offending character and what the
/// grammar expected or refused.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct JsonError {
    /// One-based character column of the error, counted from the `base_col` the parse
    /// started at.
    pub col: usize,
    /// What the grammar expected or refused; deterministic for a given input.
    pub message: String,
}

impl std::fmt::Display for JsonError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{} (column {})", self.message, self.col)
    }
}

impl std::error::Error for JsonError {}

/// Characters that a line-splitting reader such as Python's `str.splitlines` treats as a line
/// break, apart from `\n` itself. KVX refuses them raw anywhere in a line.
fn is_foreign_line_break(c: char) -> bool {
    matches!(
        c,
        '\r' | '\u{0b}'
            | '\u{0c}'
            | '\u{1c}'
            | '\u{1d}'
            | '\u{1e}'
            | '\u{85}'
            | '\u{2028}'
            | '\u{2029}'
    )
}

fn is_section_char(c: char) -> bool {
    c.is_ascii_alphanumeric() || c == '_' || c == '.' || c == '-'
}

fn is_key_start(c: char) -> bool {
    c.is_ascii_alphabetic() || c == '_'
}

fn is_key_char(c: char) -> bool {
    c.is_ascii_alphanumeric() || c == '_' || c == '-'
}

/// Parses a KVX text. Every malformed line is reported; parsing continues with the next
/// line so that one run lists every syntax error in the file.
pub fn parse(file: &str, text: &str) -> Result<Document, Vec<Error>> {
    let mut errors = Vec::new();
    let mut sections: Vec<Section> = Vec::new();
    if text.starts_with('\u{feff}') {
        errors.push(Error::new(
            file,
            1,
            1,
            ErrorKind::Syntax,
            "byte order mark is not allowed",
        ));
    }
    for (index, raw) in text.split('\n').enumerate() {
        let number = index + 1;
        let line = raw.strip_suffix('\r').unwrap_or(raw);
        let chars: Vec<char> = line.chars().collect();
        if let Some((bad, ch)) = chars
            .iter()
            .enumerate()
            .find(|(_, c)| is_foreign_line_break(**c))
        {
            errors.push(Error::new(
                file,
                number,
                bad + 1,
                ErrorKind::Syntax,
                format!(
                    "character U+{:04X} is a line break to other KVX readers; escape it inside a \
                     JSON string or remove it",
                    u32::from(*ch)
                ),
            ));
            continue;
        }
        let start = chars.iter().position(|c| !c.is_whitespace());
        let last = chars.iter().rposition(|c| !c.is_whitespace());
        let (Some(start), Some(last)) = (start, last) else {
            continue;
        };
        let end = last + 1;
        let body = &chars[start..end];
        let col0 = start + 1;
        if body[0] == '#' {
            continue;
        }
        if body[0] == '[' {
            if body.len() < 3 || body[body.len() - 1] != ']' {
                errors.push(Error::new(
                    file,
                    number,
                    col0,
                    ErrorKind::Syntax,
                    "malformed section header",
                ));
                continue;
            }
            let name: String = body[1..body.len() - 1].iter().collect();
            if let Some((bad, ch)) = name.chars().enumerate().find(|(_, c)| !is_section_char(*c)) {
                errors.push(Error::new(
                    file,
                    number,
                    col0 + 1 + bad,
                    ErrorKind::Syntax,
                    format!("character {ch:?} is not allowed in a section name"),
                ));
                continue;
            }
            if name.split('.').any(str::is_empty) {
                errors.push(Error::new(
                    file,
                    number,
                    col0,
                    ErrorKind::Syntax,
                    format!("section name [{name}] has an empty segment"),
                ));
                continue;
            }
            if let Some(previous) = sections.iter().find(|s| s.name == name) {
                errors.push(Error::new(
                    file,
                    number,
                    col0,
                    ErrorKind::DuplicateSection,
                    format!(
                        "duplicate section [{name}], first declared at line {}",
                        previous.line
                    ),
                ));
                // Keep collecting entries into a detached section so key errors still surface.
                sections.push(Section {
                    name: format!("\u{0}dup:{name}"),
                    line: number,
                    entries: Vec::new(),
                });
                continue;
            }
            sections.push(Section {
                name,
                line: number,
                entries: Vec::new(),
            });
            continue;
        }
        // key = value
        if !is_key_start(body[0]) {
            errors.push(Error::new(
                file,
                number,
                col0,
                ErrorKind::Syntax,
                "expected a comment, a [section] header or key = value",
            ));
            continue;
        }
        let mut i = 1;
        while i < body.len() && is_key_char(body[i]) {
            i += 1;
        }
        let key: String = body[..i].iter().collect();
        let mut j = i;
        while j < body.len() && (body[j] == ' ' || body[j] == '\t') {
            j += 1;
        }
        if j >= body.len() || body[j] != '=' {
            errors.push(Error::new(
                file,
                number,
                col0 + j,
                ErrorKind::Syntax,
                format!("expected '=' after key {key}"),
            ));
            continue;
        }
        j += 1;
        while j < body.len() && (body[j] == ' ' || body[j] == '\t') {
            j += 1;
        }
        let Some(section) = sections.last_mut() else {
            errors.push(Error::new(
                file,
                number,
                col0,
                ErrorKind::Syntax,
                format!("key {key} appears before any [section] header"),
            ));
            continue;
        };
        if j >= body.len() {
            errors.push(Error::new(
                file,
                number,
                col0 + j,
                ErrorKind::Json,
                format!("key {key} has no value"),
            ));
            continue;
        }
        let value_col = col0 + j;
        match parse_json(&body[j..], value_col) {
            Ok(node) => {
                if let Some(previous) = section.entries.iter().find(|e| e.key == key) {
                    errors.push(Error::new(
                        file,
                        number,
                        col0,
                        ErrorKind::DuplicateKey,
                        format!(
                            "duplicate key {key} in [{}], first declared at line {}",
                            section.name, previous.line
                        ),
                    ));
                    continue;
                }
                section.entries.push(Entry {
                    key,
                    line: number,
                    key_col: col0,
                    value: node,
                });
            }
            Err(JsonError { col, message }) => errors.push(Error::new(
                file,
                number,
                col,
                ErrorKind::Json,
                format!("value of {key} is not a single strict JSON literal: {message}"),
            )),
        }
    }
    sections.retain(|s| !s.name.starts_with('\u{0}'));
    if errors.is_empty() {
        Ok(Document {
            file: file.to_string(),
            sections,
        })
    } else {
        Err(errors)
    }
}

/// Parses exactly one strict JSON literal from `chars`; `base_col` is the column of `chars[0]`.
/// Returns the failing column and a message on error.
pub fn parse_json(chars: &[char], base_col: usize) -> Result<Node, JsonError> {
    let mut parser = JsonParser {
        chars,
        pos: 0,
        base_col,
        depth: 0,
    };
    parser.skip_ws();
    let node = parser.value()?;
    parser.skip_ws();
    if parser.pos != chars.len() {
        return parser.err("unexpected text after the JSON value");
    }
    Ok(node)
}

/// Parses one strict JSON literal from a string (column 1 is the first character).
pub fn parse_json_str(text: &str) -> Result<Node, JsonError> {
    let chars: Vec<char> = text.chars().collect();
    parse_json(&chars, 1)
}

const MAX_DEPTH: usize = 64;

struct JsonParser<'a> {
    chars: &'a [char],
    pos: usize,
    base_col: usize,
    depth: usize,
}

impl JsonParser<'_> {
    fn col(&self) -> usize {
        self.base_col + self.pos
    }

    fn err<T>(&self, message: impl Into<String>) -> Result<T, JsonError> {
        self.err_at(self.col(), message)
    }

    fn err_at<T>(&self, col: usize, message: impl Into<String>) -> Result<T, JsonError> {
        Err(JsonError {
            col,
            message: message.into(),
        })
    }

    fn peek(&self) -> Option<char> {
        self.chars.get(self.pos).copied()
    }

    fn skip_ws(&mut self) {
        while let Some(c) = self.peek() {
            if c == ' ' || c == '\t' {
                self.pos += 1;
            } else {
                break;
            }
        }
    }

    fn expect_word(&mut self, word: &str) -> Result<(), JsonError> {
        for expected in word.chars() {
            if self.peek() != Some(expected) {
                return self.err(format!("invalid literal, expected {word}"));
            }
            self.pos += 1;
        }
        Ok(())
    }

    fn value(&mut self) -> Result<Node, JsonError> {
        let col = self.col();
        let value = match self.peek() {
            None => return self.err("expected a JSON value"),
            Some('n') => {
                self.expect_word("null")?;
                Json::Null
            }
            Some('t') => {
                self.expect_word("true")?;
                Json::Bool(true)
            }
            Some('f') => {
                self.expect_word("false")?;
                Json::Bool(false)
            }
            Some('"') => Json::Str(self.string()?),
            Some('[') => self.array()?,
            Some('{') => self.object()?,
            Some(c) if c == '-' || c.is_ascii_digit() => self.number()?,
            Some(c) => return self.err(format!("unexpected character {c:?}")),
        };
        Ok(Node { value, col })
    }

    fn enter(&mut self) -> Result<(), JsonError> {
        self.depth += 1;
        if self.depth > MAX_DEPTH {
            return self.err("nesting deeper than 64 levels");
        }
        Ok(())
    }

    fn array(&mut self) -> Result<Json, JsonError> {
        self.enter()?;
        self.pos += 1;
        self.skip_ws();
        let mut items = Vec::new();
        if self.peek() == Some(']') {
            self.pos += 1;
            self.depth -= 1;
            return Ok(Json::Array(items));
        }
        loop {
            self.skip_ws();
            items.push(self.value()?);
            self.skip_ws();
            match self.peek() {
                Some(',') => {
                    self.pos += 1;
                    self.skip_ws();
                    if self.peek() == Some(']') {
                        return self.err("trailing comma in array");
                    }
                }
                Some(']') => {
                    self.pos += 1;
                    break;
                }
                _ => return self.err("expected ',' or ']' in array"),
            }
        }
        self.depth -= 1;
        Ok(Json::Array(items))
    }

    fn object(&mut self) -> Result<Json, JsonError> {
        self.enter()?;
        self.pos += 1;
        self.skip_ws();
        let mut members: Vec<(String, Node)> = Vec::new();
        if self.peek() == Some('}') {
            self.pos += 1;
            self.depth -= 1;
            return Ok(Json::Object(members));
        }
        loop {
            self.skip_ws();
            if self.peek() != Some('"') {
                return self.err("expected a string object key");
            }
            let key_col = self.col();
            let key = self.string()?;
            if members.iter().any(|(k, _)| *k == key) {
                return self.err_at(key_col, format!("duplicate JSON object key {key:?}"));
            }
            self.skip_ws();
            if self.peek() != Some(':') {
                return self.err("expected ':' after object key");
            }
            self.pos += 1;
            self.skip_ws();
            let value = self.value()?;
            members.push((key, value));
            self.skip_ws();
            match self.peek() {
                Some(',') => {
                    self.pos += 1;
                    self.skip_ws();
                    if self.peek() == Some('}') {
                        return self.err("trailing comma in object");
                    }
                }
                Some('}') => {
                    self.pos += 1;
                    break;
                }
                _ => return self.err("expected ',' or '}' in object"),
            }
        }
        self.depth -= 1;
        Ok(Json::Object(members))
    }

    fn hex4(&mut self) -> Result<u32, JsonError> {
        let mut value = 0u32;
        for _ in 0..4 {
            match self.peek().and_then(|c| c.to_digit(16)) {
                Some(d) => {
                    value = value * 16 + d;
                    self.pos += 1;
                }
                None => return self.err("invalid \\u escape"),
            }
        }
        Ok(value)
    }

    fn string(&mut self) -> Result<String, JsonError> {
        self.pos += 1; // opening quote
        let mut out = String::new();
        loop {
            let Some(c) = self.peek() else {
                return self.err("unterminated string");
            };
            match c {
                '"' => {
                    self.pos += 1;
                    return Ok(out);
                }
                '\\' => {
                    self.pos += 1;
                    let Some(e) = self.peek() else {
                        return self.err("unterminated escape");
                    };
                    self.pos += 1;
                    match e {
                        '"' => out.push('"'),
                        '\\' => out.push('\\'),
                        '/' => out.push('/'),
                        'b' => out.push('\u{8}'),
                        'f' => out.push('\u{c}'),
                        'n' => out.push('\n'),
                        'r' => out.push('\r'),
                        't' => out.push('\t'),
                        'u' => {
                            let first = self.hex4()?;
                            let code = if (0xD800..0xDC00).contains(&first) {
                                if self.peek() != Some('\\')
                                    || self.chars.get(self.pos + 1) != Some(&'u')
                                {
                                    return self.err("lone high surrogate");
                                }
                                self.pos += 2;
                                let second = self.hex4()?;
                                if !(0xDC00..0xE000).contains(&second) {
                                    return self.err("invalid low surrogate");
                                }
                                0x10000 + ((first - 0xD800) << 10) + (second - 0xDC00)
                            } else if (0xDC00..0xE000).contains(&first) {
                                return self.err("lone low surrogate");
                            } else {
                                first
                            };
                            match char::from_u32(code) {
                                Some(ch) => out.push(ch),
                                None => return self.err("invalid code point"),
                            }
                        }
                        other => {
                            self.pos -= 1;
                            return self.err(format!("invalid escape \\{other}"));
                        }
                    }
                }
                c if (c as u32) < 0x20 => return self.err("unescaped control character in string"),
                c => {
                    out.push(c);
                    self.pos += 1;
                }
            }
        }
    }

    fn number(&mut self) -> Result<Json, JsonError> {
        let start = self.pos;
        if self.peek() == Some('-') {
            self.pos += 1;
        }
        match self.peek() {
            Some('0') => {
                self.pos += 1;
                if self.peek().is_some_and(|c| c.is_ascii_digit()) {
                    return self.err("leading zeros are not allowed");
                }
            }
            Some(c) if c.is_ascii_digit() => {
                while self.peek().is_some_and(|c| c.is_ascii_digit()) {
                    self.pos += 1;
                }
            }
            _ => return self.err("expected digits"),
        }
        let mut is_float = false;
        if self.peek() == Some('.') {
            is_float = true;
            self.pos += 1;
            if !self.peek().is_some_and(|c| c.is_ascii_digit()) {
                return self.err("expected digits after decimal point");
            }
            while self.peek().is_some_and(|c| c.is_ascii_digit()) {
                self.pos += 1;
            }
        }
        if matches!(self.peek(), Some('e') | Some('E')) {
            is_float = true;
            self.pos += 1;
            if matches!(self.peek(), Some('+') | Some('-')) {
                self.pos += 1;
            }
            if !self.peek().is_some_and(|c| c.is_ascii_digit()) {
                return self.err("expected digits in exponent");
            }
            while self.peek().is_some_and(|c| c.is_ascii_digit()) {
                self.pos += 1;
            }
        }
        let text: String = self.chars[start..self.pos].iter().collect();
        if is_float {
            match text.parse::<f64>() {
                Ok(f) if f.is_finite() => Ok(Json::Float(f)),
                _ => self.err_at(
                    self.base_col + start,
                    format!("number {text} is out of range"),
                ),
            }
        } else {
            match text.parse::<i64>() {
                Ok(i) => Ok(Json::Int(i)),
                Err(_) => self.err_at(
                    self.base_col + start,
                    format!("integer {text} is out of range"),
                ),
            }
        }
    }
}

/// Renders a string as a JSON string literal (used by the KVX emitter).
///
/// `"` and `\` are escaped, control characters use the short escapes `\b \t \n \f \r` or
/// `\u00xx` with lowercase hex, and U+0085, U+2028 and U+2029 are written as `\u0085`,
/// `\u2028` and `\u2029` so the literal never contains a character another KVX reader treats
/// as a line break (the same rule as `cwr_core.kvx.dumps_value`). Every other character,
/// including non-ASCII, is written unchanged; apart from those three escapes the output is
/// byte-identical to `serde_json::to_string(s)`. It never fails.
pub fn quote(s: &str) -> String {
    let mut out = String::with_capacity(s.len() + 2);
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\u{08}' => out.push_str("\\b"),
            '\t' => out.push_str("\\t"),
            '\n' => out.push_str("\\n"),
            '\u{0c}' => out.push_str("\\f"),
            '\r' => out.push_str("\\r"),
            c if u32::from(c) < 0x20 => out.push_str(&format!("\\u{:04x}", u32::from(c))),
            '\u{85}' | '\u{2028}' | '\u{2029}' => out.push_str(&format!("\\u{:04x}", u32::from(c))),
            c => out.push(c),
        }
    }
    out.push('"');
    out
}

/// Renders a sequence of strings as a compact JSON array of string literals, byte-identical
/// to `serde_json::to_string` of the same `Vec<String>` (no spaces after commas). It never
/// fails.
pub fn quote_list<I, S>(items: I) -> String
where
    I: IntoIterator<Item = S>,
    S: AsRef<str>,
{
    let body: Vec<String> = items.into_iter().map(|s| quote(s.as_ref())).collect();
    format!("[{}]", body.join(","))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn quote_matches_serde_json_byte_for_byte() {
        let mut all: String = (0u8..0x80).map(char::from).collect();
        all.push_str("\u{7f}\u{e9}\u{1f600}");
        for s in ["", "plain", "a\"b\\c", all.as_str()] {
            assert_eq!(quote(s), serde_json::to_string(s).unwrap(), "{s:?}");
        }
        // Deliberate difference from serde_json: the three non-ASCII line breaks are escaped,
        // as cwr_core.kvx.dumps_value does, so the literal stays on one line for every reader.
        assert_eq!(
            quote("a\u{85}b\u{2028}c\u{2029}d"),
            "\"a\\u0085b\\u2028c\\u2029d\""
        );
        let list = vec!["x".to_string(), "y\n".to_string()];
        assert_eq!(quote_list(&list), serde_json::to_string(&list).unwrap());
        assert_eq!(quote_list(Vec::<String>::new()), "[]");
    }

    #[test]
    fn parses_sections_and_values() {
        let doc = parse(
            "t.kvx",
            "# c\n[a.b-c]\nk = {\"x\": [1, \"two\", null]}\n\n[d]\ne = true\n",
        )
        .unwrap();
        assert_eq!(doc.sections.len(), 2);
        assert_eq!(doc.sections[0].name, "a.b-c");
        assert_eq!(doc.sections[0].entries[0].value.col, 5);
        assert_eq!(doc.sections[1].entries[0].value.value, Json::Bool(true));
    }

    #[test]
    fn rejects_malformed_lines_with_locations() {
        let cases = [
            ("[a]\nk = 1\nk = 2\n", 3, 1, ErrorKind::DuplicateKey),
            ("[a]\n[a]\n", 2, 1, ErrorKind::DuplicateSection),
            ("k = 1\n", 1, 1, ErrorKind::Syntax),
            ("[a]\nk = 1 # trailing\n", 2, 7, ErrorKind::Json),
            ("[a]\nk = 'x'\n", 2, 5, ErrorKind::Json),
            ("[a]\nk = 01\n", 2, 6, ErrorKind::Json),
            ("[a]\nk = [1,]\n", 2, 8, ErrorKind::Json),
            ("[a]\nk = NaN\n", 2, 5, ErrorKind::Json),
            ("[a]\nk = {\"x\": 1, \"x\": 2}\n", 2, 14, ErrorKind::Json),
            ("[a]\nk = \"\\ud800\"\n", 2, 12, ErrorKind::Json),
            ("[a]\nk\n", 2, 2, ErrorKind::Syntax),
            ("[a b]\n", 1, 3, ErrorKind::Syntax),
            ("[a..b]\n", 1, 1, ErrorKind::Syntax),
            ("[a]\nk = 1 2\n", 2, 7, ErrorKind::Json),
            ("[a]\nk =\n", 2, 4, ErrorKind::Json),
        ];
        for (text, line, col, kind) in cases {
            let errors = parse("f.kvx", text).unwrap_err();
            assert_eq!(
                (errors[0].line, errors[0].col, errors[0].kind),
                (line, col, kind),
                "{text:?}: {}",
                errors[0]
            );
        }
    }

    #[test]
    fn refuses_characters_other_readers_split_lines_on() {
        for (text, col) in [
            ("[a]\nk = \"x\u{2028}y\"\n", 7),
            ("[a]\nk = \"x\u{2029}y\"\n", 7),
            ("[a]\nk = \"x\u{85}y\"\n", 7),
            ("[a]\nk = 1\u{0b}\n", 6),
            ("[a]\nk = 1\u{0c}\n", 6),
            ("[a]\nk = \"\u{1c}\"\n", 6),
            ("[a]\nk = 1\rj = 2\n", 6),
            ("[a]\u{1e}\n", 4),
        ] {
            let errors = parse("f.kvx", text).unwrap_err();
            assert_eq!(
                (errors[0].line, errors[0].col, errors[0].kind),
                (2.min(text.matches('\n').count()), col, ErrorKind::Syntax),
                "{text:?}: {}",
                errors[0]
            );
        }
        // CRLF line endings stay accepted, and an escaped separator is ordinary content.
        let doc = parse("f.kvx", "[a]\r\nk = \"x\\u2028y\"\r\n").unwrap();
        assert_eq!(
            doc.sections[0].entries[0].value.value,
            Json::Str("x\u{2028}y".into())
        );
    }

    #[test]
    fn emitted_strings_reparse_to_the_same_value() {
        let mut all: String = (0u32..0x3000).filter_map(char::from_u32).collect();
        all.push('\u{1f600}');
        let text = format!("[a]\nk = {}\n", quote(&all));
        let doc = parse("f.kvx", &text).unwrap();
        assert_eq!(doc.sections[0].entries[0].value.value, Json::Str(all));
    }

    #[test]
    fn json_errors_are_typed_and_located() {
        let error = parse_json_str("[1, 2,]").unwrap_err();
        assert_eq!(error.col, 7);
        assert_eq!(error.message, "trailing comma in array");
        assert_eq!(error.to_string(), "trailing comma in array (column 7)");
        let deep = format!("{}{}", "[".repeat(10_000), "]".repeat(10_000));
        assert!(parse_json_str(&deep).is_err());
    }

    #[test]
    fn keeps_integers_and_floats_apart() {
        assert_eq!(parse_json_str("5000").unwrap().value, Json::Int(5000));
        assert_eq!(parse_json_str("5e3").unwrap().value, Json::Float(5000.0));
        assert!(parse_json_str("99999999999999999999").is_err());
    }

    #[test]
    fn preserves_object_order() {
        let node = parse_json_str("{\"b\": 1, \"a\": 2}").unwrap();
        let Json::Object(members) = node.value else {
            panic!()
        };
        assert_eq!(members[0].0, "b");
    }
}
