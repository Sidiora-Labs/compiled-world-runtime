<!-- Copyright Sidiora Labs Inc. CWR® 2026-2027 -->
<!-- SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0 -->

# Support

CWR has no release and no service-level commitment. The Sidiora Development Team answers questions
and defect reports on a best-effort basis.

## Before you ask

1. Read [README.md](README.md) for what CWR is and how to run it.
2. Read the guide for your role under [docs/cwr/](docs/cwr/world-source.md): world author,
   operator or experimenter. The normative behaviour is in
   [spec/cwr/README.md](spec/cwr/README.md).
3. Check [GOTCHA.kvx](GOTCHA.kvx) for known pitfalls, such as cargo lock contention and strict KVX
   values.
4. Run `make check` and the relevant task driver. Most problems surface there with a file, line
   and column.
5. Use [CODEMAP.md](CODEMAP.md) to find the code that owns a behaviour.

## Ask a question or report a defect

Open an issue in the repository's issue tracker. Include the commit, the command you ran, its full
output with any credential removed, and what you expected instead. When the code and the
specification disagree, name the clause you are reading. That disagreement is a defect report.

Report security issues privately as described in [SECURITY.md](SECURITY.md). Never use the public
issue tracker for them.

## Out of scope

- Comparisons between CWR and other systems. The project does not make them by design; see
  [GOVERNANCE.md](GOVERNANCE.md).
- Numbers reported anywhere other than a campaign result under `benchmark/results/` or a
  hypothesis report under `research/`. Those are not supported claims.
