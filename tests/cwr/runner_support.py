# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Revision digests and process-group execution shared by tests/cwr/qualify.py and tests/cwr/closure.py.

Revision digests. A record names the revision of every input of the product under test, so closure
can refuse evidence produced from a tree that is no longer current. Each digest covers the files git
tracks under its paths, at their working-tree content: a tracked file edited but not committed
changes the digest, an untracked file (an editor swap file, a scratch log) never does. The digest of
one file is its git blob id computed from the working tree, so the same content gives the same
digest whether it is committed, staged or only edited. Outside a git checkout (a temporary fixture
root) the digest walks the paths instead and skips build output, caches and editor files. The
digest is a function of path and content only: it does not depend on timestamps, permissions, the
working directory or the order the file system lists entries in.

Process groups. A driver starts kernels, sandbox workers, stub servers and cargo. run_group starts a
program as the leader of a new session and, however the wait ends (normal exit, timeout, an
exception or an interrupt), terminates the whole process group: SIGTERM first, SIGKILL after a grace
period. No child outlives the call.

The independent checker tests/cwr/check_evidence.py never imports this module.
"""

from __future__ import annotations

import hashlib
import os
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

# Revision name -> repository paths (directories or files) whose tracked content it digests.
REVISION_PATHS: dict[str, tuple[str, ...]] = {
    "compiler_revision": ("crates/cwr-compiler",),
    "kernel_revision": ("crates/cwr-kernel",),
    "sdk_revision": ("sdk",),
    "product_revision": (
        "crates",
        "Cargo.toml",
        "Cargo.lock",
        "rust-toolchain",
        "rust-toolchain.toml",
        "sdk",
        "vendor",
    ),
    "harness_revision": (
        "benchmark/harness",
        "benchmark/stubs",
        "benchmark/predicates",
        "benchmark/analyse.py",
    ),
}
# Revisions closure compares for every PASS record, and the extra ones for cases whose evidence also
# depends on the benchmark harness, stubs and predicates.
PRODUCT_REVISIONS = ("compiler_revision", "kernel_revision", "sdk_revision", "product_revision")
HARNESS_TIERS = {"integration", "campaign"}
SKIP_PARTS = {"target", "__pycache__", ".git", ".ruff_cache", ".pytest_cache"}
SKIP_SUFFIXES = (".swp", ".swo", ".pyc", ".pyo", ".orig", ".rej", ".tmp", "~")
GRACE_SECONDS = 5.0


def git_blob_id(data: bytes) -> str:
    """The git object id of a blob with this content (what `git hash-object` prints)."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data, usedforsecurity=False).hexdigest()


def _content(path: Path) -> bytes:
    if path.is_symlink():
        return os.readlink(path).encode()
    return path.read_bytes()


def _git(root: Path, *args: str) -> bytes | None:
    try:
        process = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            timeout=120,
            check=False,
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return process.stdout if process.returncode == 0 else None


def _is_checkout(root: Path) -> bool:
    top = _git(root, "rev-parse", "--show-toplevel")
    return top is not None and Path(top.decode().strip()).resolve() == root.resolve()


def _tracked_blobs(root: Path, paths: tuple[str, ...]) -> dict[str, str]:
    """path -> blob id of the working-tree content of every tracked file under paths."""
    listing = _git(root, "ls-files", "-s", "-z", "--", *paths) or b""
    modified = set((_git(root, "ls-files", "-m", "-z", "--", *paths) or b"").split(b"\0"))
    blobs: dict[str, str] = {}
    for item in listing.split(b"\0"):
        if not item:
            continue
        meta, _, name = item.partition(b"\t")
        blob = meta.split(b" ")[1].decode()
        rel = name.decode("utf-8", errors="surrogateescape")
        path = root / rel
        if name in modified:
            blob = git_blob_id(_content(path)) if path.exists() or path.is_symlink() else "deleted"
        blobs[rel] = blob
    return blobs


def _walked_blobs(root: Path, paths: tuple[str, ...]) -> dict[str, str]:
    blobs: dict[str, str] = {}
    for rel in paths:
        base = root / rel
        candidates = [base] if base.is_file() else sorted(base.rglob("*")) if base.is_dir() else []
        for path in candidates:
            name = path.relative_to(root).as_posix()
            if (
                not path.is_file()
                or SKIP_PARTS & set(Path(name).parts)
                or name.endswith(SKIP_SUFFIXES)
            ):
                continue
            blobs[name] = git_blob_id(_content(path))
    return blobs


def revisions(root: Path) -> dict[str, str | None]:
    """Every revision of REVISION_PATHS for the tree at root; None when no file is under its paths."""
    root = Path(root)
    union = tuple(sorted({p for paths in REVISION_PATHS.values() for p in paths}))
    blobs = _tracked_blobs(root, union) if _is_checkout(root) else _walked_blobs(root, union)
    out: dict[str, str | None] = {}
    for name, paths in REVISION_PATHS.items():
        lines = [
            f"{rel}\0{blob}\n"
            for rel, blob in sorted(blobs.items())
            if any(rel == p or rel.startswith(p.rstrip("/") + "/") for p in paths)
        ]
        out[name] = (
            "sha256:" + hashlib.sha256("".join(lines).encode()).hexdigest() if lines else None
        )
    return out


def compared_revisions(tier: str | None) -> tuple[str, ...]:
    """The revisions whose change makes a PASS record of a case of this tier stale."""
    if tier in HARNESS_TIERS:
        return (*PRODUCT_REVISIONS, "harness_revision")
    return PRODUCT_REVISIONS


@dataclass
class GroupResult:
    """How a process-group run ended: exit code (None on timeout), whether it timed out, output."""

    returncode: int | None
    timed_out: bool
    stdout: str | bytes | None
    stderr: str | bytes | None


def _signal_group(pgid: int, signum: int) -> bool:
    try:
        os.killpg(pgid, signum)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def reap_group(process: subprocess.Popen) -> None:
    """Terminate every process of the group led by process: SIGTERM, a grace period, then SIGKILL."""
    pgid = process.pid
    if not _signal_group(pgid, signal.SIGTERM):
        if process.poll() is None:
            process.kill()
        process.wait()
        return
    deadline = time.monotonic() + GRACE_SECONDS
    while process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.05)
    _signal_group(pgid, signal.SIGKILL)
    process.wait()


def run_group(
    command: list[str],
    *,
    cwd: Path,
    env: dict,
    timeout: float | None,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text: bool = False,
) -> GroupResult:
    """Run command as the leader of a new session with stdin closed; the whole group is always reaped."""
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=stdout,
        stderr=stderr,
        text=text,
        start_new_session=True,
    )
    try:
        out, err = process.communicate(timeout=timeout)
        return GroupResult(process.returncode, False, out, err)
    except subprocess.TimeoutExpired:
        reap_group(process)
        out, err = process.communicate()
        return GroupResult(None, True, out, err)
    finally:
        reap_group(process)


def raise_on_sigterm() -> None:
    """Turn SIGTERM into SystemExit so finally blocks run and reap child process groups."""

    def handler(signum, _frame):
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, handler)
