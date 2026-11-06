<!-- Copyright Sidiora Labs Inc. CWR® 2026-2027 -->
<!-- SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0 -->

# Turn sandbox: what is enforced, by what, and how it is tested

A turn script reaches nothing but the generated SDK of its Build: no network, no host filesystem, no
host clock, no host identity and nothing that survives the turn. Two independent layers enforce
this. The first is the operating system, through Linux namespaces, mounts and resource limits. The
second is a guard inside the Python interpreter that turns every attempt to leave the Build into a
recorded refusal. This document states which guarantee each layer provides. A guarantee not listed
here is not claimed.

The contract is the closure requirement in [kernel.md](../../spec/cwr/kernel.md) section 3. The
qualification case CWR-007 tests every row below through its driver, `tests/cwr/cases/2_2.py`. A gap
between this document and the specification is a defect to report under
[SECURITY.md](../../SECURITY.md).

## Mechanism

Each `cwr instance turn` starts two fresh processes and discards both when the turn ends.

| Process | What it runs |
| --- | --- |
| Script sandbox | `bwrap` (bubblewrap) running `python3.12 -P -s -S -B -c <runner>`. It runs exactly one script and talks to the World Kernel only through the RPC socket on file descriptor 3. |
| World-code worker | The same kind of `bwrap` view, with the Build's `lib/` mounted read-only at `/build/lib` instead of the SDK. It runs the Build's primitive implementations for the kernel's atomic contract check. |

Adapter code (`adapters/<module>/live.py`) and migrations run in workers of the same kind. Every
worker has an address-space cap of 2048 MiB and a CPU-time cap of 300 seconds. Workers run the
Build's own code, so the one-process filter below applies to the script sandbox only.

The sandbox fails closed. If `bwrap` or a Python 3.12 interpreter is missing, or the Build's
`world.runtime` is not the interpreter's major and minor version, the turn is refused before anything
runs. The World Kernel never runs a script outside the sandbox.

`CWR_BWRAP` and `CWR_PYTHON` may name the binaries. `cwr instance turn` prints the mechanism it used
on stderr, for example:

```text
sandbox=bubblewrap bwrap=/usr/bin/bwrap (bubblewrap 0.9.0) python=/usr/bin/python3.12 3.12.3 namespaces=user,mount,net,pid,ipc,uts,cgroup rlimits=AS,CPU,FSIZE,NOFILE,CORE language_guard=sdk/python/cwr/_runner.py
```

## Layer 1: operating system

These guarantees hold even if a script fully defeats the in-interpreter layer.

| Guarantee | Mechanism | Test |
| --- | --- | --- |
| No network | `--unshare-all` gives a new, empty network namespace: loopback (down) and at most the kernel's fallback tunnel devices (down, no address), with no route anywhere | TCP connect and UDP send fail with `ENETUNREACH`, and `if_nameindex()` lists only `lo` and fallback tunnel devices |
| No host filesystem | A new mount namespace whose root is an empty, read-only tmpfs holding only the interpreter, its standard library, the shared objects it maps, the loader links, the Build SDK read-only at `/cwr/sdk` and a private tmpfs at `/scratch`. There is no `/proc`, `/dev`, `/etc`, `/root`, `/home` or `/tmp`. | `/` lists only `cwr`, `lib*`, `scratch` and `usr`. Host paths are absent, writing `/` gives `EROFS` and writing the SDK fails. |
| Nothing survives the turn | The process, its namespaces and the `/scratch` tmpfs end with the turn | A marker written to `/scratch` in one turn is absent in the next |
| No host identity | UTS namespace with hostname `cwr`, PID namespace, user namespace with uid and gid 65534, all capabilities dropped and nested user namespaces disabled | The script sees uid 65534 and hostname `cwr` |
| No host environment or inherited descriptors | `--clearenv` leaves only `PYTHONHASHSEED=0`, `PYTHONUTF8=1` and `CWR_RPC_FD=3`. Every kernel descriptor is close-on-exec, the RPC socket sits at descriptor 3, and stdin and stdout are `/dev/null`. | The script sees only those environment keys and descriptors 0 to 3 |
| One process | bubblewrap installs a seccomp filter on the interpreter just before it starts. `fork`, `vfork` and every `clone` without `CLONE_THREAD` fail with `EPERM`, and `clone3` fails with `ENOSYS` so that threads use `clone`. The script is one process with threads, so the per-process limits below bound the whole sandbox. | Under the script sandbox's filter, `os.fork()` fails with `EPERM` while a thread starts, in the kernel's unit tests |
| Memory cap (`memory_mb`) | `RLIMIT_AS` of `memory_mb` MiB on the one interpreter process and `RLIMIT_FSIZE` of the same size. The kernel also samples the process's resident memory every 10 ms and kills it above the cap. The `/scratch` tmpfs is capped at `memory_mb` MiB separately, so a turn holds at most `2 × memory_mb` MiB: its address space plus its scratch files. | A large allocation, gradual growth and a caught `MemoryError` each end the turn with `LIMIT memory_mb` |
| CPU cap (`cpu_ms`) | The kernel samples the tree's CPU time every 10 ms and kills it above `cpu_ms`. `RLIMIT_CPU` at `ceil(cpu_ms / 1000) + 1` seconds is a hard backstop. The reported CPU time is the sandbox's own: its process is reaped with `wait4`, so no other child of the kernel is charged to the turn. | A busy loop ends with `LIMIT cpu_ms`, with measured CPU close to the limit |
| Wall-clock cap (`wall_ms`) | A host timer from process start. The kernel kills the tree when it expires, and every kernel read and write on the RPC socket is bounded by the remaining time. The timer also bounds the kernel while it serves a call: every exchange with the world-code worker ends at the turn deadline (the worker is killed), and adapter calls end at the earlier of their `timeout_ms` and the turn deadline. A call cut off this way is recorded as `LIMIT wall_ms`, and the turn ends with `LIMIT wall_ms`. | A script blocked on the RPC channel ends with `LIMIT wall_ms` |
| No core dumps, bounded descriptors | `RLIMIT_CORE` of 0 and `RLIMIT_NOFILE` of 64 | Configuration |
| Deterministic object identities | Address-space randomisation is disabled with `personality(ADDR_NO_RANDOMIZE)`, and `PYTHONHASHSEED=0` fixes string hashing, so `id()` and `hash()` are not a source of host randomness | Configuration. Interface determinism is tested over ledgers. |
| World code has no network either | The world-code worker runs in the same namespaces with `lib/` read-only and nothing writable | Configuration |

