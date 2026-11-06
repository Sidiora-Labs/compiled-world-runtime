// Copyright Sidiora Labs Inc. CWR® 2026-2027
// SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0

//! One turn: a script in a fresh sandbox, every primitive through the atomic contract check, the
//! result envelope (kernel.md sections 3 to 5, model-surface.md section 4; clauses 1.1, 1.2, 6.1
//! to 6.5).
//!
//! [`run_turn`] verifies that the Instance is not sealed, loads the Build's contract table,
//! checks that the sandbox interpreter provides the Build's runtime (Python 3.12), starts the
//! world-code worker and the script sandbox, enters the turn (`idle` to `in_turn`), then serves
//! the RPC channel until the script ends or a limit ends it:
//!
//! * a primitive request goes through [`Checker::call`] and is answered only after its ledger
//!   record is durable;
//! * a closure refusal from the runner is recorded as `INVALID` under its `sandbox.*` facility;
//! * `wall_ms` is a host timer on the turn, `cpu_ms` and `memory_mb` are sampled from the process
//!   tree (with rlimit backstops in [`crate::sandbox`]); exceeding one kills the process and the
//!   turn ends with `LIMIT` naming it;
//! * a defect that seals the Instance (`INVARIANT_VIOLATION` at step 3 of a mutating primitive or at
//!   step 4 of any) ends the turn;
//! * the turn deadline passing while a primitive runs ends the turn with `LIMIT wall_ms`.
//!
//! The turn then advances `world_time` by `tick_per_turn`, appends the `turn` record and returns
//! the envelope `{status, returned, error, trace, world, resources}` with status `ok`, `error` or
//! `partial` by the rules of kernel.md section 5. Scripts are not transactional: nothing is ever
//! rolled back; the trace lists every call in order.

use std::time::{Duration, Instant};

use cwr_contract::Code;
use serde_json::{json, Value};

use crate::contract_check::{Checker, ContractTable, Gateway, LibWorker, SEAL_FILE};
use crate::envelope::{self, parse_failure, turn_envelope, Failure, Resources};
use crate::limits::Limits;
use crate::rpc::{self, FrameReader, Poll, Request};
use crate::sandbox::{self, ScriptSandbox};
use crate::state::Instance;
use crate::{canonical_json, KernelError, Result};

pub use crate::contract_check::{GatewayOutcome, NoGateway};

/// File holding the last result envelope of an Instance (model-surface.md section 1, component 4).
pub const LAST_ENVELOPE: &str = "last_envelope.json";

/// Turn options chosen by the operator or harness (never by the model).
#[derive(Debug, Clone, Copy)]
pub struct TurnOptions {
    /// Step 4 of the atomic check: assert every declared effect after an ok mutating call.
    pub debug_effects: bool,
    /// The in-interpreter language layer of the sandbox. Always on except for the diagnostic
    /// that demonstrates the kernel-level layer alone (crates/cwr-kernel/SANDBOX.md).
    pub language_guard: bool,
}

impl Default for TurnOptions {
    fn default() -> Self {
        TurnOptions {
            debug_effects: false,
            language_guard: true,
        }
    }
}

/// Everything a turn produced: the envelope plus operator-only diagnostics that never reach the
/// model.
#[derive(Debug, Clone)]
pub struct TurnReport {
    /// The result envelope returned to the model.
    pub envelope: Value,
    /// What the sandbox process wrote to stderr (operator diagnostics only).
    pub sandbox_stderr: String,
    /// Peak resident set size of the sandbox process (operator diagnostics only).
    pub peak_rss_kb: u64,
    /// Why the script stopped, in operator terms.
    pub ended: String,
}

