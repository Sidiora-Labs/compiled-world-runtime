<!-- Copyright Sidiora Labs Inc. CWR® 2026-2027 -->
<!-- SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0 -->

# Support

CWR is a Sidiora Labs research system under active implementation. There is no release and no
service-level commitment; support is provided by the Sidiora Development Team on a best-effort
basis to people with repository access.

## Before asking

1. Read [README.md](README.md), then [spec/cwr/README.md](spec/cwr/README.md) for what CWR promises
   and the user guides under [docs/cwr/](docs/cwr/README.md) for how to use it.
2. Check [GOTCHA.kvx](GOTCHA.kvx) for known pitfalls (cargo lock stalls, strict KVX, header
   exemptions, validator stage rules).
3. Run `make check` and the relevant task driver; most problems show up there with a located error.
4. Check [CODEMAP.md](CODEMAP.md) to find the code that owns a behaviour.

## Asking

Open an issue in the repository tracker with the commit, the command you ran, the full output
(with any secret removed) and what you expected. A disagreement between the code and the
specification is a defect report; say which clause you are reading. Security issues go through
[SECURITY.md](SECURITY.md), never the public tracker.

## What we cannot help with

Comparisons with other agents or harnesses are out of scope by design (see
[GOVERNANCE.md](GOVERNANCE.md)). Results reported outside `benchmark/results/` and the
`research/` reports are not supported claims.
