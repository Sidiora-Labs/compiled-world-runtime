# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""CWR sandbox runner: executes exactly one turn script inside the per-turn sandbox.

Spec: spec/cwr/kernel.md sections 3 to 5, spec/cwr/model-surface.md section 4; task 2.2, case CWR-007.
The kernel embeds this file byte-for-byte (crates/cwr-kernel/src/sandbox.rs, ``include_str!``) and starts it
as ``python3.12 -P -s -S -B -c <this file>`` inside the bubblewrap view described in
crates/cwr-kernel/SANDBOX.md. It is not part of a Build, is never imported by a script, and holds no rule:
every rule is enforced by the kernel on the other side of the RPC socket.

Protocol: frames of ``cwr._rpc`` (4-byte big-endian length, canonical JSON) over the inherited unix socket
named by ``CWR_RPC_FD``:

* kernel -> runner, first frame: ``{"v": 1, "op": "start", "script": str, "modules": [str, ...]}``
* runner -> kernel: primitive requests made by the generated SDK (``cwr._rpc.call``), unchanged.
* runner -> kernel: ``{"v": 1, "op": "refuse", "id": n, "facility": "sandbox.<what>", "detail": str}``, a
  closure refusal (an import outside the SDK, a file, socket, process, clock or interpreter facility); the
  kernel records it in the ledger and answers like a primitive call with an INVALID failure, which this
  runner raises as the typed exception (also an ImportError or PermissionError, so ordinary handlers work).
* runner -> kernel: ``{"v": 1, "op": "limit", "limit": "memory_mb"}`` when an allocation exceeded the address
  space cap; the runner then waits to be killed.
* runner -> kernel, last frame: ``{"v": 1, "op": "end", "outcome": "ok" | "raised" | "unencodable",
  "returned": <codec value> | null, "failure": <failure> | null, "exception": str | null}``.

Language-level layer (the kernel-level layer, namespaces and rlimits, is set up before this file runs):

