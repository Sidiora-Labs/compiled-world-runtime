// Copyright Sidiora Labs Inc. CWR® 2026-2027
// SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0

//! The gateway's two outward-facing parts (kernel.md section 7; clauses 7.1 and 7.6):
//!
//! * [`post`]: the HTTP/1.x client that is the kernel's **only** network capability. It contacts
//!   exactly the `endpoint` declared in an adapter contract: `http://host[:port]/path`, or
//!   `stub://<server>/<path>` resolved through the operator's stub map to
//!   `http://host:port/<path>`. Any other scheme, an unmapped stub server or a malformed endpoint
//!   is refused as a transport error before any socket exists. One request per call, never a
//!   retry, under one total host-time deadline of the contract's `timeout_ms`.
//! * [`AdapterCode`]: the worker that runs the Build's `adapters/<module>/live.py`, the
//!   gateway-side implementation an author ships per adapter module (world-source.md section 7).
//!   It runs in the same kind of sandbox as `lib/` (an empty network namespace, a read-only view
//!   holding only the interpreter and the Build's `adapters/`), so adapter code has no socket of
//!   its own: its only way out is `gateway.send(payload)`, a line on its stdout that the kernel
//!   answers after performing the call through [`post`] or the cassette.

use std::collections::{BTreeMap, BTreeSet};
use std::io::{BufRead, BufReader, ErrorKind, Read, Write};
use std::net::{TcpStream, ToSocketAddrs};
use std::path::Path;
use std::process::{Child, ChildStdin};
use std::sync::mpsc::{self, Receiver, RecvTimeoutError};
use std::time::Duration;

use serde_json::{json, Value};

use super::cassette::{kept_headers, RawResponse};
use super::timeout::Deadline;

// ---------------------------------------------------------------------------------------------
// Endpoint resolution
// ---------------------------------------------------------------------------------------------

/// Where one request goes.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Target {
    /// Host name or address.
    pub host: String,
    /// TCP port.
    pub port: u16,
    /// Request path, starting with `/`.
    pub path: String,
}

impl Target {
    /// The `http://host:port/path` URL of this target.
    pub fn url(&self) -> String {
        format!("http://{}:{}{}", self.host, self.port, self.path)
    }
}

fn parse_http(url: &str) -> Result<Target, String> {
    let rest = url.strip_prefix("http://").ok_or_else(|| {
        format!("endpoint {url:?}: only http:// and stub:// endpoints are supported")
    })?;
    let (authority, path) = match rest.find('/') {
        Some(i) => (&rest[..i], &rest[i..]),
        None => (rest, "/"),
    };
    if authority.is_empty() || authority.contains('@') {
        return Err(format!("endpoint {url:?}: malformed authority"));
    }
    let (host, port) = match authority.rsplit_once(':') {
        Some((h, p)) => (
            h,
            p.parse::<u16>()
                .map_err(|_| format!("endpoint {url:?}: bad port"))?,
        ),
        None => (authority, 80),
    };
    if host.is_empty()
        || !host
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || matches!(c, '.' | '-' | '[' | ']' | ':'))
    {
        return Err(format!("endpoint {url:?}: malformed host"));
    }
    if path.chars().any(|c| c.is_ascii_control() || c == ' ') {
        return Err(format!("endpoint {url:?}: malformed path"));
    }
    Ok(Target {
        host: host.to_string(),
        port,
        path: path.to_string(),
    })
}

/// Resolves a contract's declared `endpoint`. `stubs` maps a stub server name to its base URL
/// (`http://127.0.0.1:8001`); it binds names, it does not add destinations: only the endpoint the
/// contract declares is ever resolved.
pub fn resolve(endpoint: &str, stubs: &BTreeMap<String, String>) -> Result<Target, String> {
    if let Some(rest) = endpoint.strip_prefix("stub://") {
        let (server, path) = match rest.find('/') {
            Some(i) => (&rest[..i], &rest[i..]),
            None => {
                return Err(format!(
                    "endpoint {endpoint:?}: a stub endpoint is stub://<server>/<path>"
                ))
            }
        };
        let base = stubs.get(server).ok_or_else(|| {
            format!("endpoint {endpoint:?}: no address is configured for stub server {server:?}")
        })?;
        let b = parse_http(base)?;
        let joined = format!("{}{}", b.path.trim_end_matches('/'), path);
        return Ok(Target { path: joined, ..b });
    }
    parse_http(endpoint)
}

// ---------------------------------------------------------------------------------------------
// The HTTP client
// ---------------------------------------------------------------------------------------------