#[derive(Debug)]
enum End {
    Script {
        outcome: String,
        returned: Value,
        failure: Value,
        exception: Option<String>,
    },
    Limit(&'static str),
    Sealed,
    Exited(String),
    Broken(String),
}

/// The refusal envelope of a turn requested while the Instance is busy (another operation holds
/// its lock) or not `idle` (kernel.md section 2). Nothing executed and nothing was recorded: the
/// status is `error`, the error is `CONFLICT` naming [`envelope::KERNEL_INSTANCE`], the trace is
/// empty and resource use is zero. `meta` (read without the lock) fills `world` with the
/// Instance's last durable position when it is readable; otherwise `world` is null.
pub fn conflict_envelope(meta: Option<&crate::state::InstanceMeta>, detail: &str) -> Value {
    let failure = Failure::new(Code::Conflict, envelope::KERNEL_INSTANCE, detail);
    let world = meta.map_or(Value::Null, |m| {
        json!({"version": m.world_version, "build": m.build, "time": m.world_time, "turn": m.turn})
    });
    turn_envelope(
        "error",
        Value::Null,
        Some(&failure),
        Vec::new(),
        world,
        &Resources::default(),
    )
}

/// Whether an Instance is sealed by a defect, and why.
pub fn sealed(inst: &Instance) -> Option<Value> {
    std::fs::read(inst.dir.join(SEAL_FILE))
        .ok()
        .map(|b| serde_json::from_slice(&b).unwrap_or(json!({"defect": "unreadable seal"})))
}

fn respond(
    sock: &mut std::os::unix::net::UnixStream,
    id: Value,
    result: Value,
    deadline: Instant,
) -> bool {
    let left = deadline
        .saturating_duration_since(Instant::now())
        .max(Duration::from_millis(10));
    let _ = sock.set_write_timeout(Some(left));
    rpc::write_frame(
        sock,
        &json!({"v": rpc::PROTOCOL, "id": id, "result": result}),
    )
    .is_ok()
}

/// The returned value, the failure and the status of a turn that ended as `ended`, by the rules of
/// kernel.md section 5: the status is `ok` without a failure, `partial` with a failure after a
/// mutating or external call succeeded (`mutated`) and `error` otherwise. This holds for every
/// failure, including a top-level return value the SDK codec cannot encode (decisions.md C01).
/// Only a failure the kernel returned this turn (`failures`) is reported as the world's failure.
fn settle(
    ended: End,
    mutated: bool,
    failures: &[Failure],
    sealed: Option<Failure>,
    limits: &Limits,
    cpu_ms: u64,
) -> (Value, Option<Failure>, &'static str) {
    let (returned, error): (Value, Option<Failure>) = match ended {
        End::Script {
            outcome, returned, ..
        } if outcome == "ok" => (returned, None),
        End::Script {
            outcome, exception, ..
        } if outcome == "unencodable" => {
            // kernel.md section 5 and decisions.md C01: INVALID on turn.return after every effect
            // was applied; the status follows the ordinary rule, so a turn whose mutating or
            // external call succeeded is `partial`, never `error`.
            (
                Value::Null,
                Some(Failure::new(
                    Code::Invalid,
                    envelope::TURN_RETURN,
                    exception.unwrap_or_else(|| "returned value is outside the SDK codec".into()),
                )),
            )
        }
        End::Script {
            failure, exception, ..
        } => {
            // Only a failure the kernel actually returned this turn is reported as the world's
            // failure; anything else the script raised is the script's own exception.
            let world = parse_failure(&failure).filter(|f| failures.contains(f));
            let f = world.unwrap_or_else(|| {
                Failure::new(
                    Code::Invalid,
                    envelope::TURN_SCRIPT,
                    format!(
                        "uncaught exception: {}",
                        exception.unwrap_or_else(|| "unknown".into())
                    ),
                )
            });
            (Value::Null, Some(f))
        }
        End::Limit(name) => {
            let detail = match name {
                "cpu_ms" => format!(
                    "the script used more than {} ms of CPU time and was terminated",
                    limits.cpu_ms
                ),
                "wall_ms" => format!(
                    "the turn exceeded {} ms of wall time and the script was terminated",
                    limits.wall_ms
                ),
                _ => format!(
                    "the script exceeded {} MiB of memory and was terminated",
                    limits.memory_mb
                ),
            };
            (
                Value::Null,
                Some(Failure::limit(envelope::SANDBOX_PROCESS, name, detail)),
            )
        }
        End::Sealed => (Value::Null, sealed),
        End::Exited(why) => {
            if cpu_ms > limits.cpu_ms {
                (
                    Value::Null,
                    Some(Failure::limit(
                        envelope::SANDBOX_PROCESS,
                        "cpu_ms",
                        format!(
                            "the script used more than {} ms of CPU time and was terminated",
                            limits.cpu_ms
                        ),
                    )),
                )
            } else {
                (
                    Value::Null,
                    Some(Failure::new(Code::Invalid, envelope::TURN_SCRIPT, why)),
                )
            }
        }
        End::Broken(why) => (
            Value::Null,
            Some(Failure::new(
                Code::Invalid,
                "sandbox.rpc",
                format!("the sandbox broke the RPC framing: {why}"),
            )),
        ),
    };
    let status = envelope::turn_status(error.is_some(), mutated);
    (returned, error, status)
}

/// A fresh 128-bit token, in hex, that authenticates the runner's `limit` report.
///
/// Only the runner's memory monitor holds it, so a script that writes raw frames on the RPC
/// channel cannot forge `LIMIT memory_mb`. It is host randomness by design and never enters the
/// ledger, the envelope or any state, so it does not touch determinism.
fn runner_token() -> String {
    use std::hash::{BuildHasher, Hasher};
    use std::io::Read;
    let mut bytes = [0u8; 16];
    let read = std::fs::File::open("/dev/urandom").and_then(|mut f| f.read_exact(&mut bytes));
    if read.is_err() {
        for (i, chunk) in bytes.chunks_mut(8).enumerate() {
            let mut h = std::collections::hash_map::RandomState::new().build_hasher();
            h.write_usize(i);
            chunk.copy_from_slice(&h.finish().to_le_bytes());
        }
    }
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

/// Runs one turn of `script` on `inst`.
pub fn run_turn(
    inst: &mut Instance,
    script: &str,
    opts: &TurnOptions,
    gateway: &mut dyn Gateway,
) -> Result<TurnReport> {
    if let Some(s) = sealed(inst) {
        return Err(KernelError::State(format!(
            "instance {} is sealed for inspection after INVARIANT_VIOLATION: {}",
            inst.meta.id,
            canonical_json(&s)
        )));
    }
    if inst.meta.status != "idle" {
        return Err(KernelError::Conflict(format!(
            "instance is {}, not idle",
            inst.meta.status
        )));
    }
    let mut table = ContractTable::load(&inst.build)?;
    let drift = crate::drift_injector::prepare(&inst.build, &mut table, inst.meta.turn + 1)?;
    sandbox::check_runtime(&table.runtime)?;
    sandbox::bwrap()?;
    let limits = Limits::of(&inst.build)?;
    let mut worker = LibWorker::spawn(&inst.build, &table, &inst.state, inst.meta.seed)?;
    let mut sb = ScriptSandbox::spawn(&inst.build.dir.join("sdk"), &limits)?;
    if !opts.language_guard {
        inst.mark_language_guard_off();
    }
    let turn = inst.begin_turn()?;
    drift.record(inst, turn)?;
    let deadline = sb.started + Duration::from_millis(limits.wall_ms);
    let mut checker = Checker::new(
        inst,
        &mut worker,
        &table,
        gateway,
        limits,
        opts.debug_effects,
        turn,
    );
    checker.set_deadline(deadline);

    let token = runner_token();
    let start = json!({"v": rpc::PROTOCOL, "op": "start", "script": script, "modules": table.modules, "guard": opts.language_guard, "token": token});
    let _ = sb
        .sock
        .set_write_timeout(Some(Duration::from_millis(limits.wall_ms)));
    let accepted = rpc::write_frame(&mut sb.sock, &start);
    let ended = if let Err(e) = accepted {
        End::Exited(format!("the sandbox did not accept the script: {e}"))
    } else {
        let mut reader = FrameReader::new();
        let mut last_sample = Instant::now() - Duration::from_secs(1);
        let mut run = || -> Result<End> {
            loop {
                let now = Instant::now();
                if now >= deadline {
                    return Ok(End::Limit("wall_ms"));
                }
                if now.duration_since(last_sample) >= Duration::from_millis(10) {
                    last_sample = now;
                    let (cpu, rss_kb) = sb.sample();
                    if cpu > limits.cpu_ms {
                        return Ok(End::Limit("cpu_ms"));
                    }
                    if rss_kb > limits.memory_mb * 1024 {
                        return Ok(End::Limit("memory_mb"));
                    }
                }
                match reader.poll(&mut sb.sock) {
                    Poll::Frame(v) => match Request::parse(&v) {
                        Request::Call {
                            id,
                            primitive,
                            params,
                        } => {
                            let reply = checker.call(&primitive, &params)?;
                            if reply.wall_expired {
                                return Ok(End::Limit("wall_ms"));
                            }
                            if reply.seal {
                                let _ = respond(&mut sb.sock, id, reply.result, deadline);
                                return Ok(End::Sealed);
                            }
                            if !respond(&mut sb.sock, id, reply.result, deadline) {
                                return Ok(End::Exited(
                                    "the sandbox stopped reading the RPC channel".into(),
                                ));
                            }
                        }
                        Request::Refuse {
                            id,
                            facility,
                            detail,
                        } => {
                            let reply = checker.closure(&facility, &detail)?;
                            if !respond(&mut sb.sock, id, reply.result, deadline) {
                                return Ok(End::Exited(
                                    "the sandbox stopped reading the RPC channel".into(),
                                ));
                            }
                        }
                        Request::Limit { limit, token: t }
                            if limit == "memory_mb" && t.as_deref() == Some(token.as_str()) =>
                        {
                            return Ok(End::Limit("memory_mb"))
                        }
                        Request::Limit { token: t, .. } if t.as_deref() != Some(token.as_str()) => {
                            return Ok(End::Broken(
                                "a limit report without the runner's token (written by the script)"
                                    .into(),
                            ))
                        }
                        Request::Limit { limit, .. } => {
                            return Ok(End::Broken(format!("unknown limit report {limit:?}")))
                        }
                        Request::End {
                            outcome,
                            returned,
                            failure,
                            exception,
                        } => {
                            return Ok(End::Script {
                                outcome,
                                returned,
                                failure,
                                exception,
                            })
                        }
                        Request::Malformed { id: Some(id), why } => {
                            let reply = checker.closure("sandbox.rpc", &why)?;
                            if !respond(&mut sb.sock, id, reply.result, deadline) {
                                return Ok(End::Exited(
                                    "the sandbox stopped reading the RPC channel".into(),
                                ));
                            }
                        }
                        Request::Malformed { id: None, why } => return Ok(End::Broken(why)),
                    },
                    Poll::Pending => {
                        if let Some(st) = sb.try_wait() {
                            return Ok(End::Exited(format!(
                                "the sandbox process ended without a result ({st})"
                            )));
                        }
                    }
                    Poll::Closed => {
                        let until = Instant::now() + Duration::from_millis(200);
                        while sb.try_wait().is_none() && Instant::now() < until {
                            std::thread::sleep(Duration::from_millis(5));
                        }
                        let st = sb
                            .try_wait()
                            .map(|s| s.to_string())
                            .unwrap_or_else(|| "still running".into());
                        return Ok(End::Exited(format!(
                            "the sandbox closed the RPC channel without a result ({st})"
                        )));
                    }
                    Poll::Broken(why) => return Ok(End::Broken(why)),
                }
            }
        };
        run()?
    };
    let peak_rss_kb = sb.peak_rss_kb();
    let wall_ms = sb.started.elapsed().as_millis() as u64;
    let (_status, cpu_ms, stderr) = sb.finish();
    let ended_text = format!("{ended:?}");

    let (returned, error, status) = settle(
        ended,
        checker.mutated,
        &checker.failures,
        checker.sealed.clone(),
        &limits,
        cpu_ms,
    );
    let resources = Resources {
        cpu_ms,
        wall_ms,
        primitive_calls: checker.budget.primitive_calls,
        adapter_calls: checker.budget.adapter_calls,
        remaining_primitive_calls: checker.budget.remaining_primitive_calls(&limits),
        remaining_adapter_calls: checker.budget.remaining_adapter_calls(&limits),
    };
    let trace = std::mem::take(&mut checker.trace);
    let calls = checker.budget.primitive_calls;
    drop(checker);
    drop(worker);
    let rec = inst.end_turn(
        status,
        error.as_ref().map(|f| f.code),
        calls,
        &returned,
        resources.ledger_json(),
    )?;
    let world = json!({"version": inst.build.version, "build": inst.build.hash, "time": rec["world_time"], "turn": rec["turn"]});
    let env = turn_envelope(status, returned, error.as_ref(), trace, world, &resources);
    crate::surface::envelope_render::store(&inst.dir, &rec, &env)?;

    Ok(TurnReport {
        envelope: env,
        sandbox_stderr: stderr,
        peak_rss_kb,
        ended: ended_text,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn runner_tokens_are_fresh_128_bit_hex() {
        let (a, b) = (runner_token(), runner_token());
        assert_eq!(a.len(), 32);
        assert!(a.bytes().all(|c| c.is_ascii_hexdigit()));
        assert_ne!(a, b);
    }

    const LIMITS: Limits = Limits {
        cpu_ms: 5000,
        wall_ms: 30000,
        memory_mb: 256,
        primitive_calls: 500,
        adapter_calls: 50,
    };

    fn unencodable() -> End {
        End::Script {
            outcome: "unencodable".into(),
            returned: Value::Null,
            failure: Value::Null,
            exception: Some("set is outside the SDK codec".into()),
        }
    }

    #[test]
    fn unencodable_return_after_a_mutation_is_partial() {
        let (returned, error, status) = settle(unencodable(), true, &[], None, &LIMITS, 0);
        assert_eq!(status, "partial");
        assert_eq!(returned, Value::Null);
        let f = error.expect("an unencodable return is a failure");
        assert_eq!(
            (f.code, f.primitive.as_str()),
            (Code::Invalid, envelope::TURN_RETURN)
        );
    }

    #[test]
    fn unencodable_return_with_nothing_changed_is_error() {
        let (_, error, status) = settle(unencodable(), false, &[], None, &LIMITS, 0);
        assert_eq!(status, "error");
        assert_eq!(error.map(|f| f.code), Some(Code::Invalid));
    }

    #[test]
    fn ok_script_is_ok_whatever_it_changed() {
        let end = End::Script {
            outcome: "ok".into(),
            returned: json!(3),
            failure: Value::Null,
            exception: None,
        };
        let (returned, error, status) = settle(end, true, &[], None, &LIMITS, 0);
        assert_eq!((returned, error, status), (json!(3), None, "ok"));
    }

    #[test]
    fn conflict_envelope_is_the_standard_shape() {
        let env = conflict_envelope(None, "instance is in_turn, not idle");
        assert_eq!(env["status"], "error");
        assert_eq!(env["error"]["code"], "CONFLICT");
        assert_eq!(env["error"]["primitive"], envelope::KERNEL_INSTANCE);
        assert_eq!(env["trace"], json!([]));
        assert_eq!(env["resources"]["primitive_calls"], 0);
        assert_eq!(env["resources"]["cpu_ms"], 0);
        let mut keys: Vec<&str> = env
            .as_object()
            .map(|m| m.keys().map(String::as_str).collect())
            .unwrap_or_default();
        keys.sort_unstable();
        assert_eq!(
            keys,
            ["error", "resources", "returned", "status", "trace", "world"]
        );
    }
}