`cwr instance turn --diagnostic-no-language-guard` switches off layer 2 only, so that the rows above
can be shown with ordinary `os`, `socket` and `open` calls. It is an operator diagnostic. A harness
never passes it.

## Layer 2: in-interpreter closure

The runner, `sdk/python/cwr/_runner.py`, turns every attempt to leave the Build into a recorded
refusal instead of an operating-system error. The runner reports the attempt on the RPC channel. The
kernel appends a ledger record with `primitive` set to `sandbox.<facility>` and `outcome.code` set to
`INVALID`, without advancing world time. The script receives the typed `INVALID` exception, which is
also an `ImportError` or `PermissionError` so that ordinary handlers work.

The facilities are `sandbox.import`, `sandbox.filesystem`, `sandbox.network`, `sandbox.process`,
`sandbox.clock`, `sandbox.interpreter`, `sandbox.os`, `sandbox.thread` and `sandbox.rpc`.

| Guarantee | Mechanism | Test |
| --- | --- | --- |
| Only the Build SDK is importable | `builtins.__import__` admits only the Build's world modules, `cwr` and `cwr.errors`. A PEP 578 audit hook, which cannot be removed, refuses every module load after the guard is armed. | `import time`, `datetime`, `socket`, `os`, `sys`, `json`, `random`, `subprocess` and an unlisted world module are each refused as `sandbox.import` |
| No host clock | `time` is never importable. Every callable of the already-loaded `time` module is replaced by a refusing stub, in the module and in every container that captured it before the guard was armed, such as `threading._time`. The runner rewrites each dict, list, set, closure cell, tuple and frozenset the garbage collector tracks that holds an original, and does not run the script if any reference cannot be rewritten. `time.timezone`, `altzone`, `daylight` and `tzname` read as UTC. The `time.sleep` audit event is refused. The SDK exposes world ticks only. | `import time`, a graph walk to `threading._time`, and a graph walk to `os.stat` for a file timestamp are refused as `sandbox.clock` or `sandbox.os`. A walk of every module, class, closure and `builtins` finds no built-in of `time`, `posix`, `_thread`, `_signal` or `_imp`, and no clock-named callable that returns a value. The same walk with the guard disabled finds them, as a control |
| No files | `builtins.open` is a refusing stub. The `open` audit event, which covers `io.open`, `FileIO` and `open_code`, and every `os.*`, `shutil.*`, `glob.*` and `tempfile.*` event are refused. Every callable of `posix` and `os` is a refusing stub, including the copies held in `os.supports_fd`, `os.supports_dir_fd` and the other capability sets. | `open('/etc/passwd')`, opening a file of the repository, an object-graph walk to `os.listdir` and `os.stat` taken from `os.supports_fd` are refused |
| No sockets | The script never receives a socket: the SDK's channel is a raw file object, so socket options and timestamps are unreachable. `socket` is not importable, and every `socket.*` audit event (creation, connect, bind, DNS) is refused. | A socket class found through `object.__subclasses__()` cannot be instantiated and is refused as `sandbox.network` |
| No processes, threads or signals | `subprocess`, `os`, `_thread` and `signal` are not importable, and their already-loaded callables are stubs wherever they are referenced, including the copies `threading` captured. Process audit events (`os.system`, `os.exec*`, `os.fork`, `os.posix_spawn`, `subprocess.Popen`, `os.kill`) are refused. | `import subprocess` is refused, and `threading.Thread(...).start()` reached through the object graph is refused as `sandbox.thread` |
| No frame introspection | The audit events for `tb_frame`, `gi_frame`, `__code__` access, `sys._getframe`, `sys.settrace`, `sys.setprofile`, `gc.get_objects`, `gc.get_referrers`, `marshal.loads`, `ctypes` and reads of a function's `__defaults__` and `__kwdefaults__` are refused, so a script cannot reach the runner's frames | `e.__traceback__.tb_frame` is refused as `sandbox.interpreter` |
| A caught `MemoryError` still ends the turn | A `sys.monitoring` RAISE callback reports a `MemoryError` the interpreter raised for a failed allocation as `LIMIT memory_mb` the moment it is raised, and the kernel then kills the process. A `MemoryError` the script raises, re-raises or throws itself is an ordinary exception. The report carries a per-turn token from the start frame that only the callback holds, and the kernel treats a `limit` frame without it as broken framing. If the callback cannot be registered, the script does not run | `try: bytearray(huge) except MemoryError: return 'survived'` ends with `LIMIT memory_mb`; `raise MemoryError('x')` and a raw `limit` frame do not |
| Refusals cannot be disarmed | The audit hook alone decides and reports every refusal, and nothing switches it off, not even the end of the turn. The refusing stubs and the import guard only raise the audit event `cwr.refuse`. Their closures hold that builtin, strings and frozen sets, never a flag, the RPC channel or the original `__import__` | Walking `open.__closure__` and `__import__.__closure__` and flipping every reachable cell leaves a file read and `import os` refused and recorded |
| Unlisted primitives are refused by the kernel | Every request is checked against the Build's `contracts.json` in the kernel. The SDK is not trusted. | A raw `cwr._rpc.call('fs.nuke', {})` is `INVALID`, recorded and not executed |
| Forged failures are not believed | A turn's error counts as a world failure only if it equals one the kernel returned during the turn. Anything else the script raises is reported as `INVALID` naming `turn.script`. | A script that raises `cwr.errors.Locked(...)` itself gets `INVALID` naming `turn.script` |