1. Only ``/cwr/sdk`` (the Build's generated SDK) and the interpreter's stdlib are on ``sys.path`` while the SDK
   is imported; afterwards a script may import only the Build's world modules, ``cwr`` and ``cwr.errors``.
2. The RPC channel is handed to the SDK as a file object, not a socket, so the script has no socket API.
3. Every callable of ``posix``/``os``, ``time``, ``_thread``, ``_signal`` and ``_imp`` is replaced by a
   refusing stub, in its module and in every container that captured it before the guard (for example
   ``threading._time``, the sets of ``os.supports_fd``, closure cells), so a reference reached by walking
   the object graph is refused too. If any reference cannot be rewritten, the script is not run.
4. ``builtins.__import__`` and ``builtins.open`` are replaced; an audit hook (which cannot be removed) refuses
   file, socket, process, clock, frame-introspection and module-load events.
5. A ``sys.monitoring`` RAISE callback turns any MemoryError into ``LIMIT memory_mb`` even if the script
   would catch it.
"""


def _cwr_main():  # everything lives in this frame so that module globals expose nothing
    import _io
    import builtins
    import contextlib
    import json
    import posix
    import sys

    fd = int(posix.environ.get(b"CWR_RPC_FD", b"3"))
    sys.path[:] = ["/cwr/sdk"] + [p for p in sys.path if p and not p.startswith("/cwr")]

    from cwr import _codec, _rpc, errors

    class _Chan:
        """The RPC channel as the SDK sees it: sendall and recv over a raw file object, no socket API."""

        __slots__ = ("_f",)

        def __init__(self, f):
            self._f = f

        def sendall(self, data):
            view = memoryview(data)
            while view:
                n = self._f.write(view)
                if not n:
                    raise _rpc.RpcError("RPC channel closed while sending")
                view = view[n:]

        def recv(self, n):
            return self._f.read(n) or b""

        def close(self):
            self._f.close()

    chan = _Chan(_io.FileIO(fd, "r+", closefd=True))
    _rpc.install(chan)

    def send(obj):
        _rpc.send_frame(
            chan,
            json.dumps(
                obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
            ).encode("utf-8"),
        )

    start = json.loads(_rpc.recv_frame(chan).decode("utf-8"))
    if start.get("op") != "start" or start.get("v") != 1:
        raise SystemExit(70)
    script = start["script"]
    modules = list(start.get("modules") or [])
    guard_enabled = bool(start.get("guard", True))

    # Import the Build's world modules from the SDK directory only.
    import importlib.util

    world = {}
    for m in modules:
        spec = importlib.util.spec_from_file_location(m, f"/cwr/sdk/{m}.py")
        if spec is None:
            continue
        try:
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
        except OSError:
            continue
        sys.modules[m] = mod
        world[m] = mod

    # Compile the script as the body of one function so that a top-level ``return`` is its result.
    import ast

    code = None
    compile_error = None
    try:
        tree = ast.parse(script, filename="<turn>", mode="exec")
        future = [
            n for n in tree.body if isinstance(n, ast.ImportFrom) and n.module == "__future__"
        ]
        body = [n for n in tree.body if n not in future] or [ast.Pass()]
        fn = ast.FunctionDef(
            name="__cwr_turn__",
            args=ast.arguments(
                posonlyargs=[],
                args=[],
                vararg=None,
                kwonlyargs=[],
                kw_defaults=[],
                kwarg=None,
                defaults=[],
            ),
            body=body,
            decorator_list=[],
            returns=None,
            type_params=[],
            lineno=1,
            col_offset=0,
        )
        module = ast.Module(body=[*future, fn], type_ignores=[])
        ast.fix_missing_locations(module)
        code = compile(module, "<turn>", "exec")
    except SyntaxError as e:
        compile_error = f"SyntaxError: {e.msg} (line {e.lineno})"
    except (ValueError, TypeError, RecursionError, MemoryError) as e:
        compile_error = f"{type(e).__name__}: {e}"

    # Codecs a script may name, loaded now because loading a module is refused once the guard is armed.
    import encodings.ascii
    import encodings.latin_1  # noqa: F401  imported for its side effect of registering the codec

    # ------------------------------------------------------------------ closure refusals

    busy = [False]
    refusal_classes = {}

    def refusal_class(cls, py_base):
        key = (cls, py_base)
        if key not in refusal_classes:
            refusal_classes[key] = type(
                "Refused" + cls.__name__, (cls, py_base), {"__module__": "cwr.errors"}
            )
        return refusal_classes[key]

    def refuse(facility, detail, py_base=PermissionError):
        """Report a closure refusal to the kernel (it is recorded in the ledger) and raise it typed."""
        if busy[0]:
            raise PermissionError(f"{facility} refused by the CWR sandbox")
        busy[0] = True
        try:
            rid = _rpc._next_id
            _rpc._next_id = rid + 1
            send(
                {
                    "v": 1,
                    "op": "refuse",
                    "id": rid,
                    "facility": facility,
                    "detail": str(detail)[:200],
                }
            )
            response = json.loads(_rpc.recv_frame(chan).decode("utf-8"))
            failure = response["result"]["error"]
        finally:
            busy[0] = False
        exc = errors.from_failure(failure)
        try:
            typed = refusal_class(type(exc), py_base)(
                failure["primitive"],
                failure.get("precondition_index"),
                failure.get("retry_after"),
                failure.get("limit"),
                failure.get("detail", ""),
            )
        except TypeError:
            typed = exc
        raise typed

    def stub(facility, name):
        def refused(*args, **kwargs):
            refuse(facility, name)

        refused.__name__ = name
        refused.__qualname__ = name
        return refused

    allowed_imports = {"cwr", "cwr.errors"} | set(world)
    real_import = builtins.__import__

    def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
        if level:
            package = (globals or {}).get("__package__") or ""
            if package == "cwr" or package.startswith("cwr."):
                return real_import(name, globals, locals, fromlist, level)
            full = name
        else:
            full = name
        top = full.split(".")[0] if full else full
        if full in allowed_imports or (top == "cwr" and full in allowed_imports):
            mod = real_import(name, globals, locals, fromlist, level)
            for item in fromlist or ():
                if (
                    item != "*"
                    and full == "cwr"
                    and "cwr." + item not in allowed_imports
                    and not hasattr(errors, item)
                    and item not in ("errors",)
                ):
                    refuse("sandbox.import", "cwr." + item, ImportError)
            return mod
        refuse("sandbox.import", full or "<relative>", ImportError)

    # The events of PEP 578 refused once the script runs, by prefix, with the facility each is reported as.
    deny = (
        ("open", "sandbox.filesystem"),
        ("os.system", "sandbox.process"),
        ("os.exec", "sandbox.process"),
        ("os.fork", "sandbox.process"),
        ("os.posix_spawn", "sandbox.process"),
        ("os.spawn", "sandbox.process"),
        ("os.kill", "sandbox.process"),
        ("os.startfile", "sandbox.process"),
        ("os.", "sandbox.filesystem"),
        ("subprocess.", "sandbox.process"),
        ("pty.", "sandbox.process"),
        ("_posixsubprocess", "sandbox.process"),
        ("signal.", "sandbox.process"),
        ("resource.", "sandbox.process"),
        ("syslog.", "sandbox.process"),
        ("socket.", "sandbox.network"),
        ("urllib.", "sandbox.network"),
        ("http.", "sandbox.network"),
        ("ftplib.", "sandbox.network"),
        ("smtplib.", "sandbox.network"),
        ("poplib.", "sandbox.network"),
        ("imaplib.", "sandbox.network"),
        ("nntplib.", "sandbox.network"),
        ("telnetlib.", "sandbox.network"),
        ("webbrowser.", "sandbox.network"),
        ("shutil.", "sandbox.filesystem"),
        ("tempfile.", "sandbox.filesystem"),
        ("glob.", "sandbox.filesystem"),
        ("fcntl.", "sandbox.filesystem"),
        ("mmap.", "sandbox.filesystem"),
        ("sqlite3.", "sandbox.filesystem"),
        ("time.", "sandbox.clock"),
        ("ctypes.", "sandbox.interpreter"),
        ("sys._getframe", "sandbox.interpreter"),
        ("sys._current_frames", "sandbox.interpreter"),
        ("sys._current_exceptions", "sandbox.interpreter"),
        ("sys.settrace", "sandbox.interpreter"),
        ("sys.setprofile", "sandbox.interpreter"),
        ("sys.addaudithook", "sandbox.interpreter"),
        ("sys.remote_exec", "sandbox.interpreter"),
        ("object.__getattr__", "sandbox.interpreter"),
        ("gc.", "sandbox.interpreter"),
        ("marshal.", "sandbox.interpreter"),
        ("code.__new__", "sandbox.interpreter"),
        ("function.__new__", "sandbox.interpreter"),
        ("cpython.run_", "sandbox.interpreter"),
        ("builtins.input", "sandbox.interpreter"),
        ("builtins.breakpoint", "sandbox.interpreter"),
    )
    armed = [False]

    def audit(event, args):
        if not armed[0] or busy[0]:
            return
        if event == "import":
            name = args[0] if args else ""
            refuse("sandbox.import", name, ImportError)
        for prefix, facility in deny:
            if event.startswith(prefix):
                what = event
                if args and isinstance(args[0], (str, bytes, int)):
                    what = f"{event} {args[0]!r}"
                refuse(
                    facility, what, ImportError if facility == "sandbox.import" else PermissionError
                )

    def neuter_all(neuter):
        """Replace every callable of the given modules, wherever it is referenced, by a refusing stub.

        Rebinding a module's attributes is not enough: a module that ran ``from time import monotonic``
        (``threading._time``), filled a set with ``os`` callables (``os.supports_fd``) or closed over one
        still holds the original. Every container the garbage collector tracks that refers to an
        original is rewritten to hold the stub: dicts, lists, sets and cells in place, tuples and
        frozensets by rebuilding them and rewriting their own holders (function defaults included).
        Returns a description of every reference that could not be rewritten; the runner then refuses to
        run the script rather than run it with a host facility reachable.
        """
        import gc

        replaced = {}
        for mod, facility in neuter:
            if mod is None:
                continue
            for name, value in list(vars(mod).items()):
                if name.startswith("__") or isinstance(value, type) or not callable(value):
                    continue
                new = replaced[id(value)][1] if id(value) in replaced else stub(facility, name)
                with contextlib.suppress(AttributeError, TypeError):
                    setattr(mod, name, new)
                    replaced[id(value)] = (value, new)
                del value, new
        frames = set()
        f = sys._getframe()
        while f is not None:
            frames.add(id(f))
            f = f.f_back
        mapping = {k: new for k, (_old, new) in replaced.items()}
        originals = [old for old, _new in replaced.values()]
        replaced.clear()
        rebuilt = []
        work = list(originals)
        batches = []
        bookkeeping = {id(mapping), id(originals), id(rebuilt), id(work), id(batches)}

        def holders(obj):
            return [
                h
                for h in gc.get_referrers(obj)
                if id(h) not in bookkeeping and id(h) not in frames and h is not obj
            ]

        rounds = 0
        while work and rounds < 8:
            rounds += 1
            batch = list(work)
            work.clear()
            batches.append(batch)
            bookkeeping.add(id(batch))
            for old in batch:
                new = mapping[id(old)]
                for h in holders(old):
                    kind = type(h)
                    if kind is dict:
                        for k, v in list(h.items()):
                            if v is old:
                                h[k] = new
                    elif kind is list:
                        for i, v in enumerate(h):
                            if v is old:
                                h[i] = new
                    elif kind is set:
                        h.discard(old)
                        h.add(new)
                    elif kind.__name__ == "cell":
                        h.cell_contents = new
                    elif kind is tuple or kind is frozenset:
                        if id(h) not in mapping:
                            mapping[id(h)] = kind(mapping.get(id(v), v) for v in h)
                            rebuilt.append(h)
                            work.append(h)
                    elif kind.__name__ == "function" and h.__defaults__ is old:
                        h.__defaults__ = new
        leftover = set()
        if work:
            leftover.add("references nested deeper than 8 containers")
        checked = originals + rebuilt
        bookkeeping.add(id(checked))
        for old in checked:
            for h in holders(old):
                if type(h) in (tuple, frozenset) and id(h) in mapping:
                    continue
                name = getattr(old, "__qualname__", type(old).__name__)
                leftover.add(f"{name} held by {type(h).__name__}")
        for held in (mapping, originals, rebuilt, batches, checked):
            held.clear()
        return leftover

    # ------------------------------------------------------------------ MemoryError is LIMIT memory_mb

    limit_payload = json.dumps(
        {"v": 1, "op": "limit", "limit": "memory_mb"}, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    limit_frame = len(limit_payload).to_bytes(4, "big") + limit_payload

    def on_raise(code_obj, offset, exc):
        if isinstance(exc, MemoryError):
            try:
                chan.sendall(limit_frame)
                while chan.recv(65536):
                    pass
            finally:
                raise SystemExit(71)

    monitoring = sys.monitoring
    tool = 4
    try:
        monitoring.use_tool_id(tool, "cwr-sandbox")
        monitoring.register_callback(tool, monitoring.events.RAISE, on_raise)
        monitoring.set_events(tool, monitoring.events.RAISE)
    except ValueError:
        pass

    # ------------------------------------------------------------------ arm the guard

    class _Sink:
        def write(self, s):
            return len(s)

        def flush(self):
            pass

    sys.stdout = _Sink()
    sys.stderr = _Sink()

    if guard_enabled:
        import _imp
        import _signal
        import _thread

        # Imported only to be neutered below, so that no reference to it reveals host time.
        import time as _time_mod

        environ = getattr(sys.modules.get("os"), "environ", None)
        if environ is not None:
            environ.clear()  # before neutering: clearing calls posix.unsetenv
        neuter = (
            (posix, "sandbox.os"),
            (sys.modules.get("os"), "sandbox.os"),
            (sys.modules.get("time"), "sandbox.clock"),
            (_thread, "sandbox.thread"),
            (_signal, "sandbox.process"),
            (_imp, "sandbox.import"),
        )
        leftover = neuter_all(neuter)
        clock = sys.modules.get("time")
        if clock is not None:
            # Host facts the clock module carries as data: the timezone is reported as UTC.
            for name, value in (("timezone", 0), ("altzone", 0), ("daylight", 0)):
                setattr(clock, name, value)
            clock.tzname = ("UTC", "UTC")
            del clock, name, value
        if leftover:
            compile_error = "sandbox closure incomplete: " + ", ".join(sorted(leftover))[:300]
            code = None
        for name in ("os", "socket"):
            _rpc.__dict__.pop(name, None)
        for name in (
            "os",
            "posix",
            "time",
            "_thread",
            "_signal",
            "socket",
            "_socket",
            "ast",
            "_ast",
            "importlib",
            "importlib.util",
            "importlib._bootstrap",
            "importlib._abc",
            "importlib.machinery",
            "io",
            "_io",
            "__main__",
        ):
            sys.modules.pop(name, None)
        builtins.__import__ = guarded_import
        builtins.open = stub("sandbox.filesystem", "open")
        builtins.input = stub("sandbox.interpreter", "input")
        builtins.breakpoint = stub("sandbox.interpreter", "breakpoint")
        del posix, _io, _signal, _thread, _imp, _time_mod
        sys.addaudithook(audit)
        armed[0] = True

    # ------------------------------------------------------------------ run the script

    outcome, returned, failure, exception = "ok", None, None, None
    if compile_error is not None:
        outcome, exception = "raised", compile_error
    else:
        namespace = {"__name__": "__turn__", "__builtins__": builtins}
        try:
            exec(code, namespace)
            result = namespace.pop("__cwr_turn__")()
        except BaseException as e:  # every escape is reported; nothing reaches the host
            outcome = "raised"
            if isinstance(e, MemoryError):
                on_raise(None, 0, e)
            if isinstance(e, errors.WorldError):
                failure = {
                    "code": e.code,
                    "primitive": e.primitive,
                    "precondition_index": e.precondition_index,
                    "retry_after": e.retry_after,
                    "limit": e.limit,
                    "detail": e.detail,
                }
            try:
                exception = f"{type(e).__name__}: {e}"[:500]
            except BaseException:  # str(e) itself raised: report the type alone
                exception = type(e).__name__
        else:
            encoded = _codec.encode(result)

            def representable(v):
                if isinstance(v, bool) or v is None or isinstance(v, str):
                    return True
                if isinstance(v, int):
                    return -(2**63) <= v < 2**64
                if isinstance(v, float):
                    return True
                if isinstance(v, list):
                    return all(representable(x) for x in v)
                if isinstance(v, dict):
                    if "$invalid" in v and len(v) == 1:
                        return False
                    return all(representable(x) for x in v.values())
                return False

            try:
                ok = representable(encoded)
            except RecursionError:
                ok = False
            if ok:
                returned = encoded
            else:
                outcome, exception = (
                    "unencodable",
                    f"returned value of type {type(result).__name__} is outside the SDK codec",
                )
    armed[0] = False
    send(
        {
            "v": 1,
            "op": "end",
            "outcome": outcome,
            "returned": returned,
            "failure": failure,
            "exception": exception,
        }
    )
    # Interpreter shutdown would call threading's neutered callables; the turn is over, skip it.
    sys.modules.pop("threading", None)


_cwr_main()
