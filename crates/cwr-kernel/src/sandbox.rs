// Copyright Sidiora Labs Inc. CWR® 2026-2027
// SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0

//! The per-turn sandbox (kernel.md section 3; clause 6.1; decisions.md P04 and P13).
//!
//! Mechanism on this host (pinned and reported, decisions.md "Decisions required during
//! implementation"; the full guarantee list is crates/cwr-kernel/SANDBOX.md):
//!
//! * **Process and namespaces**: a fresh `bwrap` (bubblewrap) process per turn with every
//!   namespace unshared (user, mount, network, pid, ipc, uts, cgroup), uid/gid 65534 inside, all
//!   capabilities dropped, nested user namespaces disabled, a new session and death with the
//!   kernel. The network namespace is empty: there is no route to any host, not even loopback.
//! * **Filesystem view**: an empty read-only root holding only the interpreter (its executable,
//!   its stdlib and the shared objects it maps), the Build's generated SDK at `/cwr/sdk`
//!   read-only, and a private size-capped tmpfs scratch directory at `/scratch` (the working
//!   directory) that vanishes with the process. No `/proc`, no `/dev`, no host path.
//! * **Environment**: cleared; only `PYTHONHASHSEED=0`, `PYTHONUTF8=1` and `CWR_RPC_FD=3` are
//!   set. The only inherited descriptor besides stdio (null, null, a capture pipe) is the RPC
//!   socket at fd 3. Address-space randomisation is disabled so object identities do not become
//!   a host random source.
//! * **One process**: bubblewrap installs a seccomp filter ([`no_fork_filter`]) on the interpreter
//!   just before it executes: `fork`, `vfork` and every `clone` without `CLONE_THREAD` fail with
//!   `EPERM` and `clone3` with `ENOSYS` (so threads fall back to `clone`). The script is therefore
//!   one process (threads allowed), whatever the in-interpreter layer misses, and the per-process
//!   limits below bound the whole sandbox.
//! * **Resource limits**: `RLIMIT_AS = memory_mb` (address space, which bounds resident memory),
//!   `RLIMIT_CPU` as a hard backstop just above `cpu_ms`, `RLIMIT_FSIZE`, `RLIMIT_NOFILE` and
//!   `RLIMIT_CORE = 0`; the kernel also polls the process tree's CPU time and resident memory
//!   and kills it the moment `cpu_ms` or `memory_mb` is exceeded, and kills it at `wall_ms`. The
//!   turn's CPU time is that tree's own: the sandbox child is reaped with `wait4`, never measured
//!   through `RUSAGE_CHILDREN` of the whole kernel process.
//! * **Language layer**: `sdk/python/cwr/_runner.py`, embedded here, guards imports, files,
//!   sockets, processes, the clock and frame introspection from inside the interpreter.
//!
//! Without bubblewrap the kernel refuses to run a turn (fail closed); it never runs a script
//! unsandboxed. The kernel-side world-code worker ([`crate::contract_check::LibWorker`]) runs in
//! the same kind of view with the Build's `lib/` instead of the SDK and no scratch directory, so
//! `lib/` has no network capability either (clause 7.1). Every worker ([`spawn_worker`]: world
//! code, adapter code, migrations) runs under [`WORKER_MEMORY_MB`] of address space and
//! [`WORKER_CPU_SECONDS`] of CPU time. The one-process filter applies to the script sandbox only:
//! worker code is the Build's own, not the model's.

use std::fs;
use std::io::Read;
use std::os::unix::io::AsRawFd;
use std::os::unix::net::UnixStream;
use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, ExitStatus, Stdio};
use std::sync::{Arc, Mutex, OnceLock, PoisonError};
use std::time::{Duration, Instant};

use crate::limits::Limits;
use crate::{KernelError, Result};

/// The runner, embedded byte-for-byte.
pub const RUNNER: &str = include_str!("../../../sdk/python/cwr/_runner.py");

/// Mount point of the Build SDK inside the script sandbox.
pub const SDK_MOUNT: &str = "/cwr/sdk";
/// Mount point of the Build `lib/` inside the world-code worker.
pub const LIB_MOUNT: &str = "/build/lib";
/// The scratch directory inside the script sandbox.
pub const SCRATCH: &str = "/scratch";
/// The RPC descriptor number inside the sandbox.
pub const RPC_FD: i32 = 3;

// ---------------------------------------------------------------------------------------------
// libc (std links it already; no extra crate)
// ---------------------------------------------------------------------------------------------

#[repr(C)]
struct RLimit {
    cur: u64,
    max: u64,
}

#[repr(C)]
struct TimeVal {
    sec: i64,
    usec: i64,
}

#[repr(C)]
struct RUsage {
    utime: TimeVal,
    stime: TimeVal,
    rest: [i64; 14],
}