fn error_class(e: &std::io::Error) -> String {
    match e.kind() {
        ErrorKind::ConnectionRefused => "ConnectionRefusedError",
        ErrorKind::ConnectionReset => "ConnectionResetError",
        ErrorKind::ConnectionAborted => "ConnectionAbortedError",
        ErrorKind::BrokenPipe => "BrokenPipeError",
        ErrorKind::UnexpectedEof => "RemoteDisconnected",
        ErrorKind::AddrNotAvailable => "AddrNotAvailable",
        ErrorKind::PermissionDenied => "PermissionError",
        _ => "OSError",
    }
    .to_string()
}

fn timed_out(e: &std::io::Error) -> bool {
    matches!(e.kind(), ErrorKind::TimedOut | ErrorKind::WouldBlock)
}

fn find(hay: &[u8], needle: &[u8]) -> Option<usize> {
    hay.windows(needle.len()).position(|w| w == needle)
}

/// Parses a complete or partial response buffer: `Some` once the head and (with a
/// Content-Length) the whole body have arrived, or at EOF.
fn parse_response(buf: &[u8], eof: bool) -> Option<Result<RawResponse, String>> {
    let head_end = find(buf, b"\r\n\r\n")?;
    let head = String::from_utf8_lossy(&buf[..head_end]).to_string();
    let mut lines = head.split("\r\n");
    let status_line = lines.next().unwrap_or("");
    let mut parts = status_line.split_whitespace();
    let proto = parts.next().unwrap_or("");
    let status = match (
        proto.starts_with("HTTP/1."),
        parts.next().and_then(|s| s.parse::<u16>().ok()),
    ) {
        (true, Some(s)) if (100..=599).contains(&s) => s,
        _ => return Some(Err("BadStatusLine".into())),
    };
    let mut headers = Vec::new();
    for l in lines {
        if let Some((k, v)) = l.split_once(':') {
            headers.push((k.trim().to_string(), v.trim().to_string()));
        }
    }
    let body = &buf[head_end + 4..];
    let length = headers
        .iter()
        .find(|(k, _)| k.eq_ignore_ascii_case("content-length"))
        .and_then(|(_, v)| v.parse::<usize>().ok());
    match length {
        Some(n) if body.len() >= n => {}
        Some(_) if eof => return Some(Err("IncompleteRead".into())),
        Some(_) => return None,
        None if !eof => return None,
        None => {}
    }
    let body = match length {
        Some(n) => &body[..n],
        None => body,
    };
    Some(Ok(RawResponse::Http {
        status,
        headers: kept_headers(&headers),
        body: String::from_utf8_lossy(body).to_string(),
    }))
}

/// Sends one POST with `body` to `target` and returns the raw response in cassette form. The
/// whole exchange (address lookup, connect, send, receive) shares `deadline`; on expiry the
/// answer is [`RawResponse::Timeout`]. Never retries: a failure is reported, not repeated.
pub fn post(
    target: &Target,
    body: &[u8],
    headers: &[(&str, String)],
    deadline: &Deadline,
) -> RawResponse {
    let addrs: Vec<_> = match (
        target.host.trim_start_matches('[').trim_end_matches(']'),
        target.port,
    )
        .to_socket_addrs()
    {
        Ok(a) => a.collect(),
        Err(_) => {
            return RawResponse::TransportError {
                error: "gaierror".into(),
            }
        }
    };
    let Some(addr) = addrs.first() else {
        return RawResponse::TransportError {
            error: "gaierror".into(),
        };
    };
    let Some(left) = deadline.remaining() else {
        return RawResponse::Timeout;
    };
    // One address, one connection attempt: trying further addresses would be a retry.
    let mut stream = match TcpStream::connect_timeout(addr, left) {
        Ok(s) => s,
        Err(e) if timed_out(&e) => return RawResponse::Timeout,
        Err(e) => {
            return RawResponse::TransportError {
                error: error_class(&e),
            }
        }
    };
    let _ = stream.set_nodelay(true);
    let mut req = format!("POST {} HTTP/1.1\r\nHost: {}:{}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n", target.path, target.host, target.port, body.len());
    for (k, v) in headers {
        req.push_str(&format!("{k}: {v}\r\n"));
    }
    req.push_str("\r\n");
    let mut bytes = req.into_bytes();
    bytes.extend_from_slice(body);
    let Some(left) = deadline.remaining() else {
        return RawResponse::Timeout;
    };
    let _ = stream.set_write_timeout(Some(left));
    if let Err(e) = stream.write_all(&bytes) {
        return if timed_out(&e) || deadline.expired() {
            RawResponse::Timeout
        } else {
            RawResponse::TransportError {
                error: error_class(&e),
            }
        };
    }
    let mut buf = Vec::new();
    let mut chunk = [0u8; 8192];
    loop {
        let Some(left) = deadline.remaining() else {
            return RawResponse::Timeout;
        };
        let _ = stream.set_read_timeout(Some(left));
        match stream.read(&mut chunk) {
            Ok(0) => {
                if buf.is_empty() {
                    return RawResponse::TransportError {
                        error: "RemoteDisconnected".into(),
                    };
                }
                return match parse_response(&buf, true) {
                    Some(Ok(r)) => r,
                    Some(Err(e)) => RawResponse::TransportError { error: e },
                    None => RawResponse::TransportError {
                        error: "IncompleteRead".into(),
                    },
                };
            }
            Ok(n) => {
                buf.extend_from_slice(&chunk[..n]);
                match parse_response(&buf, false) {
                    Some(Ok(r)) => return r,
                    Some(Err(e)) => return RawResponse::TransportError { error: e },
                    None => {}
                }
            }
            Err(e) if e.kind() == ErrorKind::Interrupted => {}
            Err(e) if timed_out(&e) => return RawResponse::Timeout,
            Err(e) => {
                return RawResponse::TransportError {
                    error: error_class(&e),
                }
            }
        }
    }
}

