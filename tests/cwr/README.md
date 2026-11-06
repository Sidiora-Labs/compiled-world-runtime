<!-- Copyright Sidiora Labs Inc. CWR® 2026-2027 -->
<!-- SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0 -->

# Qualification: registry, runner, checker and evidence

Status: most cases have not been run through the runner yet. [registry.kvx](registry.kvx) holds the
current status of every case.

A claim about CWR's behaviour counts only when an executed qualification attempt supports it and an
independent checker accepts the record. This directory holds that machinery. A driver exercises the
real `cwr` binary and reports what it observed. The runner stores each attempt as an immutable,
hash-chained record. The checker recomputes every outcome from the stored ledgers and final state and
never trusts the driver's own verdict. The contract is
[verification.md](../../spec/cwr/verification.md).

## Contents

| Path | Role |
| --- | --- |
| `registry.kvx` | The 20 qualification cases: tier, clauses covered, the assertion inventory, the drivers that contribute to each case, and each case's status and evidence |
| `qualify.py` | The runner. It runs drivers, stages their output, has the checker verify the record, writes it once and appends it to the attempt index. |
| `check_evidence.py` | The independent checker. It starts no process, opens no network connection and reads only records, the files they name, the index, the registry and the schema. |
| `closure.py` | Assembles release closure from accepted records and lists every item that blocks a release claim |
| `evidence.schema.json` | The schema every attempt record must match, with every field required and nothing extra |
| `cases/W_N.py` | One driver per specification task, run as `python3 tests/cwr/cases/W_N.py` from the repository root |
| `evidence/` | Attempt records, their artifacts and the hash-chained attempt index `evidence/index.jsonl` |

## Cases

| Tier | Cases |
| --- | --- |
| component | CWR-001 contract grammar and failure envelope, CWR-002 compiler closure and visibility lints, CWR-003 contract tests and rule determinism, CWR-004 ContractDiff and changelog gate, CWR-005 immutable Build artifacts and generated manual, CWR-006 ledger, snapshot, world clock and RNG, CWR-007 sandbox closure and resource limits, CWR-019 evidence registry and false-success traps |
| integration | CWR-008 atomic primitives and ordered preconditions, CWR-009 adapter gateway, CWR-010 crash recovery and ledger replay, CWR-011 controlled upgrade, CWR-012 context composition and turn protocol, CWR-013 experimental Builds and kernel gate, CWR-014 reference world, stubs and cassettes, CWR-015 task suite, CWR-016 upgrade pairs, CWR-017 harness isolation and ledger-only metrics |
| campaign | CWR-018 hypothesis campaigns H1 to H4 |
| release | CWR-020 specification, documentation and release closure |

## Run

```sh
python3 tests/cwr/qualify.py --check-registry            # validate the registry against the specification
python3 tests/cwr/qualify.py --case CWR-007              # run every driver of one case
python3 tests/cwr/qualify.py --task 2.2                  # run one driver as a component attempt
python3 tests/cwr/check_evidence.py --status CWR-019     # derive one case's status from the index
python3 tests/cwr/closure.py                             # read-only release closure report
```

`make qualify` runs every case in order and stops at the first that does not pass. `make closure`
runs the closure report.

| `qualify.py` exit | Meaning |
| --- | --- |
| 0 | PASS |
| 1 | FAIL |
| 2 | Refused: invalid registry, unknown case or task, missing driver or no registered inventory |
| 3 | BLOCKED: a resource the attempt needs is missing |
| 4 | The checker rejected the record as written |

## Driver protocol

The runner starts a driver with `CWR_QUALIFY=1`, `CWR_TASK`, `CWR_CASES` (comma separated) and
`CWR_EVIDENCE_DIR`, an empty directory the driver may write into. A driver reports observations on
stdout, one per line. Every other line is ignored.

```text
ASSERT <CWR-NNN.aK> PASS|FAIL [diagnostic]
BLOCKED <missing resource>
```

A driver that touched Builds or Instances writes `$CWR_EVIDENCE_DIR/attempt.json`, naming the Build
hashes, world, cassette digest, gateway mode, sandbox, and for each Instance its seed, arm, task,
ledger and final state. Ledgers and final states are files inside the evidence directory.

## What makes a PASS

- The driver observed exactly the registered assertion inventory for its scope, and every assertion
  passed.
- The checker recomputed every registered assertion with the case's independent oracle and every one
  passed. An assertion with no oracle is UNVERIFIED and can never be PASS.
- The record validates against the schema and the registry, every log, ledger and final state
  re-hashes to its recorded digest, and the record is an entry of the attempt index whose head and
  length are pinned in the registry.
- Ledgers validate against the kernel ledger schema, and Build hashes, Instance ids, seeds, tail sizes,
  arms and model identity are recomputed from the ledger headers.
- A campaign PASS also needs the complete arm matrix and a registered pre-registration.

A driver's exit code or `ASSERT` line alone never makes a PASS. A task-scope (component) record never
changes a case's status.

## Evidence store

Each attempt gets `evidence/<case>/<timestamp>-<n>/` for its logs and artifacts and
`evidence/<case>/<timestamp>-<n>.json` for its record. The timestamp is the attempt's UTC start second
and cannot be chosen. A record is written exactly once and made read-only, and no record is ever
overwritten or edited. Every record is appended, under an exclusive lock, to `evidence/index.jsonl`,
where each line carries the sha256 of the previous line.

## Closure

`closure.py` refuses a release claim, and lists every blocking item, when a case is missing, not
PASS, tampered, without assertions or built from a revision that is no longer the current tree; when
a specification clause maps to no PASS case; when a hypothesis report or its pre-registration is
missing; or when owner acceptance is not recorded. Owner acceptance is the owner's act alone. Nothing
in this repository records it on the owner's behalf.