extern "C" {
    fn setrlimit(resource: i32, rlim: *const RLimit) -> i32;
    fn wait4(pid: i32, status: *mut i32, options: i32, usage: *mut RUsage) -> i32;
    fn kill(pid: i32, sig: i32) -> i32;
    fn personality(persona: u64) -> i32;
    fn dup2(oldfd: i32, newfd: i32) -> i32;
    fn fcntl(fd: i32, cmd: i32, ...) -> i32;
    fn sysconf(name: i32) -> i64;
    fn pipe2(fds: *mut i32, flags: i32) -> i32;
}

const RLIMIT_CPU: i32 = 0;
const RLIMIT_FSIZE: i32 = 1;
const RLIMIT_CORE: i32 = 4;
const RLIMIT_NOFILE: i32 = 7;
const RLIMIT_AS: i32 = 9;
const ADDR_NO_RANDOMIZE: u64 = 0x0040000;
const F_DUPFD_CLOEXEC: i32 = 1030;
const O_CLOEXEC: i32 = 0o2000000;
const SC_CLK_TCK: i32 = 2;
const WNOHANG: i32 = 1;
const SIGKILL: i32 = 9;
const EINTR: i32 = 4;

/// The descriptor number bubblewrap reads the one-process seccomp program from.
pub const SECCOMP_FD: i32 = 4;
/// Address-space cap of every worker process (world code, adapter code, migrations), in MiB.
pub const WORKER_MEMORY_MB: u64 = 2048;
/// CPU-time cap of every worker process, in seconds. A worker lives for one turn or one upgrade;
/// the turn's `wall_ms` deadline ends a world-code exchange long before this backstop.
pub const WORKER_CPU_SECONDS: u64 = 300;

fn clk_tck() -> u64 {
    // SAFETY: sysconf has no preconditions.
    let t = unsafe { sysconf(SC_CLK_TCK) };
    if t > 0 {
        t as u64
    } else {
        100
    }
}

impl RUsage {
    fn zero() -> RUsage {
        RUsage {
            utime: TimeVal { sec: 0, usec: 0 },
            stime: TimeVal { sec: 0, usec: 0 },
            rest: [0; 14],
        }
    }

    /// User plus system time, in milliseconds.
    fn cpu_ms(&self) -> u64 {
        let ms = |t: &TimeVal| (t.sec.max(0) as u64) * 1000 + (t.usec.max(0) as u64) / 1000;
        ms(&self.utime) + ms(&self.stime)
    }
}

/// Reaps `pid` with `wait4`: `Some((status, cpu_ms))` once it has exited, where `cpu_ms` is the
/// CPU time of that process and the descendants it waited for, and nothing else; `None` while it
/// still runs (only with `nohang`) or when it cannot be waited for.
fn reap(pid: i32, nohang: bool) -> Option<(ExitStatus, u64)> {
    use std::os::unix::process::ExitStatusExt;
    loop {
        let mut status = 0i32;
        let mut u = RUsage::zero();
        // SAFETY: status and u are valid, writable buffers; pid is a child of this process that
        // nothing else waits for.
        let r = unsafe { wait4(pid, &mut status, if nohang { WNOHANG } else { 0 }, &mut u) };
        if r == pid {
            return Some((ExitStatus::from_raw(status), u.cpu_ms()));
        }
        if r < 0 && std::io::Error::last_os_error().raw_os_error() == Some(EINTR) {
            continue;
        }
        return None;
    }
}

// ---------------------------------------------------------------------------------------------
// The one-process seccomp program
// ---------------------------------------------------------------------------------------------

const BPF_LD_W_ABS: u16 = 0x20;
const BPF_JEQ_K: u16 = 0x15;
const BPF_JGE_K: u16 = 0x35;
const BPF_JSET_K: u16 = 0x45;
const BPF_RET_K: u16 = 0x06;
const SECCOMP_RET_ALLOW: u32 = 0x7fff_0000;
const SECCOMP_RET_EPERM: u32 = 0x0005_0000 | 1;
const SECCOMP_RET_ENOSYS: u32 = 0x0005_0000 | 38;
const CLONE_THREAD: u32 = 0x0001_0000;

/// (audit arch, clone, clone3, fork, vfork) for this target; aarch64 has no fork or vfork, so
/// clone stands in for both.
#[cfg(target_arch = "x86_64")]
const FORK_NRS: Option<(u32, u32, u32, u32, u32)> = Some((0xC000_003E, 56, 435, 57, 58));
#[cfg(target_arch = "aarch64")]
const FORK_NRS: Option<(u32, u32, u32, u32, u32)> = Some((0xC000_00B7, 220, 435, 220, 220));
#[cfg(not(any(target_arch = "x86_64", target_arch = "aarch64")))]
const FORK_NRS: Option<(u32, u32, u32, u32, u32)> = None;

