<!-- Copyright Sidiora Labs Inc. CWR® 2026-2027 -->
<!-- SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0 -->

Named record types used by parameters and return values. A record is a dict with exactly the listed
keys. Fields marked as content come from an external service and are not guaranteed; only their
presence and type are.

```text
record SearchHit
  rank: int         content: position in this answer, from 1
  title: str        content
  url: str          content
  snippet: str      content

record PostReceipt
  id: str           post id assigned by the service, used by publish.status and publish.delete
  key: str          the idempotency key the post was made under
  accepted_at: int  content: the world tick at which the service accepted the post

record PostStatus
  id: str           the post id that was asked about
  key: str          content: the idempotency key the service has for this post
  state: str        content, as reported by the service
  accepted_at: int  content

record Limits
  cpu_ms: int
  wall_ms: int
  memory_mb: int
  primitive_calls: int
  adapter_calls: int
  remaining_primitive_calls: int    calls left in this turn
  remaining_adapter_calls: int      external calls left in this turn
  tick_per_primitive: int
  tick_per_turn: int

record LedgerRecord
  seq: int
  turn: int
  world_time: int
  kind: str                         primitive, adapter, turn, drift, proposal, upgrade or recovery
  primitive: str                    qualified primitive name; empty for other kinds
  params_hash: str                  empty for kinds without parameters
  preconditions: list[PreconditionCheck]
  outcome: Outcome

record PreconditionCheck
  index: int
  expr_hash: str
  passed: bool

record Outcome
  status: str                       ok or error; empty for kinds without an outcome
  code: str                         envelope code; empty on ok
  shape: str                        declared return type name
  side_effect: str                  read_only, mutating or external
```
