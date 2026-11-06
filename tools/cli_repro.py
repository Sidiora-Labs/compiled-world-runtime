#!/usr/bin/env python3
# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Prove that the release `cwr` binary is byte-reproducible across host paths.

Usage:
    python3 tools/cli_repro.py [--keep]

Builds the `cwr-cli` release binary twice with the command RELEASE.md gives, each time from a
different copy of the source tree, under a different CARGO_HOME (the second one a copy of the
first one's registry) and into a different target directory, offline and with the locked
dependencies. Prints the sha256 of both binaries and whether either embeds a host path.

Exit code 0 when the two binaries are identical and embed neither checkout path nor either
CARGO_HOME, 1 when they differ or a host path is embedded, 2 when a build fails or a usage
error occurs. `--keep` leaves the scratch directory in place and prints its path.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# The parts of the tree a `cwr-cli` build reads.
TREE = ("Cargo.toml", "Cargo.lock", "rustfmt.toml", "crates", "sdk")


def copy_tree(dst: Path) -> None:
    """Copy the build inputs of the repository into `dst`, without build outputs."""
    dst.mkdir(parents=True)
    for name in TREE:
        src = ROOT / name
        if src.is_dir():
            shutil.copytree(src, dst / name, ignore=shutil.ignore_patterns("target", "__pycache__"))
        elif src.is_file():
            shutil.copy2(src, dst / name)


def build(tree: Path, cargo_home: Path, target: Path) -> Path:
    """Build the release CLI as RELEASE.md does and return the binary path."""
    env = dict(os.environ)
    env["CARGO_HOME"] = str(cargo_home)
    env["CARGO_TARGET_DIR"] = str(target)
    env["RUSTFLAGS"] = f"--remap-path-prefix={cargo_home}=/cargo --remap-path-prefix={tree}=/cwr"
    cmd = ["cargo", "build", "--release", "--locked", "--offline", "-p", "cwr-cli"]
    proc = subprocess.run(cmd, cwd=tree, env=env, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        tail = "\n".join(proc.stderr.splitlines()[-20:])
        raise RuntimeError(f"cargo build failed in {tree} (exit {proc.returncode}):\n{tail}")
    return target / "release" / "cwr"


def sha256(path: Path) -> str:
    """The sha256 of the file at `path` as lowercase hex."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def embeds(binary: Path, needles: list[str]) -> list[str]:
    """The needles that occur in the bytes of `binary`."""
    data = binary.read_bytes()
    return [n for n in needles if n.encode() in data]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--keep", action="store_true", help="keep the scratch directory")
    args = parser.parse_args()
    home = Path(os.environ.get("CARGO_HOME", Path.home() / ".cargo"))
    registry = home / "registry"
    if not registry.is_dir():
        print(f"cli_repro: no crate registry under {home}; fetch the dependencies first")
        return 2
    scratch = Path(tempfile.mkdtemp(prefix="cwr-cli-repro-"))
    try:
        tree_a, tree_b = scratch / "a" / "src", scratch / "b-longer" / "checkout"
        copy_tree(tree_a)
        copy_tree(tree_b)
        home_b = scratch / "cargo-home-b"
        home_b.mkdir()
        shutil.copytree(registry, home_b / "registry", symlinks=True)
        bin_a = build(tree_a, home, scratch / "target-a")
        bin_b = build(tree_b, home_b, scratch / "target-b")
        sum_a, sum_b = sha256(bin_a), sha256(bin_b)
        paths = [str(tree_a), str(tree_b), str(home), str(home_b)]
        leaked = sorted(set(embeds(bin_a, paths) + embeds(bin_b, paths)))
        print(f"build a: sha256:{sum_a}")
        print(f"build b: sha256:{sum_b}")
        print(f"embedded host paths: {leaked if leaked else 'none'}")
        same = sum_a == sum_b and not leaked
        print("reproducible" if same else "NOT reproducible")
        return 0 if same else 1
    except (RuntimeError, OSError) as e:
        print(f"cli_repro: {e}")
        return 2
    finally:
        if args.keep:
            print(f"scratch kept at {scratch}")
        else:
            shutil.rmtree(scratch, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
