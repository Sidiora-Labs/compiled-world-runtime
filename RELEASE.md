<!-- Copyright Sidiora Labs Inc. CWR® 2026-2027 -->
<!-- SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0 -->

# Releasing CWR

A CWR release is a source bundle from which every Build, ledger and published result can be
reproduced, together with a release file that pins every input by digest. A release is cut only
when every qualification case has an accepted attempt and the owner has accepted the result. This
document states how versions are numbered, what a release contains, how a release is cut and how
anyone can verify one.

No version has been released yet. Version 0.1.0 is declared, but its inputs are not pinned, the
packaging of the CLI and SDK is not yet fixed and 19 of the 20 qualification cases have no
accepted attempt.

## Versions

A version has the form `MAJOR.MINOR.PATCH`, with an optional pre-release suffix such as `-rc.1`
and an optional build suffix such as `+linux`.

The version is declared in three places, and the three must agree:

| File | Field |
|---|---|
| [Cargo.toml](Cargo.toml) | `[workspace.package] version`, inherited by every crate |
| [sdk/python/pyproject.toml](sdk/python/pyproject.toml) | `[project] version` |
| [benchmark/release.kvx](benchmark/release.kvx) | `[meta] release` |

The release bundler, [tools/release.py](tools/release.py), refuses to run when they disagree, and
`cwr release check` reports the disagreement as CWR-R019.

The size of a version bump follows the rule the World Compiler enforces for world versions
([decision P15](spec/cwr/decisions.md)). A removal or a stricter rule requires a major bump. An
addition or a looser rule requires a minor bump. Any other change requires a patch bump.

While the major version is 0, a minor bump satisfies a required major bump and a patch bump
satisfies a required minor bump. From 1.0.0 the required bump applies literally.

The first stable release at 1.0.0 or higher also converts the license to MIT, as
[LICENSE](LICENSE) states.

## What a release contains