/// The one-process seccomp program as the bytes of a `struct sock_filter` array, the form
/// bubblewrap's `--seccomp` reads: a foreign syscall ABI, `fork`, `vfork` and `clone` without
/// `CLONE_THREAD` fail with `EPERM`; `clone3` fails with `ENOSYS`, so the C library creates
/// threads with `clone`; everything else is allowed. `None` on a target without a program, where
/// the kernel refuses to start a sandbox (fail closed). `seccomp_data` is
/// `{nr: u32, arch: u32, ip: u64, args: [u64; 6]}`; the low word of `args[0]` is at offset 16.
pub fn no_fork_filter() -> Option<Vec<u8>> {
    let (arch, clone, clone3, fork, vfork) = FORK_NRS?;
    let prog: [(u16, u8, u8, u32); 13] = [
        (BPF_LD_W_ABS, 0, 0, 4),               // 0: A = arch
        (BPF_JEQ_K, 1, 0, arch),               // 1: native ABI -> 3
        (BPF_RET_K, 0, 0, SECCOMP_RET_EPERM),  // 2
        (BPF_LD_W_ABS, 0, 0, 0),               // 3: A = nr
        (BPF_JGE_K, 8, 0, 0x4000_0000),        // 4: x32 ABI -> 13 (EPERM)
        (BPF_JEQ_K, 6, 0, clone3),             // 5: -> 12 (ENOSYS)
        (BPF_JEQ_K, 6, 0, fork),               // 6: -> 13 (EPERM)
        (BPF_JEQ_K, 5, 0, vfork),              // 7: -> 13 (EPERM)
        (BPF_JEQ_K, 1, 0, clone),              // 8: -> 10
        (BPF_RET_K, 0, 0, SECCOMP_RET_ALLOW),  // 9
        (BPF_LD_W_ABS, 0, 0, 16),              // 10: A = low word of clone flags
        (BPF_JSET_K, 2, 1, CLONE_THREAD),      // 11: thread -> 14 (ALLOW), else -> 13 (EPERM)
        (BPF_RET_K, 0, 0, SECCOMP_RET_ENOSYS), // 12
    ];
    let tail: [(u16, u8, u8, u32); 2] = [
        (BPF_RET_K, 0, 0, SECCOMP_RET_EPERM), // 13
        (BPF_RET_K, 0, 0, SECCOMP_RET_ALLOW), // 14
    ];
    let mut out = Vec::with_capacity((prog.len() + tail.len()) * 8);
    for (code, jt, jf, k) in prog.iter().chain(tail.iter()) {
        out.extend_from_slice(&code.to_ne_bytes());
        out.push(*jt);
        out.push(*jf);
        out.extend_from_slice(&k.to_ne_bytes());
    }
    Some(out)
}

/// A pipe whose read end holds the one-process seccomp program, for bubblewrap's `--seccomp`.
/// Both ends are close-on-exec; [`apply_limits`] places the read end at [`SECCOMP_FD`].
fn seccomp_pipe() -> Result<std::os::fd::OwnedFd> {
    use std::os::fd::FromRawFd;
    let prog = no_fork_filter().ok_or_else(|| {
        KernelError::Refused(format!(
            "sandbox unavailable: no one-process seccomp program for target architecture {}",
            std::env::consts::ARCH
        ))
    })?;
    let mut fds = [0i32; 2];
    // SAFETY: fds is a valid two-element buffer.
    if unsafe { pipe2(fds.as_mut_ptr(), O_CLOEXEC) } != 0 {
        return Err(KernelError::Io(format!(
            "seccomp pipe: {}",
            std::io::Error::last_os_error()
        )));
    }
    // SAFETY: pipe2 returned two fresh descriptors that this process now owns.
    let (read, write) = unsafe {
        (
            std::os::fd::OwnedFd::from_raw_fd(fds[0]),
            std::os::fd::OwnedFd::from_raw_fd(fds[1]),
        )
    };
    let mut w = fs::File::from(write);
    std::io::Write::write_all(&mut w, &prog)
        .map_err(|e| KernelError::Io(format!("seccomp pipe: {e}")))?;
    drop(w);
    Ok(read)
}

// ---------------------------------------------------------------------------------------------
// Interpreter and bubblewrap discovery
// ---------------------------------------------------------------------------------------------

/// The interpreter as probed on this host.
#[derive(Debug, Clone)]
pub struct Interpreter {
    /// Absolute path of the interpreter binary.
    pub executable: PathBuf,
    /// Interpreter version as reported by the binary.
    pub version: String,
    /// Host paths bound read-only at the same path inside every view.
    pub binds: Vec<PathBuf>,
    /// Top-level symbolic links replicated inside the view: (target, link path).
    pub symlinks: Vec<(String, String)>,
}

/// Modules imported by the probe: everything the runner and the world-code worker load before
/// their guards are armed, so the shared objects they map are inside the view.
const PROBE: &str = r#"
import sys, os, json, struct, socket, ast, base64, binascii, math, copy, keyword, typing, types, re, signal
import collections, itertools, dataclasses, hashlib, importlib.util, encodings.ascii, encodings.latin_1, _io, time
maps = set()
try:
    with open("/proc/self/maps") as f:
        for line in f:
            parts = line.split(None, 5)
            if len(parts) == 6 and parts[5].startswith("/"):
                maps.add(parts[5].strip())