// ---------------------------------------------------------------------------------------------
// The adapter-code worker (adapters/<module>/live.py)
// ---------------------------------------------------------------------------------------------

/// Per-adapter constants a live.py module declares (examples/refworld/README.md, convention 2).
/// The undeclared-status fallback and the retry_after ratio are not among them: they are rules
/// of the adapter contract (`undeclared_status`, `retry_after_ratio` in contracts.json), read
/// the same way by the compiler's contract tests and by this gateway.
#[derive(Debug, Clone, Default)]
pub struct AdapterConsts {
    /// Whether live.py defines the adapter's call function.
    pub has_function: bool,
    /// `STATUS_CODES[adapter]`: status to `ok` or a code name; `None` for the wire protocol.
    pub status_codes: Option<BTreeMap<u16, String>>,
    /// Body field that carries the service's retry delay, if declared.
    pub retry_after_field: Option<String>,
    /// Parameter that carries the idempotency key, if declared.
    pub key_param: Option<String>,
}

impl AdapterConsts {
    fn from_json(v: &Value) -> AdapterConsts {
        let status_codes = v.get("status_codes").and_then(Value::as_object).map(|m| {
            m.iter()
                .filter_map(|(k, c)| Some((k.parse::<u16>().ok()?, c.as_str()?.to_string())))
                .collect()
        });
        AdapterConsts {
            has_function: v
                .get("has_function")
                .and_then(Value::as_bool)
                .unwrap_or(false),
            status_codes,
            retry_after_field: v
                .get("retry_after_field")
                .and_then(Value::as_str)
                .map(str::to_string),
            key_param: v
                .get("key_param")
                .and_then(Value::as_str)
                .map(str::to_string),
        }
    }
}

/// The worker program. Protocol: one JSON object per line. `init` loads every listed module and
/// answers its constants; `call` runs one adapter function with a client whose `send(payload)`
/// emits `{"cb": "send", "payload": ...}` and blocks for the kernel's answer: `{"body": ...}` or
/// `{"code": ...}` (raised as `GatewayFailure` inside the adapter code). Anything the adapter code
/// prints goes to stderr, never into the protocol.
pub const WORKER: &str = r#####"
import importlib.util, json, keyword, sys
_out = sys.stdout
sys.stdout = sys.stderr
ROOT = "/build/lib"
MODS = {}

def emit(v):
    line = json.dumps(v, sort_keys=True, ensure_ascii=False, allow_nan=False)
    _out.write(line + "\n")
    _out.flush()

def recv():
    line = sys.stdin.readline()
    if not line:
        raise SystemExit(0)
    return json.loads(line)

class GatewayFailure(Exception):
    def __init__(self, code, detail=""):
        super().__init__(code)
        self.code = code
        self.detail = detail

class Client:
    """The gateway client handed to adapter code: send() is its only way out."""
    def send(self, payload):
        emit({"cb": "send", "payload": payload})
        r = recv()
        if "body" in r:
            return r["body"]
        raise GatewayFailure(r.get("code", "UNAVAILABLE"), r.get("detail", ""))

def pyname(n):
    return n + "_" if keyword.iskeyword(n) else n

