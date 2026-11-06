<!-- Copyright Sidiora Labs Inc. CWR® 2026-2027 -->
<!-- SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0 -->

# CWR turn sandbox: what is enforced, by what, and how it is tested

Status: implemented by task 2.2 (`crates/cwr-kernel/src/sandbox.rs`, `executor.rs`, `contract_check.rs`,
`sdk/python/cwr/_runner.py`); exercised by `tests/cwr/cases/2_2.py` (case CWR-007). The contract is the
observable closure of [spec/cwr/kernel.md](../../spec/cwr/kernel.md) section 3 and clause 6.1; this file pins
the mechanism chosen on this host (decisions.md, "Decisions required during implementation") and says
exactly which guarantee each layer provides. A guarantee not listed here is not claimed.

## Mechanism

Each `cwr instance turn` starts two fresh processes and discards both at turn end:

* the **script sandbox**: `bwrap` (bubblewrap) running `python3.12 -P -s -S -B -c <_runner.py>`, which runs
  exactly one script and talks to the kernel only through the RPC socket at fd 3;
* the **world-code worker**: the same kind of `bwrap` view with the Build's `lib/` mounted at `/build/lib`
  instead of the SDK, running the Build's `lib/*.py` for the kernel's atomic contract check.

If `bwrap` or a Python 3.12 interpreter is missing, or the Build's `world.runtime` is not the interpreter's
major.minor, the turn is refused before anything executes (fail closed). The kernel never runs a script
outside the sandbox. `CWR_BWRAP` and `CWR_PYTHON` may name the binaries; `cwr instance turn` prints the
mechanism it used on stderr (`sandbox=bubblewrap bwrap=... python=...`).

## Layer 1: kernel-enforced (Linux namespaces, mounts, rlimits)

These hold even if the script fully defeats the in-interpreter layer.