except OSError:
    pass
print(json.dumps({"version": "%d.%d.%d" % sys.version_info[:3], "executable": os.path.realpath(sys.executable),
                  "stdlib": os.path.realpath(os.path.dirname(os.__file__)), "maps": sorted(maps)}))
"#;

/// The interpreter command: `CWR_PYTHON` if set, else `python3.12`.
pub fn python_command() -> String {
    std::env::var("CWR_PYTHON")
        .ok()
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| "python3.12".to_string())
}

/// Resolves a command name against PATH (an absolute or relative path is taken as is).
fn which(cmd: &str) -> Option<PathBuf> {
    if cmd.contains('/') {
        return fs::canonicalize(cmd).ok();
    }
    std::env::var_os("PATH")
        .map(|p| std::env::split_paths(&p).collect::<Vec<_>>())
        .unwrap_or_default()
        .into_iter()
        .map(|d| d.join(cmd))
        .find(|c| c.is_file())
        .and_then(|c| fs::canonicalize(c).ok())
}

fn probe_interpreter() -> std::result::Result<Interpreter, String> {
    let name = python_command();
    let cmd =
        which(&name).ok_or_else(|| format!("the script interpreter {name} is not installed"))?;
    let out = Command::new(&cmd)
        .args(["-P", "-s", "-S", "-B", "-c", PROBE])
        .env_clear()
        .stdin(Stdio::null())
        .stderr(Stdio::null())
        .output()
        .map_err(|e| format!("cannot run the script interpreter {}: {e}", cmd.display()))?;
    if !out.status.success() {
        return Err(format!(
            "interpreter probe {} failed with {}",
            cmd.display(),
            out.status
        ));
    }
    let v: serde_json::Value = serde_json::from_slice(&out.stdout)
        .map_err(|e| format!("interpreter probe output: {e}"))?;
    let s = |k: &str| {
        v.get(k)
            .and_then(|x| x.as_str())
            .map(str::to_string)
            .ok_or_else(|| format!("interpreter probe lacks {k}"))
    };
    let executable = Some(PathBuf::from(s("executable")?))
        .filter(|p| p.is_file())
        .unwrap_or(cmd);
    let stdlib = PathBuf::from(s("stdlib")?);
    let version = s("version")?;
    let mut binds: Vec<PathBuf> = vec![executable.clone(), stdlib.clone()];
    for m in v
        .get("maps")
        .and_then(|x| x.as_array())
        .into_iter()
        .flatten()
        .filter_map(|x| x.as_str())
    {
        let p = Path::new(m);
        if p.starts_with(&stdlib) || p == executable || m.contains(" (deleted)") {
            continue;
        }
        if let Some(parent) = p.parent().and_then(|d| fs::canonicalize(d).ok()) {
            if !binds.iter().any(|b| parent.starts_with(b)) {
                binds.push(parent);
            }
        }
    }
    let mut symlinks = Vec::new();
    for top in ["/lib", "/lib64", "/lib32", "/libx32"] {
        if let Ok(target) = fs::read_link(top) {
            let t = target.to_string_lossy().into_owned();
            symlinks.push((t.clone(), top.to_string()));
            let abs = if target.is_absolute() {
                target.clone()
            } else {
                Path::new("/").join(&target)
            };
            if let Ok(abs) = fs::canonicalize(abs) {
                // A small directory such as /usr/lib64 that holds only the loader link.
                let small = fs::read_dir(&abs).map(|d| d.count() <= 8).unwrap_or(false);
                if small && !binds.iter().any(|b| abs.starts_with(b)) {
                    binds.push(abs);
                }
            }
        } else if Path::new(top).is_dir() {
            // A real directory (not a link): bind what the interpreter mapped from it only.
        }
    }
    binds.sort();
    binds.dedup();
    Ok(Interpreter {
        executable,
        version,
        binds,
        symlinks,
    })
}

/// The probed interpreter (probed once per kernel process).
pub fn interpreter() -> Result<&'static Interpreter> {
    static PROBED: OnceLock<std::result::Result<Interpreter, String>> = OnceLock::new();
    PROBED
        .get_or_init(probe_interpreter)
        .as_ref()
        .map_err(|e| KernelError::Refused(format!("sandbox unavailable: {e}")))
}

/// Checks that the interpreter provides the Build's runtime (`world.runtime`, "3.12").
pub fn check_runtime(runtime: &str) -> Result<&'static Interpreter> {
    let i = interpreter()?;
    let mm: Vec<&str> = i.version.split('.').take(2).collect();
    if mm.join(".") != runtime {
        return Err(KernelError::Refused(format!(
            "the Build requires Python {runtime}; the sandbox interpreter {} is Python {}",
            i.executable.display(),
            i.version
        )));
    }
    Ok(i)
}