## Limits of the guarantees

- Layer 2 is a guard inside an interpreter the script shares. A script that defeats it, for example
  through an interpreter bug, is still inside layer 1: no network, no host files, no host identity and
  the resource caps. It could then read the host clock, because the `clock_gettime` vDSO cannot be
  filtered by namespaces or seccomp, and open files inside its own read-only view. The clock guarantee
  is therefore a layer 2 guarantee only.
- The runner removes `sys.monitoring` from the `sys` module the script can reach, so the script cannot
  free or replace the memory callback. A script that calls `throw` on an exhausted generator with its
  own `MemoryError` raises it at a call site, exactly where a failed allocation raises it, so that one
  case still ends its own turn with `LIMIT memory_mb`. Memory stays bounded either way, by the
  address-space cap and the kernel's resident-memory sampling.
- A script can write raw frames on its RPC channel. The kernel validates every frame. A forged `limit`
  frame lacks the turn's token and ends the script's own turn as `INVALID` naming `sandbox.rpc`.
- The process count is bounded by the one-process seccomp filter, not by `RLIMIT_NPROC`, which
  counts per host uid and would throttle the kernel's own user. The filter exists for x86_64 and
  aarch64. On any other architecture the kernel refuses to start a sandbox.
- `RLIMIT_AS` caps address space, which is stricter than resident memory. An interpreter whose
  mappings alone approach `memory_mb` cannot run. Python 3.12 with the SDK maps about 25 MiB.
- No cgroup is created, and the host's cgroup tree is left untouched. Memory is bounded per process
  and per scratch tmpfs, as stated above, not by a cgroup. Any future mechanism, such as cgroups v2
  or a WebAssembly host, must satisfy CWR-007.
