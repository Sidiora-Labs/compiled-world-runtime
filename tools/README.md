<!-- Copyright Sidiora Labs Inc. CWR® 2026-2027 -->
<!-- SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0 -->

# tools: repository tooling

Python 3.12, standard library only, safe under `python3 -I`. Run from anywhere; each tool locates
the repository from its own path.

| Tool | Used by | Does |
|---|---|---|
| `header.py` | `make headers`, `make headers-apply` | Checks or inserts the two-line Sidiora source header in the comment syntax of each file; the exemption list (JSON, lock files, `bin`/`rules` task data, recorded data directories) is explicit in the file. |
| `codemap.py` | by hand after structural changes | Generates [CODEMAP.md](../CODEMAP.md): language table, directory purposes from READMEs, crates from `Cargo.toml`, targets from the `Makefile`. `--check` fails when the map is stale. Output is deterministic. |
| `release.py` | `make release` | Refuses unless the `make check` toolchain and inputs are present, the versions in `crates/cwr-cli/Cargo.toml`, `sdk/python/pyproject.toml` and `benchmark/release.kvx` agree, and the release is pinned; then writes a byte-reproducible tarball and sha256 manifest into [artifacts/](../artifacts/README.md). `--dry-run` checks only; `--snapshot` bundles an unpinned tree under a `-snapshot` name. |

```sh
python3 tools/header.py --check [PATH ...]
python3 tools/codemap.py [--check | --stdout]
python3 tools/release.py [--snapshot] [--dry-run] [--output DIR]
```

`release.py` reads KVX through [core/cwr_core](../core/README.md). It does not check release pins,
closure or owner acceptance; those belong to the release driver (task 6.1) and closure (task 6.2)
described in [RELEASE.md](../RELEASE.md).

## Tests

```sh
python3 -m unittest discover -s tools/tests -t .
```