/// The bubblewrap binary: `CWR_BWRAP` if set, else `bwrap` on PATH.
pub fn bwrap() -> Result<PathBuf> {
    if let Some(p) = std::env::var_os("CWR_BWRAP").filter(|s| !s.is_empty()) {
        let p = PathBuf::from(p);
        return if p.is_file() {
            Ok(p)
        } else {
            Err(KernelError::Refused(format!(
                "sandbox unavailable: CWR_BWRAP={} does not exist",
                p.display()
            )))
        };
    }
    for dir in std::env::var_os("PATH")
        .map(|p| std::env::split_paths(&p).collect::<Vec<_>>())
        .unwrap_or_default()
    {
        let c = dir.join("bwrap");
        if c.is_file() {
            return Ok(c);
        }
    }
    Err(KernelError::Refused("sandbox unavailable: bubblewrap (bwrap) is not installed; the kernel never runs a script unsandboxed".into()))
}

/// One line describing the mechanism actually in use for a turn, including whether the
/// in-interpreter language guard is on, for operators and drivers.
pub fn describe(language_guard: bool) -> String {
    let b = bwrap().map(|p| {
        let v = Command::new(&p)
            .arg("--version")
            .output()
            .ok()
            .map(|o| String::from_utf8_lossy(&o.stdout).trim().to_string())
            .unwrap_or_default();
        format!("{} ({})", p.display(), v)
    });
    let i = interpreter().map(|i| format!("{} {}", i.executable.display(), i.version));
    format!(
        "sandbox=bubblewrap bwrap={} python={} namespaces=user,mount,net,pid,ipc,uts,cgroup rlimits=AS,CPU,FSIZE,NOFILE,CORE language_guard={}",
        b.unwrap_or_else(|e| e.to_string()),
        i.unwrap_or_else(|e| e.to_string()),
        if language_guard {
            "sdk/python/cwr/_runner.py"
        } else {
            "off (operator diagnostic; every record of the turn carries language_guard=false)"
        }
    )
}

// ---------------------------------------------------------------------------------------------
// Command construction
// ---------------------------------------------------------------------------------------------

fn base_args(interp: &Interpreter) -> Vec<String> {
    let mut a: Vec<String> = [
        "--unshare-all",
        "--unshare-user",
        "--uid",
        "65534",
        "--gid",
        "65534",
        "--disable-userns",
        "--hostname",
        "cwr",
        "--die-with-parent",
        "--new-session",
        "--cap-drop",
        "ALL",
        "--clearenv",
        "--setenv",
        "PYTHONHASHSEED",
        "0",
        "--setenv",
        "PYTHONUTF8",
        "1",
    ]
    .iter()
    .map(|s| s.to_string())
    .collect();
    for b in &interp.binds {
        let s = b.display().to_string();
        a.extend(["--ro-bind".to_string(), s.clone(), s]);
    }
    for (target, link) in &interp.symlinks {
        a.extend(["--symlink".to_string(), target.clone(), link.clone()]);
    }
    a
}

/// Process limits applied in the child before `bwrap` executes (inherited by the interpreter).
#[derive(Debug, Clone, Copy)]
struct ProcLimits {
    address_space: Option<u64>,
    cpu_seconds: Option<u64>,
    file_size: Option<u64>,
    open_files: u64,
}

/// Runs in the forked child before `bwrap` executes: places the RPC socket (if any) at
/// [`RPC_FD`] and the seccomp program (if any) at [`SECCOMP_FD`], both inheritable, then applies
/// `l`.
fn apply_limits(
    l: ProcLimits,
    rpc_fd: Option<i32>,
    seccomp_fd: Option<i32>,
) -> std::io::Result<()> {
    let set = |res: i32, cur: u64, max: u64| -> std::io::Result<()> {
        let r = RLimit { cur, max };
        // SAFETY: r is a valid rlimit; setrlimit is async-signal-safe.
        if unsafe { setrlimit(res, &r) } != 0 {
            return Err(std::io::Error::last_os_error());
        }
        Ok(())
    };
    // Copy both descriptors above the targets first (close-on-exec copies), so that placing one
    // can never overwrite the other; dup2 then makes the targets inheritable.
    // SAFETY: plain descriptor syscalls on descriptors this process owns.
    unsafe {
        let high = |fd: i32| -> std::io::Result<i32> {
            let h = fcntl(fd, F_DUPFD_CLOEXEC, 16);
            if h < 0 {
                Err(std::io::Error::last_os_error())
            } else {
                Ok(h)
            }
        };
        let sec = match seccomp_fd {
            Some(fd) => Some(high(fd)?),
            None => None,
        };
        let rpc = match rpc_fd {
            Some(fd) => Some(high(fd)?),
            None => None,
        };
        if let Some(fd) = rpc {
            if dup2(fd, RPC_FD) != RPC_FD {
                return Err(std::io::Error::last_os_error());
            }
        }
        if let Some(fd) = sec {
            if dup2(fd, SECCOMP_FD) != SECCOMP_FD {
                return Err(std::io::Error::last_os_error());
            }
        }
    }

    if let Some(b) = l.address_space {
        set(RLIMIT_AS, b, b)?;
    }
    if let Some(s) = l.cpu_seconds {
        set(RLIMIT_CPU, s, s + 1)?;
    }
    if let Some(b) = l.file_size {
        set(RLIMIT_FSIZE, b, b)?;
    }
    set(RLIMIT_NOFILE, l.open_files, l.open_files)?;
    set(RLIMIT_CORE, 0, 0)?;
    // SAFETY: personality only changes this process's execution domain flags.
    unsafe {
        personality(ADDR_NO_RANDOMIZE);
    }
    Ok(())
}

