#!/usr/bin/env python3
# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Check release preconditions and bundle a reproducible CWR release tarball into artifacts/.

Usage:
    python3 tools/release.py [--snapshot] [--dry-run] [--output DIR]

`make release` runs this after `check`, `test` and `qualify`. It refuses, listing every reason, when:

  * a `make check` prerequisite is missing: Python 3.12+, cargo, rustfmt, clippy and ruff that run,
    and the inputs make check reads (tools/header.py, the spec validator, ruff.toml, rustfmt.toml,
    Cargo.toml, Cargo.lock);
  * the versions declared by crates/cwr-cli/Cargo.toml (resolving `version.workspace`),
    sdk/python/pyproject.toml ([project] version) and benchmark/release.kvx ([meta] release)
    disagree or are malformed; a file that does not exist is skipped;
  * benchmark/release.kvx exists and its [meta] release is null (unpinned), unless --snapshot;
  * a file selected for the bundle looks like a credential (.env, keys, certificates);
  * a bundle of the same name already exists with different content.

The bundle is artifacts/cwr-<version>[-snapshot].tar.gz: every non-ignored file of the tree under a
cwr-<version>/ prefix (vendor/cargo when `make vendor` has run; hidden paths other than
.cargo/config.toml and the repository dotfiles excluded), the release `cwr`
binary as bin/cwr when it has been built, and MANIFEST.sha256 listing the sha256 of every file in
`sha256sum -c` format. Next to it, <bundle>.sha256 holds the tarball's own digest. The archive is
byte-reproducible: sorted entries, zero timestamps, owner and group 0, normalised modes.

A bundle is not evidence. Pins, closure and owner acceptance are checked by the release driver
(task 6.1) and closure (task 6.2), not here. Exit codes: 0 bundled (or checks passed under
--dry-run), 1 refused, 2 usage error.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "core"))

from cwr_core import kvx  # noqa: E402

# make check runs ruff with target-version py312; an older interpreter is refused at runtime.
MIN_PYTHON = (3, 12)
VERSION_RE = re.compile(r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?")
CHECK_INPUTS = (
    "tools/header.py",
    "spec/cwr/tools/validate_spec.py",
    "ruff.toml",
    "rustfmt.toml",
    "Cargo.toml",
    "Cargo.lock",
)
# Hidden files that belong in a source bundle (the cargo source switch to vendor/ included);
# every other hidden path stays out.
ALLOWED_HIDDEN = {
    ".cargo/config.toml",
    ".editorconfig",
    ".gitattributes",
    ".gitignore",
    ".gitmodules",
    ".markdownlint.yaml",
}
SECRET_RE = re.compile(r"(^|/)(\.env(\..*)?|[^/]*\.env|[^/]*\.(key|pem|p12|crt)|secrets/.*)$")
EXCLUDED_TOP = {"artifacts", "target", "build", "xbuild", "out"}


@dataclass(frozen=True)
class Declared:
    """A version string as declared by one source file, or None when it declares none."""

    source: str
    version: str | None


def run_ok(command: list[str]) -> bool:
    """True when the command starts and exits 0 within 60 seconds."""
    try:
        result = subprocess.run(command, capture_output=True, timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def check_prerequisites(root: Path) -> list[str]:
    """Return every unmet prerequisite of `make check`."""
    errors: list[str] = []
    if sys.version_info[:2] < MIN_PYTHON:
        errors.append(f"python 3.12 or newer is required, running {sys.version.split()[0]}")
    cargo = os.environ.get("CARGO", "cargo")
    ruff = os.environ.get("RUFF", "ruff")
    tools = {
        "cargo": [cargo, "--version"],
        "rustfmt": [cargo, "fmt", "--version"],
        "clippy": [cargo, "clippy", "--version"],
        "ruff": [ruff, "--version"],
    }
    for name, command in tools.items():
        if shutil.which(command[0]) is None:
            errors.append(f"{name}: {command[0]} not found on PATH")
        elif not run_ok(command):
            errors.append(f"{name}: `{' '.join(command)}` failed")
    errors += [
        f"missing {path} (read by make check)"
        for path in CHECK_INPUTS
        if not (root / path).is_file()
    ]
    return errors


def cli_version(root: Path) -> Declared | None:
    manifest = root / "crates" / "cwr-cli" / "Cargo.toml"
    if not manifest.is_file():
        return None
    version = tomllib.loads(manifest.read_text(encoding="utf-8")).get("package", {}).get("version")
    if isinstance(version, dict) and version.get("workspace") is True:
        workspace = tomllib.loads((root / "Cargo.toml").read_text(encoding="utf-8"))
        version = workspace.get("workspace", {}).get("package", {}).get("version")
    return Declared("crates/cwr-cli/Cargo.toml", version if isinstance(version, str) else None)


def sdk_version(root: Path) -> Declared | None:
    manifest = root / "sdk" / "python" / "pyproject.toml"
    if not manifest.is_file():
        return None
    version = tomllib.loads(manifest.read_text(encoding="utf-8")).get("project", {}).get("version")
    return Declared("sdk/python/pyproject.toml", version if isinstance(version, str) else None)


def pinned_version(root: Path) -> Declared | None:
    pins = root / "benchmark" / "release.kvx"
    if not pins.is_file():
        return None
    release = kvx.load(pins).get("meta", {}).get("release")
    return Declared(
        "benchmark/release.kvx [meta] release", release if isinstance(release, str) else None
    )


def check_versions(root: Path, snapshot: bool) -> tuple[str | None, list[str]]:
    """Return the agreed version and every version error."""
    errors: list[str] = []
    try:
        declared = [d for d in (cli_version(root), sdk_version(root), pinned_version(root)) if d]
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError, kvx.KVXError) as error:
        return None, [f"cannot read a version source: {error}"]
    if not declared:
        return None, [
            "no version source exists (crates/cwr-cli, sdk/python, benchmark/release.kvx)"
        ]
    versions: dict[str, list[str]] = {}
    for item in declared:
        if item.version is None:
            if item.source.startswith("benchmark/release.kvx"):
                if not snapshot:
                    errors.append(f"{item.source} is null: the release is not pinned (task 6.1)")
            else:
                errors.append(f"{item.source} declares no version string")
        elif not VERSION_RE.fullmatch(item.version):
            errors.append(f"{item.source}: {item.version!r} is not MAJOR.MINOR.PATCH[-pre][+build]")
        else:
            versions.setdefault(item.version, []).append(item.source)
    if len(versions) > 1:
        detail = "; ".join(f"{v} in {', '.join(srcs)}" for v, srcs in sorted(versions.items()))
        errors.append(f"versions disagree: {detail}")
    agreed = next(iter(versions)) if len(versions) == 1 else None
    return agreed, errors


def list_tree(root: Path) -> list[str]:
    """Non-ignored files of the tree, as sorted POSIX paths, without hidden or output paths."""
    result = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        capture_output=True,
        check=True,
    )
    selected = []
    for path in result.stdout.decode("utf-8").split("\0"):
        if not path or not (root / path).is_file():
            continue
        parts = path.split("/")
        if parts[0] in EXCLUDED_TOP or "__pycache__" in parts:
            continue
        if any(part.startswith(".") for part in parts) and path not in ALLOWED_HIDDEN:
            continue
        selected.append(path)
    return sorted(selected)