| Guarantee | Mechanism | Evidence in the driver |
|---|---|---|
| No network | `--unshare-all` gives a new, empty network namespace: loopback (down) plus at most the kernel's fallback tunnel devices (`tunl0` and the like, down, no address), no route to any address | diagnostic turn: TCP connect and UDP send fail with `ENETUNREACH` (101); `if_nameindex()` lists only `lo` and fallback tunnel devices |
| No host filesystem | new mount namespace; the root is an empty tmpfs remounted read-only holding only the interpreter executable, its stdlib directory, the directories of the shared objects it maps, the loader symlinks, the Build SDK read-only at `/cwr/sdk` and a private tmpfs at `/scratch`; no `/proc`, `/dev`, `/etc`, `/root`, `/home`, `/tmp` | diagnostic turn: `/` lists only `cwr`, `lib*`, `scratch`, `usr`; host paths absent; writing `/` gives `EROFS`; writing the SDK fails |
| Nothing survives the turn | the process, its namespaces and the `/scratch` tmpfs die with the turn | a marker written to `/scratch` in one turn is absent in the next |
| No host identity | uts namespace (hostname `cwr`), pid namespace, user namespace with uid/gid 65534, all capabilities dropped, nested user namespaces disabled | diagnostic turn: uid 65534, hostname `cwr` |
| No host environment, no inherited descriptors | `--clearenv` (only `PYTHONHASHSEED=0`, `PYTHONUTF8=1`, `CWR_RPC_FD=3`); every kernel descriptor is close-on-exec, the RPC socket is placed at fd 3; stdin and stdout are `/dev/null` on the host side | diagnostic turn: environment keys and open descriptors 0 to 3 only |
| Memory cap (`memory_mb`) | `RLIMIT_AS = memory_mb` MiB on the process tree (address space, which bounds resident memory); `/scratch` tmpfs sized to `memory_mb`; `RLIMIT_FSIZE = memory_mb` MiB; the kernel also samples resident memory of the tree every 10 ms and kills above the cap | big allocation, gradual growth and a caught `MemoryError` each end the turn with `LIMIT memory_mb` |
| CPU cap (`cpu_ms`) | the kernel samples the tree's CPU time (`/proc/<pid>/stat`, utime+stime of every descendant) every 10 ms and kills it above `cpu_ms`; `RLIMIT_CPU` at `ceil(cpu_ms/1000)+1` s is a hard backstop | busy loop ends with `LIMIT cpu_ms`, measured CPU close to the limit |
| Wall cap (`wall_ms`) | host timer on the turn from process start; the kernel checks it at least every 10 ms while it waits on the RPC channel and kills the tree when it expires; every kernel read and write on the RPC socket is bounded by the remaining wall time. The budget covers the kernel's own work on each primitive, including the fsync that makes its ledger record durable before the answer, so on a host with slow storage a turn affords fewer calls. A primitive already executing when the timer expires is not interrupted (primitives are atomic). It completes and is recorded, and the turn then ends with `LIMIT wall_ms`. The reported `wall_ms` can therefore exceed the limit by at most the duration of that one primitive plus one poll interval | a script blocked on the RPC channel ends with `LIMIT wall_ms` |
| No core dumps, bounded descriptors | `RLIMIT_CORE = 0`, `RLIMIT_NOFILE = 64` | (configuration) |
| Deterministic object identities | address-space randomisation is disabled with `personality(ADDR_NO_RANDOMIZE)`, and `PYTHONHASHSEED=0` fixes string hashing, so `id()` and `hash()` are not a host random source | (configuration; interface determinism is tested over ledgers) |
| `lib/` has no network either | the world-code worker runs in the same namespaces with the Build `lib/` read-only and nothing writable | (configuration; clause 7.1 is task 2.3's) |

`--diagnostic-no-language-guard` on `cwr instance turn` switches off layer 2 only, so that the rows above can
be demonstrated with ordinary `os`, `socket` and `open` calls. It is an operator diagnostic; a harness never
passes it.

## Layer 2: in-interpreter closure (`sdk/python/cwr/_runner.py`)

Layer 2 turns every attempt to leave the Build into a recorded refusal instead of an operating-system error:
the runner reports it on the RPC channel, the kernel appends a ledger record with `primitive =
"sandbox.<facility>"` and `outcome.code = INVALID` (no tick), and the script receives the typed `INVALID`
exception (also an `ImportError` or `PermissionError`, so ordinary handlers work). Facilities:
`sandbox.import`, `sandbox.filesystem`, `sandbox.network`, `sandbox.process`, `sandbox.clock`,
`sandbox.interpreter`, `sandbox.os`, `sandbox.thread`, `sandbox.rpc`.

| Guarantee | Mechanism | Evidence in the driver |
|---|---|---|
| Only the Build SDK is importable | `builtins.__import__` admits only the Build's world modules, `cwr` and `cwr.errors`; an audit hook (PEP 578, cannot be removed) refuses every module load after the guard is armed | `import time`, `datetime`, `socket`, `os`, `sys`, `json`, `random`, `subprocess`, an unlisted world module: each `sandbox.import` |
| No host clock | `time` is never importable; every callable of the already-loaded `time` module is replaced in place by a refusing stub (so a reference reached through the object graph is refused too); the `time.sleep` audit event is refused; the SDK exposes world ticks only (`world.now`) | `import time` and an object-graph walk to `os.stat` (a file timestamp) are refused |
| No files | `builtins.open` is a refusing stub; the `open` audit event (covers `io.open`, `FileIO`, `open_code`) and every `os.*`, `shutil.*`, `glob.*`, `tempfile.*` event are refused; every callable of `posix`/`os` is a refusing stub | `open('/etc/passwd')`, opening `Cargo.toml` of the repository by absolute path, a graph walk to `os.listdir` are refused |
| No sockets | the script never receives a socket: the SDK's channel is a raw file object (`sendall`/`recv` over `FileIO`), so `setsockopt`, `recvmsg` timestamps and the like are not reachable; `socket` is not importable; every `socket.*` audit event (creation, connect, bind, DNS) is refused | a socket class found by walking `object.__subclasses__()` cannot be instantiated: `sandbox.network` |
| No processes, threads or signals | `subprocess`, `os`, `_thread`, `signal` are not importable and their already-loaded callables are stubs; process audit events (`os.system`, `os.exec*`, `os.fork`, `os.posix_spawn`, `subprocess.Popen`, `os.kill`) are refused | `import subprocess` refused |
| No frame introspection | the audit events for `tb_frame`, `gi_frame`, `__code__` access, `sys._getframe`, `sys.settrace`, `sys.setprofile`, `gc.get_objects`/`get_referrers`, `marshal.loads`, `ctypes` are refused, so a script cannot reach the runner's frames | `e.__traceback__.tb_frame` refused with `sandbox.interpreter` |
| A caught `MemoryError` still ends the turn | a `sys.monitoring` RAISE callback reports any `MemoryError` as `LIMIT memory_mb` the moment it is raised; the kernel then kills the process | `try: bytearray(huge) except MemoryError: return 'survived'` ends with `LIMIT memory_mb` |
| Unlisted primitives are refused by the kernel, not the SDK | every request is checked against `contracts.json` in Rust; the SDK is not trusted | a raw `cwr._rpc.call('fs.nuke', {})` is `INVALID`, recorded, not executed |
| Forged failures are not believed | the turn's `error` is a world failure only if it equals one the kernel returned during the turn; anything else the script raises is reported as `INVALID turn.script` | a script raising `cwr.errors.Locked(...)` itself gets `INVALID turn.script` |

## What is not claimed

* Layer 2 is a guard inside a Python interpreter the script shares. A script that defeats it (for example
  through an interpreter bug) is still inside layer 1: no network, no host files, no host identity, the
  resource caps. It could then read the host clock (the `clock_gettime` vDSO cannot be filtered by
  namespaces or seccomp) or open files inside its own read-only view. The clock guarantee is therefore a
  layer 2 guarantee only.
* A script can disable its own `sys.monitoring` callback through the `sys` module it reaches by walking
  module globals; it would then catch its own `MemoryError`, but the address-space cap and the kernel's
  resident-memory sampling still hold, so memory stays bounded.
* A script can write raw frames on the RPC channel it is given. Every frame is validated by the kernel; the
  worst a forged `limit` frame can do is end the script's own turn with `LIMIT memory_mb`.
* Process count is bounded by the pid namespace and layer 2, not by `RLIMIT_NPROC` (which counts per host
  uid and would throttle the kernel's own user).
* `RLIMIT_AS` caps address space, a stricter measure than resident memory; an interpreter whose mappings
  alone approach `memory_mb` cannot run (Python 3.12 with the SDK maps about 25 MiB).
* No cgroup is created: the host's cgroup tree is left untouched. A stricter mechanism (cgroups v2, a
  WebAssembly host) is a future choice; any mechanism must satisfy CWR-007.