fn spawn_stderr_reader(child: &mut Child) -> Arc<Mutex<Vec<u8>>> {
    let buf = Arc::new(Mutex::new(Vec::new()));
    if let Some(mut err) = child.stderr.take() {
        let b = buf.clone();
        std::thread::spawn(move || {
            let mut chunk = [0u8; 4096];
            while let Ok(n) = err.read(&mut chunk) {
                if n == 0 {
                    break;
                }
                let mut g = b.lock().unwrap_or_else(PoisonError::into_inner);
                if g.len() < 65536 {
                    g.extend_from_slice(&chunk[..n]);
                }
            }
        });
    }
    buf
}

// ---------------------------------------------------------------------------------------------
// The script sandbox
// ---------------------------------------------------------------------------------------------

/// A running script sandbox.
pub struct ScriptSandbox {
    child: Child,
    /// Kernel end of the RPC socket.
    pub sock: UnixStream,
    /// When the sandbox process was spawned (wall-limit origin).
    pub started: Instant,
    stderr: Arc<Mutex<Vec<u8>>>,
    ticks: u64,
    last_cpu_ms: u64,
    peak_rss_kb: u64,
    exited: Option<ExitStatus>,
    /// CPU time of the reaped sandbox child and the descendants it waited for (`wait4`).
    reaped_cpu_ms: u64,
}