def release_binary() -> Path | None:
    target = Path(os.environ.get("CARGO_TARGET_DIR", "target"))
    if not target.is_absolute():
        target = ROOT / target
    binary = target / "release" / "cwr"
    return binary if binary.is_file() else None


def tar_entry(name: str, data: bytes, executable: bool) -> tuple[tarfile.TarInfo, io.BytesIO]:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mtime = 0
    info.mode = 0o755 if executable else 0o644
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    return info, io.BytesIO(data)


def build_bundle(prefix: str, members: Iterable[tuple[str, Path]]) -> bytes:
    """Return the gzip-compressed tarball bytes for (archive path, source file) members."""
    entries: list[tuple[str, bytes, bool]] = []
    manifest_lines = []
    for name, source in sorted(members):
        data = source.read_bytes()
        executable = os.access(source, os.X_OK)
        entries.append((name, data, executable))
        manifest_lines.append(f"{hashlib.sha256(data).hexdigest()}  {name}")
    entries.append(("MANIFEST.sha256", ("\n".join(manifest_lines) + "\n").encode(), False))
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for name, data, executable in entries:
            archive.addfile(*tar_entry(f"{prefix}/{name}", data, executable))
    compressed = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=compressed, mtime=0, compresslevel=9) as gz:
        gz.write(raw.getvalue())
    return compressed.getvalue()


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--snapshot",
        action="store_true",
        help="allow an unpinned benchmark/release.kvx; names the bundle -snapshot",
    )
    parser.add_argument("--dry-run", action="store_true", help="run every check, write nothing")
    parser.add_argument("--output", default=str(ROOT / "artifacts"), help="bundle directory")
    args = parser.parse_args(argv)

    errors = check_prerequisites(ROOT)
    version, version_errors = check_versions(ROOT, args.snapshot)
    errors += version_errors
    try:
        files = list_tree(ROOT)
    except (OSError, subprocess.CalledProcessError) as error:
        errors.append(f"cannot list the tree with git: {error}")
        files = []
    errors += [
        f"refusing to bundle a credential-like file: {p}" for p in files if SECRET_RE.search(p)
    ]
    if errors:
        print("release refused:", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1

    label = f"cwr-{version}" + ("-snapshot" if args.snapshot else "")
    members = [(path, ROOT / path) for path in files]
    binary = release_binary()
    if binary is not None:
        members.append(("bin/cwr", binary))
    print(f"{label}: {len(members)} files{' including bin/cwr' if binary else ''}")
    if args.dry_run:
        print("dry run: all release checks passed; nothing written")
        return 0

    bundle = build_bundle(label, members)
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    tarball = output / f"{label}.tar.gz"
    if tarball.is_file() and tarball.read_bytes() != bundle:
        print(
            f"release refused: {tarball} exists with different content; "
            "bump the version or remove it deliberately",
            file=sys.stderr,
        )
        return 1
    tarball.write_bytes(bundle)
    digest = hashlib.sha256(bundle).hexdigest()
    (output / f"{label}.tar.gz.sha256").write_text(f"{digest}  {tarball.name}\n", encoding="utf-8")
    print(f"wrote {tarball} sha256:{digest}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