| File | Contents |
|---|---|
| `cwr-<version>.tar.gz` | Every tracked or unignored file of the tree under `cwr-<version>/`, the release `cwr` binary as `bin/cwr` when one has been built, and `MANIFEST.sha256` |
| `cwr-<version>.tar.gz.sha256` | The sha256 of the tarball, in `sha256sum -c` format |
| `MANIFEST.sha256`, inside the tarball | The sha256 of every other file in the bundle, in `sha256sum -c` format |
| `benchmark/release.kvx`, inside the tarball | The pins listed under [Diagnostics](#diagnostics), with closure and owner acceptance |

The tarball includes vendored crate sources under `vendor/cargo` when `make vendor` has run. It
excludes the top-level output directories (`target`, `build`, `xbuild`, `out`, `artifacts`), ignored
files, Python caches and hidden
paths other than the repository dotfiles and `.cargo/config.toml`. The bundler refuses any file
that looks like a credential: an environment file, a key or a certificate.

The release file also names the packaged `cwr` CLI and Python SDK under `[artifact.cli]` and
`[artifact.sdk]`, and pins their checksums.

## Reproducibility

The tarball is byte-reproducible. Entries are sorted, timestamps are zero, owner and group are 0
and file modes are normalised to 0644 or 0755. Two runs of the bundler over the same tree write
identical bytes.

The bundler refuses to overwrite an existing bundle of the same name with different content. A
changed tree is published under a new version.

`bin/cwr` is added only when a release binary exists in the cargo target directory. A rebuilt
bundle matches a published one only when the same binary is present, or both lack it.

Every other input is pinned by content. Crate versions are fixed by `Cargo.lock`, and its digest
is a pin. A component revision is the sha256 tree digest of its directory, not a version-control
commit. Builds are content-addressed, so `cwr release check` rebuilds the control, experimental
and upgrade-pair Builds from the bundled World Sources and compares their hashes with the pins.

## Cutting a release

Run these steps from the repository root.

1. Set the version in the three files listed under [Versions](#versions).
2. Run the gates: `make check`, then `make test`.
3. Run every qualification case with `make qualify`, then assemble closure with
   `python3 tests/cwr/closure.py`. Closure refuses, and lists every blocking item, when a case
   has no accepted attempt, fails, is tampered, makes no assertion or was recorded against a Build
   from a compiler, kernel or SDK revision other than the current one.
4. The owner runs the acceptance journey with `python3 tests/cwr/closure.py --journey` and
   records acceptance with `python3 tests/cwr/closure.py --accept --owner` followed by the
   owner's name. Nothing in the repository records acceptance on the owner's behalf.
5. Place the packaged CLI and SDK at the paths named by `[artifact.cli]` and `[artifact.sdk]` in
   [benchmark/release.kvx](benchmark/release.kvx).
6. Record the pins with `cwr release pin --write`, then confirm them with
   `cwr release check --claim`. The check exits 0 only when every pin equals its recomputation
   and closure and owner acceptance are recorded.
7. Move the Unreleased section of [CHANGELOG.md](CHANGELOG.md) under a heading that names the
   version and the date.
8. Bundle with `make release`. It runs `check`, `test` and `qualify` again, then the bundler,
   which writes the tarball and its `.sha256` file into `artifacts/`. `scripts/release.sh` runs
   the same target and keeps the log in `artifacts/release.log`.

To rehearse, `python3 tools/release.py --dry-run` runs every bundler check and writes nothing.
`python3 tools/release.py --snapshot` bundles a tree whose `[meta] release` is still null and
names the bundle `cwr-<version>-snapshot`. A snapshot is never a release.

The bundler exits 0 when it writes a bundle, or when every check passes under `--dry-run`. It
exits 1 when it refuses and 2 on a usage error. It refuses, listing every reason, when:

- a tool that `make check` needs is missing or fails to run: Python 3.12 or newer, cargo, rustfmt,
  clippy or ruff
- a file that `make check` reads is missing
- the declared versions disagree or are not of the form `MAJOR.MINOR.PATCH[-pre][+build]`
- `[meta] release` is null and `--snapshot` is not given
- a file selected for the bundle looks like a credential
- a bundle of the same name already exists with different content

## Verifying a release

The commands below use version 0.1.0 as the example. Download `cwr-0.1.0.tar.gz` and
`cwr-0.1.0.tar.gz.sha256` into one directory. Check the tarball
with `sha256sum -c cwr-0.1.0.tar.gz.sha256`, then unpack it with `tar -xzf cwr-0.1.0.tar.gz`.

Inside the unpacked `cwr-0.1.0` directory, check every file against the manifest, then check every
pin against the bundled sources:

```sh
sha256sum -c --quiet MANIFEST.sha256
cwr release check
```

`cwr release check` needs the `cwr` binary and Python 3.12 or newer as `python3` on `PATH`; the
World Compiler runs each world's contract tests with Python. It recomputes every revision, digest,
Build hash and checksum from the unpacked tree and prints one line per pin. It exits 0 when every
pin matches, 2 with one named diagnostic for each pin that does not, and 1 on a usage error or an
unreadable release file. `--claim` also requires closure and owner acceptance. `--json` prints the
same report as JSON.

To confirm that the tarball itself reproduces, check out the same tree and run
`python3 tools/release.py --output "$(mktemp -d)"`. The digest it prints must equal the one in
`cwr-0.1.0.tar.gz.sha256`.

## Diagnostics

Each pin in [benchmark/release.kvx](benchmark/release.kvx) has one named diagnostic. A diagnostic
names the release-file key it guards and blocks the release.

| Diagnostic | Key | Fails when |
|---|---|---|
| CWR-R001 | `pin.compiler.revision` | The compiler revision is not pinned or differs from the tree |
| CWR-R002 | `pin.kernel.revision` | The kernel revision is not pinned or differs from the tree |
| CWR-R003 | `pin.sdk.revision` | The SDK revision is not pinned or differs from the tree |
| CWR-R004 | `pin.cli.revision` | The CLI revision is not pinned or differs from the tree |
| CWR-R005 | `build.refworld_control.hash` | The refworld control Build hash is missing or fails verification |
| CWR-R006 | `build.x_drift.hashes` | A drift Build hash is missing or fails verification |
| CWR-R007 | `build.x_one_primitive.hash` | The one-primitive Build hash is missing or fails verification |
| CWR-R008 | `build.x_version_only.hashes` | A version-only Build hash is missing or fails verification |
| CWR-R009 | `build.x_silent_change.hashes` | A silent-change Build hash is missing or fails verification |
| CWR-R010 | `build.upgrade_pairs.hashes` | An upgrade pair Build hash is missing or fails verification |
| CWR-R011 | `cassettes.digest` | The cassette set digest is missing or differs from `benchmark/suite.kvx` |
| CWR-R012 | `suite.digest` | The task suite digest is missing or differs from `benchmark/suite.kvx` |
| CWR-R013 | `artifact.cli.sha256` | The packaged CLI is missing or its checksum does not match |
| CWR-R014 | `artifact.sdk.sha256` | The packaged SDK is missing or its checksum does not match |
| CWR-R015 | `evidence.closure` | Closure or owner acceptance is missing or names blocking items (checked with `--claim`) |
| CWR-R016 | `pin.contract.revision` | The contract crate revision is not pinned or differs from the tree |
| CWR-R017 | `dependencies.cargo_lock` | The `Cargo.lock` digest is not pinned or differs from the tree |
| CWR-R018 | `results.digests` | A campaign result digest is missing or differs from `benchmark/results` |
| CWR-R019 | `meta.release` | The release version is missing or disagrees with the CLI and SDK versions |
| CWR-R020 | `meta.spec_sha256` | The specification digest is not pinned or differs from `spec/cwr/spec.kvx` |
| CWR-R021 | `evidence.preregistration_sha256` | The pre-registration digest is not pinned or differs from the registered file |

## What a release cannot rest on

A release rests on executed qualification cases, each with an attempt record accepted by the
independent checker, and on owner acceptance. None of the following substitutes for them:

- the specification validator, which checks the structure of the specification only
- a component case standing in for its parent case, a hypothesis or the release
- a driver's exit code without an attempt record and a checker result
- evidence recorded against a Build, cassette set, task suite, model, context tail, compiler,
  kernel or metric definition other than the ones the release pins
- any comparison with a system outside CWR: every experimental arm is a CWR Build
