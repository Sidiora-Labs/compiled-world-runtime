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
//! * **Resource limits**: `RLIMIT_AS = memory_mb` (address space, which bounds resident memory),
//!   `RLIMIT_CPU` as a hard backstop just above `cpu_ms`, `RLIMIT_FSIZE`, `RLIMIT_NOFILE` and
//!   `RLIMIT_CORE = 0`; the kernel also polls the process tree's CPU time and resident memory
//!   and kills it the moment `cpu_ms` or `memory_mb` is exceeded, and kills it at `wall_ms`.
//! * **Language layer**: `sdk/python/cwr/_runner.py`, embedded here, guards imports, files,
//!   sockets, processes, the clock and frame introspection from inside the interpreter.
//!
//! Without bubblewrap the kernel refuses to run a turn (fail closed); it never runs a script
//! unsandboxed. The kernel-side world-code worker ([`crate::contract_check::LibWorker`]) runs in
//! the same kind of view with the Build's `lib/` instead of the SDK and no scratch directory, so
//! `lib/` has no network capability either (clause 7.1).

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
    fn getrusage(who: i32, usage: *mut RUsage) -> i32;
    fn personality(persona: u64) -> i32;
    fn dup2(oldfd: i32, newfd: i32) -> i32;
    fn fcntl(fd: i32, cmd: i32, ...) -> i32;
    fn sysconf(name: i32) -> i64;
}

const RLIMIT_CPU: i32 = 0;
const RLIMIT_FSIZE: i32 = 1;
const RLIMIT_CORE: i32 = 4;
const RLIMIT_NOFILE: i32 = 7;
const RLIMIT_AS: i32 = 9;
const RUSAGE_CHILDREN: i32 = -1;
const ADDR_NO_RANDOMIZE: u64 = 0x0040000;
const F_SETFD: i32 = 2;
const SC_CLK_TCK: i32 = 2;

fn clk_tck() -> u64 {
    // SAFETY: sysconf has no preconditions.
    let t = unsafe { sysconf(SC_CLK_TCK) };
    if t > 0 {
        t as u64
    } else {
        100
    }
}

/// CPU time (ms) of all children of this process that have been waited for.
pub fn children_cpu_ms() -> u64 {
    let mut u = RUsage {
        utime: TimeVal { sec: 0, usec: 0 },
        stime: TimeVal { sec: 0, usec: 0 },
        rest: [0; 14],
    };
    // SAFETY: u is a valid, writable rusage-sized buffer.
    if unsafe { getrusage(RUSAGE_CHILDREN, &mut u) } != 0 {
        return 0;
    }
    let ms = |t: &TimeVal| (t.sec.max(0) as u64) * 1000 + (t.usec.max(0) as u64) / 1000;
    ms(&u.utime) + ms(&u.stime)
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

/// One line describing the mechanism, for operators and drivers.
pub fn describe() -> String {
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
        "sandbox=bubblewrap bwrap={} python={} namespaces=user,mount,net,pid,ipc,uts,cgroup rlimits=AS,CPU,FSIZE,NOFILE,CORE language_guard=sdk/python/cwr/_runner.py",
        b.unwrap_or_else(|e| e.to_string()),
        i.unwrap_or_else(|e| e.to_string())
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

fn apply_limits(l: ProcLimits, rpc_fd: Option<i32>) -> std::io::Result<()> {
    let set = |res: i32, cur: u64, max: u64| -> std::io::Result<()> {
        let r = RLimit { cur, max };
        // SAFETY: r is a valid rlimit; setrlimit is async-signal-safe.
        if unsafe { setrlimit(res, &r) } != 0 {
            return Err(std::io::Error::last_os_error());
        }
        Ok(())
    };
    if let Some(fd) = rpc_fd {
        // SAFETY: plain descriptor syscalls on descriptors this process owns.
        unsafe {
            if fd == RPC_FD {
                if fcntl(fd, F_SETFD, 0) != 0 {
                    return Err(std::io::Error::last_os_error());
                }
            } else if dup2(fd, RPC_FD) != RPC_FD {
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
    cpu_before: u64,
    ticks: u64,
    last_cpu_ms: u64,
    peak_rss_kb: u64,
    exited: Option<ExitStatus>,
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
        let mut args = base_args(interp);
        args.extend([
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
        let cpu_before = children_cpu_ms();
        let mut cmd = Command::new(&bw);
        cmd.args(&args)
            .env_clear()
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::piped());
        // SAFETY: the closure only performs async-signal-safe syscalls (dup2, fcntl, setrlimit,
        // personality) between fork and exec.
        unsafe {
            cmd.pre_exec(move || apply_limits(pl, Some(fd)));
        }
        let mut child = cmd.spawn().map_err(|e| {
            KernelError::Refused(format!(
                "sandbox unavailable: cannot start {}: {e}",
                bw.display()
            ))
        })?;
        drop(child_end);
        let stderr = spawn_stderr_reader(&mut child);
        kernel_end
            .set_read_timeout(Some(Duration::from_millis(10)))
            .map_err(|e| KernelError::Io(e.to_string()))?;
        Ok(ScriptSandbox {
            child,
            sock: kernel_end,
            started: Instant::now(),
            stderr,
            cpu_before,
            ticks: clk_tck(),
            last_cpu_ms: 0,
            peak_rss_kb: 0,
            exited: None,
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
        let mut pids = vec![self.child.id()];
        let mut i = 0;
        while i < pids.len() && pids.len() < 64 {
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

    /// Whether the sandbox process has exited (without blocking).
    pub fn try_wait(&mut self) -> Option<ExitStatus> {
        if self.exited.is_none() {
            if let Ok(Some(s)) = self.child.try_wait() {
                self.exited = Some(s);
            }
        }
        self.exited
    }

    /// Kills the process tree (bwrap dies, `--die-with-parent` and the pid namespace take the
    /// interpreter with it) and reaps it. Returns the exit status and the CPU time of the tree.
    pub fn finish(mut self) -> (Option<ExitStatus>, u64, String) {
        self.sample();
        if self.exited.is_none() {
            let _ = self.child.kill();
            self.exited = self.child.wait().ok();
        }
        let cpu = children_cpu_ms()
            .saturating_sub(self.cpu_before)
            .max(self.last_cpu_ms);
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
}

impl Drop for ScriptSandbox {
    fn drop(&mut self) {
        if self.exited.is_none() {
            let _ = self.child.kill();
            let _ = self.child.wait();
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
        address_space: None,
        cpu_seconds: None,
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
        cmd.pre_exec(move || apply_limits(pl, None));
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
    fn proc_parsing_on_self() {
        let me = std::process::id();
        assert!(proc_cpu_ticks(me).is_some());
        assert!(proc_rss_kb(me).unwrap_or(0) > 0);
    }
}