impl ScriptSandbox {
    /// Starts the runner in a fresh sandbox over the Build's `sdk/` directory.
    pub fn spawn(sdk_dir: &Path, limits: &Limits) -> Result<ScriptSandbox> {
        let interp = interpreter()?;
        let bw = bwrap()?;
        let sdk = fs::canonicalize(sdk_dir)
            .map_err(|e| KernelError::Refused(format!("Build SDK {}: {e}", sdk_dir.display())))?;
        let (kernel_end, child_end) =
            UnixStream::pair().map_err(|e| KernelError::Io(format!("socketpair: {e}")))?;
        let mem = limits.memory_mb.saturating_mul(1024 * 1024);
        let seccomp = seccomp_pipe()?;
        let mut args = base_args(interp);
        args.extend([
            "--seccomp".into(),
            SECCOMP_FD.to_string(),
            "--setenv".into(),
            "CWR_RPC_FD".into(),
            RPC_FD.to_string(),
            "--ro-bind".into(),
            sdk.display().to_string(),
            SDK_MOUNT.into(),
            "--size".into(),
            mem.max(1024 * 1024).to_string(),
            "--tmpfs".into(),
            SCRATCH.into(),
            "--chdir".into(),
            SCRATCH.into(),
            "--remount-ro".into(),
            "/".into(),
            "--".into(),
            interp.executable.display().to_string(),
            "-P".into(),
            "-s".into(),
            "-S".into(),
            "-B".into(),
            "-c".into(),
            RUNNER.into(),
        ]);
        let pl = ProcLimits {
            address_space: Some(mem),
            cpu_seconds: Some(limits.cpu_ms.div_ceil(1000) + 1),
            file_size: Some(mem.max(1)),
            open_files: 64,
        };
        let fd = child_end.as_raw_fd();
        let sfd = seccomp.as_raw_fd();
        let mut cmd = Command::new(&bw);
        cmd.args(&args)
            .env_clear()
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::piped());
        // SAFETY: the closure only performs async-signal-safe syscalls (dup2, fcntl, setrlimit,
        // personality) between fork and exec.
        unsafe {
            cmd.pre_exec(move || apply_limits(pl, Some(fd), Some(sfd)));
        }
        let mut child = cmd.spawn().map_err(|e| {
            KernelError::Refused(format!(
                "sandbox unavailable: cannot start {}: {e}",
                bw.display()
            ))
        })?;
        drop(child_end);
        drop(seccomp);
        let stderr = spawn_stderr_reader(&mut child);
        kernel_end
            .set_read_timeout(Some(Duration::from_millis(10)))
            .map_err(|e| KernelError::Io(e.to_string()))?;
        Ok(ScriptSandbox {
            child,
            sock: kernel_end,
            started: Instant::now(),
            stderr,
            ticks: clk_tck(),
            last_cpu_ms: 0,
            peak_rss_kb: 0,
            exited: None,
            reaped_cpu_ms: 0,
        })
    }

    /// Process id of the sandbox process.
    pub fn pid(&self) -> u32 {
        self.child.id()
    }

    /// Polls the process tree: returns (cpu_ms, rss_kb) and remembers the maxima.
    pub fn sample(&mut self) -> (u64, u64) {
        if self.exited.is_some() {
            return (self.last_cpu_ms, self.peak_rss_kb);
        }
        // The one-process filter keeps the tree to bubblewrap, its pid-namespace init and the
        // interpreter, so the walk is complete without a cap.
        let mut pids = vec![self.child.id()];
        let mut i = 0;
        while i < pids.len() {
            pids.extend(children_of(pids[i]));
            i += 1;
        }
        let mut ticks = 0u64;
        let mut rss = 0u64;
        for p in &pids {
            ticks += proc_cpu_ticks(*p).unwrap_or(0);
            rss += proc_rss_kb(*p).unwrap_or(0);
        }
        let ms = ticks * 1000 / self.ticks.max(1);
        self.last_cpu_ms = self.last_cpu_ms.max(ms);
        self.peak_rss_kb = self.peak_rss_kb.max(rss);
        (self.last_cpu_ms, rss)
    }

    fn pid_i32(&self) -> i32 {
        i32::try_from(self.child.id()).unwrap_or(-1)
    }

    /// Whether the sandbox process has exited (without blocking). An exited process is reaped
    /// here with `wait4`, which also yields its own CPU time.
    pub fn try_wait(&mut self) -> Option<ExitStatus> {
        if self.exited.is_none() {
            if let Some((s, cpu)) = reap(self.pid_i32(), true) {
                self.exited = Some(s);
                self.reaped_cpu_ms = cpu;
            }
        }
        self.exited
    }

    /// Kills the process tree (bwrap dies, `--die-with-parent` and the pid namespace take the
    /// interpreter with it) and reaps it. Returns the exit status and the CPU time of the tree:
    /// the larger of the reaped child's own `wait4` usage and the last `/proc` sample of the
    /// tree, never the CPU of any other child of the kernel.
    pub fn finish(mut self) -> (Option<ExitStatus>, u64, String) {
        self.sample();
        if self.exited.is_none() {
            self.kill_and_reap();
        }
        let cpu = self.reaped_cpu_ms.max(self.last_cpu_ms);
        // Give the stderr reader a moment to drain what the dead process left.
        let deadline = Instant::now() + Duration::from_millis(50);
        while Arc::strong_count(&self.stderr) > 1 && Instant::now() < deadline {
            std::thread::sleep(Duration::from_millis(2));
        }
        let err =
            String::from_utf8_lossy(&self.stderr.lock().unwrap_or_else(PoisonError::into_inner))
                .into_owned();
        (self.exited, cpu, err)
    }

    /// Highest resident set size observed, in kilobytes.
    pub fn peak_rss_kb(&self) -> u64 {
        self.peak_rss_kb
    }

    /// Sends SIGKILL to the unreaped sandbox child and reaps it with `wait4`.
    fn kill_and_reap(&mut self) {
        let pid = self.pid_i32();
        if pid <= 0 {
            return;
        }
        // SAFETY: pid is our child and has not been reaped, so it cannot have been reused.
        unsafe {
            kill(pid, SIGKILL);
        }
        if let Some((s, cpu)) = reap(pid, false) {
            self.exited = Some(s);
            self.reaped_cpu_ms = cpu;
        }
    }
}

impl Drop for ScriptSandbox {
    fn drop(&mut self) {
        if self.exited.is_none() {
            self.kill_and_reap();
        }
    }
}

fn children_of(pid: u32) -> Vec<u32> {
    let mut out = Vec::new();
    let tasks = format!("/proc/{pid}/task");
    if let Ok(rd) = fs::read_dir(&tasks) {
        for t in rd.flatten() {
            if let Ok(s) = fs::read_to_string(t.path().join("children")) {
                out.extend(s.split_whitespace().filter_map(|x| x.parse::<u32>().ok()));
            }
        }
    }
    out
}

/// utime + stime + cutime + cstime of one process, in clock ticks.
fn proc_cpu_ticks(pid: u32) -> Option<u64> {
    let s = fs::read_to_string(format!("/proc/{pid}/stat")).ok()?;
    let after = &s[s.rfind(')')? + 1..];
    let f: Vec<&str> = after.split_whitespace().collect();
    // After the comm field: state is index 0, so utime (field 14) is index 11.
    let g = |i: usize| {
        f.get(i)
            .and_then(|x| x.parse::<i64>().ok())
            .map(|x| x.max(0) as u64)
    };
    Some(g(11)? + g(12)? + g(13)? + g(14)?)
}

fn proc_rss_kb(pid: u32) -> Option<u64> {
    let s = fs::read_to_string(format!("/proc/{pid}/status")).ok()?;
    s.lines()
        .find(|l| l.starts_with("VmRSS:"))
        .and_then(|l| l.split_whitespace().nth(1))
        .and_then(|x| x.parse().ok())
}

