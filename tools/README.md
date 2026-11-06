<!-- Copyright Sidiora Labs Inc. CWR® 2026-2027 -->
<!-- SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0 -->

# Repository tooling

These tools keep the repository consistent and package releases. Each one is Python 3.12, standard
library only and safe under `python3 -I`. A tool locates the repository from its own path, so it runs
from any directory.

| Tool | Run by | Purpose |
| --- | --- | --- |
| `header.py` | `make headers`, `make headers-apply` | Checks or inserts the two-line Sidiora source header in the comment syntax of each file. The exempt file types and directories are listed explicitly in the tool, and the check reports how many files it skipped. |
| `codemap.py` | by hand after a structural change | Generates [CODEMAP.md](../CODEMAP.md) from the tree: a language table, a purpose for every directory taken from the first heading of its README, the crates and the make targets. The output is deterministic. `--check` fails when the map is stale. |
| `release.py` | `make release` | Checks the release preconditions and writes a byte-reproducible bundle into [artifacts/](../artifacts/README.md). It refuses, listing every reason, when the toolchain is incomplete, the versions of the CLI crate, the SDK package and `benchmark/release.kvx` disagree, the release is not pinned or a file selected for the bundle looks like a credential. |
| `cli_repro.py` | by hand before pinning the CLI | Builds the release `cwr` binary twice, from two checkout paths, under two `CARGO_HOME` values and into two target directories, with the flags [RELEASE.md](../RELEASE.md) gives, and fails unless both binaries are identical and embed no host path. Needs a populated crate registry and takes two release builds. |

```sh
python3 tools/header.py --check [PATH ...]
python3 tools/codemap.py [--check | --stdout]
python3 tools/release.py [--snapshot] [--dry-run] [--output DIR]
python3 tools/cli_repro.py [--keep]
```

`release.py` checks packaging only. Release pins, evidence closure and owner acceptance are checked
by the procedure in [RELEASE.md](../RELEASE.md).

A directory README's first heading becomes that directory's purpose in CODEMAP.md, so a heading
should name what the directory holds.

## Tests

```sh
python3 -m unittest discover -s tools/tests -t .
```