def load(m):
    spec = importlib.util.spec_from_file_location("cwr_live_" + m, ROOT + "/" + m + "/live.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

def consts(mod, adapter, member):
    sc = getattr(mod, "STATUS_CODES", None)
    m = sc.get(adapter) if isinstance(sc, dict) else None
    keyp = getattr(mod, "IDEMPOTENCY_KEY_PARAM", None)
    field = getattr(mod, "RETRY_AFTER_FIELD", None)
    return {
        "has_function": callable(getattr(mod, pyname(member), None)),
        "status_codes": {str(int(k)): str(v) for k, v in m.items()} if isinstance(m, dict) else None,
        "retry_after_field": field if isinstance(field, str) else None,
        "key_param": keyp.get(adapter) if isinstance(keyp, dict) and isinstance(keyp.get(adapter), str) else None,
    }

while True:
    line = sys.stdin.readline()
    if not line:
        break
    req = json.loads(line)
    op = req.get("op")
    if op == "init":
        errors = []
        for m in req["modules"]:
            try:
                MODS[m] = load(m)
            except BaseException as e:
                errors.append("%s: %s: %s" % (m, type(e).__name__, e))
        out = {}
        for a in req["adapters"]:
            m, member = a.split(".", 1)
            if m in MODS:
                try:
                    out[a] = consts(MODS[m], a, member)
                except BaseException as e:
                    errors.append("%s: constants: %s: %s" % (a, type(e).__name__, e))
        emit({"constants": out, "errors": errors})
    elif op == "call":
        a = req["adapter"]
        m, member = a.split(".", 1)
        mod = MODS.get(m)
        fn = getattr(mod, pyname(member), None) if mod is not None else None
        if not callable(fn):
            emit({"missing": True})
            continue
        try:
            result = fn(Client(), *req["args"])
            emit({"result": result})
        except GatewayFailure as e:
            emit({"failed": e.code})
        except BaseException as e:
            emit({"exception": "%s: %s" % (type(e).__name__, e)})
    else:
        emit({"fatal": "unknown op %r" % (op,)})
"#####;

/// The running adapter-code worker of one turn.
pub struct AdapterCode {
    child: Child,
    stdin: ChildStdin,
    lines: Receiver<String>,
    /// Constants declared by each loaded live.py, by adapter.
    pub constants: BTreeMap<String, AdapterConsts>,
    /// Modules whose live.py failed to load, with the reason.
    pub load_errors: Vec<String>,
}

/// The modules of a Build that ship `adapters/<module>/live.py`.
pub fn live_modules(adapters_dir: &Path) -> BTreeSet<String> {
    let mut out = BTreeSet::new();
    if let Ok(rd) = std::fs::read_dir(adapters_dir) {
        for e in rd.flatten() {
            if e.path().join("live.py").is_file() {
                if let Some(n) = e.file_name().to_str() {
                    out.insert(n.to_string());
                }
            }
        }
    }
    out
}

/// How long the worker may take to start and load the Build's adapter code.
const INIT_BUDGET: Duration = Duration::from_secs(20);

impl AdapterCode {
    /// Starts the worker over the Build's `adapters/` directory and loads `modules`.
    pub fn spawn(
        adapters_dir: &Path,
        modules: &[String],
        adapters: &[String],
    ) -> Result<AdapterCode, String> {
        let mut child =
            crate::sandbox::spawn_worker(Some(adapters_dir), WORKER).map_err(|e| e.to_string())?;
        let stdin = child
            .stdin
            .take()
            .ok_or("adapter-code worker has no stdin")?;
        let stdout = child
            .stdout
            .take()
            .ok_or("adapter-code worker has no stdout")?;
        let (tx, rx) = mpsc::channel();
        std::thread::spawn(move || {
            let mut r = BufReader::new(stdout);
            loop {
                let mut line = String::new();
                match r.read_line(&mut line) {
                    Ok(0) | Err(_) => break,
                    Ok(_) => {
                        if tx.send(line).is_err() {
                            break;
                        }
                    }
                }
            }
        });
        let mut w = AdapterCode {
            child,
            stdin,
            lines: rx,
            constants: BTreeMap::new(),
            load_errors: Vec::new(),
        };
        let r = w.request(
            &json!({"op": "init", "modules": modules, "adapters": adapters}),
            INIT_BUDGET,
            &mut |_| json!({"code": "UNAVAILABLE"}),
        )?;
        if let Some(c) = r.get("constants").and_then(Value::as_object) {
            for (a, v) in c {
                w.constants.insert(a.clone(), AdapterConsts::from_json(v));
            }
        }
        w.load_errors = r
            .get("errors")
            .and_then(Value::as_array)
            .map(|a| {
                a.iter()
                    .filter_map(|e| e.as_str().map(str::to_string))
                    .collect()
            })
            .unwrap_or_default();
        Ok(w)
    }

    fn write(&mut self, v: &Value) -> Result<(), String> {
        let mut line = serde_json::to_string(v).map_err(|e| e.to_string())?;
        line.push('\n');
        self.stdin
            .write_all(line.as_bytes())
            .and_then(|_| self.stdin.flush())
            .map_err(|e| format!("adapter-code worker: {e}"))
    }

    /// One request; `send` callbacks are answered by `on_send` until the final answer. The
    /// worker's own time (not the time spent in `on_send`) is bounded by `budget`; past it the
    /// worker is killed and the error says so.
    pub fn request(
        &mut self,
        req: &Value,
        budget: Duration,
        on_send: &mut dyn FnMut(&Value) -> Value,
    ) -> Result<Value, String> {
        self.write(req)?;
        let mut left = budget;
        loop {
            let started = std::time::Instant::now();
            let line = match self.lines.recv_timeout(left) {
                Ok(l) => l,
                Err(RecvTimeoutError::Timeout) => {
                    let _ = self.child.kill();
                    return Err("timeout: the adapter code did not finish within its budget".into());
                }
                Err(RecvTimeoutError::Disconnected) => {
                    return Err("the adapter-code worker exited unexpectedly".into())
                }
            };
            left = left.saturating_sub(started.elapsed());
            let v: Value = serde_json::from_str(&line)
                .map_err(|e| format!("adapter-code worker sent malformed output: {e}"))?;
            if v.get("cb").is_some() {
                let reply = on_send(&v);
                self.write(&reply)?;
                continue;
            }
            if let Some(f) = v.get("fatal").and_then(Value::as_str) {
                return Err(f.to_string());
            }
            return Ok(v);
        }
    }
}

impl Drop for AdapterCode {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::net::TcpListener;

    #[test]
    fn endpoint_resolution_is_restricted() {
        let mut stubs = BTreeMap::new();
        stubs.insert("search".to_string(), "http://127.0.0.1:8001".to_string());
        assert_eq!(
            resolve("stub://search/v1/query", &stubs).unwrap().url(),
            "http://127.0.0.1:8001/v1/query"
        );
        assert!(
            resolve("stub://publish/v1/post", &stubs).is_err(),
            "an unmapped stub server is refused"
        );
        assert!(
            resolve("https://example.invalid/x", &stubs).is_err(),
            "only http and stub schemes"
        );
        assert!(resolve("file:///etc/passwd", &stubs).is_err());
        assert!(resolve("http://user@host/x", &stubs).is_err());
        assert_eq!(
            resolve("http://localhost:9/p", &stubs).unwrap(),
            Target {
                host: "localhost".into(),
                port: 9,
                path: "/p".into()
            }
        );
    }

    #[test]
    fn response_parsing() {
        let r = parse_response(b"HTTP/1.0 429 Too Many\r\nContent-Type: application/json\r\nX-CWR-Retry-After-Ticks: 5\r\nServer: x\r\nContent-Length: 2\r\n\r\n{}", false).unwrap().unwrap();
        match r {
            RawResponse::Http {
                status,
                headers,
                body,
            } => {
                assert_eq!(status, 429);
                assert_eq!(body, "{}");
                assert_eq!(headers.len(), 2);
                assert_eq!(headers["x-cwr-retry-after-ticks"], "5");
            }
            other => panic!("{other:?}"),
        }
        assert!(
            parse_response(b"HTTP/1.0 200 OK\r\nContent-Length: 10\r\n\r\n{}", false).is_none()
        );
    }

    #[test]
    fn post_times_out_on_a_silent_server_and_reports_refusal() {
        let l = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = l.local_addr().unwrap().port();
        let hold = std::thread::spawn(move || {
            let (s, _) = l.accept().unwrap();
            std::thread::sleep(Duration::from_millis(600));
            drop(s);
        });
        let t = Target {
            host: "127.0.0.1".into(),
            port,
            path: "/x".into(),
        };
        let started = std::time::Instant::now();
        let r = post(&t, b"{}", &[], &Deadline::after_ms(200));
        let took = started.elapsed();
        assert_eq!(r, RawResponse::Timeout);
        assert!(
            took >= Duration::from_millis(200) && took < Duration::from_millis(550),
            "{took:?}"
        );
        hold.join().unwrap();
        // Nothing listens any more: refused, reported once, never retried.
        let r = post(&t, b"{}", &[], &Deadline::after_ms(500));
        assert!(matches!(r, RawResponse::TransportError { .. }), "{r:?}");
    }
}