// ---------------------------------------------------------------------------------------------
// The world-code worker sandbox
// ---------------------------------------------------------------------------------------------

/// Starts the kernel-side world-code worker (`python -c <source>`) in a sandbox whose view holds
/// the interpreter and the Build's `lib/` at [`LIB_MOUNT`], with stdin and stdout piped.
pub fn spawn_worker(lib_dir: Option<&Path>, source: &str) -> Result<Child> {
    let interp = interpreter()?;
    let bw = bwrap()?;
    let mut args = base_args(interp);
    if let Some(lib) = lib_dir {
        let lib = fs::canonicalize(lib)
            .map_err(|e| KernelError::Refused(format!("Build lib {}: {e}", lib.display())))?;
        args.extend([
            "--ro-bind".into(),
            lib.display().to_string(),
            LIB_MOUNT.into(),
        ]);
    }
    args.extend([
        "--remount-ro".into(),
        "/".into(),
        "--".into(),
        interp.executable.display().to_string(),
        "-P".into(),
        "-s".into(),
        "-S".into(),
        "-B".into(),
        "-c".into(),
        source.to_string(),
    ]);
    let pl = ProcLimits {
        address_space: Some(WORKER_MEMORY_MB * 1024 * 1024),
        cpu_seconds: Some(WORKER_CPU_SECONDS),
        file_size: None,
        open_files: 64,
    };
    let mut cmd = Command::new(&bw);
    cmd.args(&args)
        .env_clear()
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::null());
    // SAFETY: as in ScriptSandbox::spawn.
    unsafe {
        cmd.pre_exec(move || apply_limits(pl, None, None));
    }
    cmd.spawn().map_err(|e| {
        KernelError::Refused(format!(
            "sandbox unavailable: cannot start {}: {e}",
            bw.display()
        ))
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn runner_is_embedded() {
        assert!(RUNNER.contains("def _cwr_main"));
        assert!(RUNNER.contains("sys.addaudithook"));
    }

    #[test]
    fn the_script_sandbox_filter_allows_one_process_with_threads() {
        let src = r#"
import os, sys, threading
out = []
try:
    pid = os.fork()
    if pid == 0:
        os._exit(0)
    out.append("forked")
except OSError as e:
    out.append("fork:%d" % e.errno)
t = threading.Thread(target=lambda: out.append("thread"))
t.start(); t.join()
sys.stdout.write(" ".join(out) + "\n")
"#;
        // The same bubblewrap arguments, seccomp descriptor placement and limits as
        // ScriptSandbox::spawn, without the SDK and RPC socket a real turn needs.
        let interp = interpreter().expect("interpreter");
        let seccomp = seccomp_pipe().expect("seccomp program");
        let mut args = base_args(interp);
        args.extend([
            "--seccomp".to_string(),
            SECCOMP_FD.to_string(),
            "--remount-ro".into(),
            "/".into(),
            "--".into(),
            interp.executable.display().to_string(),
            "-c".into(),
            src.into(),
        ]);
        let pl = ProcLimits {
            address_space: Some(256 * 1024 * 1024),
            cpu_seconds: Some(10),
            file_size: None,
            open_files: 64,
        };
        let sfd = seccomp.as_raw_fd();
        let mut cmd = Command::new(bwrap().expect("bubblewrap"));
        cmd.args(&args)
            .env_clear()
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        // SAFETY: as in ScriptSandbox::spawn.
        unsafe {
            cmd.pre_exec(move || apply_limits(pl, None, Some(sfd)));
        }
        let out = cmd.output().unwrap();
        drop(seccomp);
        let text = String::from_utf8_lossy(&out.stdout);
        assert!(
            text.contains("fork:1"),
            "fork must fail with EPERM: {text} {}",
            String::from_utf8_lossy(&out.stderr)
        );
        assert!(text.contains("thread"), "threads stay allowed: {text}");
    }

    #[test]
    fn workers_run_under_their_address_space_cap() {
        let src = "import resource, sys\nsys.stdout.write('as:%d\\n' % resource.getrlimit(resource.RLIMIT_AS)[0])\n";
        let child = spawn_worker(None, src).expect("bubblewrap sandbox");
        let out = child.wait_with_output().unwrap();
        let text = String::from_utf8_lossy(&out.stdout);
        assert!(
            text.contains(&format!("as:{}", WORKER_MEMORY_MB * 1024 * 1024)),
            "{text}"
        );
    }

    #[test]
    fn the_filter_is_whole_sock_filter_entries() {
        let f = no_fork_filter().unwrap();
        assert_eq!(f.len() % 8, 0);
        assert_eq!(f.len() / 8, 15);
    }

    #[test]
    fn proc_parsing_on_self() {
        let me = std::process::id();
        assert!(proc_cpu_ticks(me).is_some());
        assert!(proc_rss_kb(me).unwrap_or(0) > 0);
    }
}
