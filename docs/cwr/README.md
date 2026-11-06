<!-- Copyright Sidiora Labs Inc. CWR® 2026-2027 -->
<!-- SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0 -->

# CWR user guides

These guides explain how to write a world, run it and measure it. Each guide is derived from the
contract chapters in [spec/cwr](../../spec/cwr/README.md). Where a guide and a contract chapter
disagree, the contract chapter wins.

Status: of the twenty qualification cases, only CWR-019 has an accepted passing attempt. The guides
describe the behaviour the specification defines; a section is qualified only when its case passes.

| Guide | Audience | Derived from |
|---|---|---|
| [world-source.md](world-source.md) | World authors | `spec/cwr/world-source.md`, `spec/cwr/compiler.md` |
| [kernel.md](kernel.md) | Operators | `spec/cwr/kernel.md`, `spec/cwr/model-surface.md`, `spec/cwr/compiler.md` section 9 |
| [experiments.md](experiments.md) | Experimenters | `spec/cwr/experiments.md`, `spec/cwr/verification.md`, `spec/cwr/compiler.md` section 10 |

## Checking qualification status

The evidence checker reports the recorded outcome of any case:

```sh
python3 tests/cwr/check_evidence.py --status CWR-019
```

The output is a JSON object that names the case, its status (`PASS`, `FAIL` or `UNRUN`) and the
evidence record it was read from.

## Open points

Every guide ends with an "Open points in the contract" section. It lists disagreements between
specification chapters that the guide does not resolve. Each resolution goes into the
specification first, then into the guide.

## Related documents

[README.md](../../README.md), [CHANGELOG.md](../../CHANGELOG.md), [RELEASE.md](../../RELEASE.md)
and [benchmark/release.kvx](../../benchmark/release.kvx).
