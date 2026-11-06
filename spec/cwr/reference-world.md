<!-- Copyright Sidiora Labs Inc. CWR® 2026-2027 -->
<!-- SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0 -->

# Reference world: refworld modules, rules, stub servers, task suite and upgrade pairs

Status: proposed contract; nothing has been built or run and every case is UNRUN. Requirement 11 in [spec.kvx](spec.kvx) applies, together with the adapter rules in [kernel.md](kernel.md) and the experimental-build rules in [compiler.md](compiler.md).

refworld is the first World Source. It is small enough that every invariant in [design.body.md](design.body.md) can be exercised by contract tests and drivers, and rich enough to produce tasks across three length bins and to reach every code in the failure envelope. It is the control Build for every experimental arm in [experiments.md](experiments.md). Its illustrative source files live under [examples/refworld](examples/refworld/world.kvx); they have not been compiled and establish no Build hash.

## 1. Modules

Every module is one contract file under `contracts/` or `adapters/`, one implementation file under `lib/`, a fixture set under `fixtures/` and one section of the generated manual. Internal modules keep all state inside the Instance state store; adapter modules reach the outside only through the gateway.

| Module | Kind | Primitives | State it owns |
|---|---|---|---|
| fs | internal | exists, locked, size, list, read, write, delete, lock, unlock, move | Path-keyed byte files and the per-Instance lock set |
| kv | internal | has, get, set, del, keys | Namespaced key/value map with a per-namespace quota |
| jobs | internal | submit, status, result, cancel | Job table with queued, running, done and failed states keyed to world_time |
| search | adapter | query | None; interface guaranteed, hits are content |
| publish | adapter | post, status, delete | None; the stub keeps the idempotency store |
| world | internal | now, version, seed, limits, random, history, propose | World clock reading, RNG stream, ledger view and the proposal outbox |

The example contract files [contracts/fs.kvx](examples/refworld/contracts/fs.kvx), [adapters/publish.kvx](examples/refworld/adapters/publish.kvx) and [adapters/search.kvx](examples/refworld/adapters/search.kvx) show the grammar for the fs, publish and search modules. The kv, jobs and world contracts follow the same grammar and are task 4.1 deliverables.

## 2. Primitive catalogue

Preconditions are listed in declared order with their failure codes; the kernel reports the first failing one. Every expression references only parameters and read_only primitives of the same Build. The catalogue is normative for task 4.1; the rendered manual must match it entry for entry.

### 2.1 fs

| Primitive | Side effect | Preconditions in order | Effects |
|---|---|---|---|
| fs.exists(path) | read_only | none | none |
| fs.locked(path) | read_only | none | none |
| fs.size(path) | read_only | fs.exists(path) → NOT_FOUND | none |
| fs.list(prefix) | read_only | none | none |
| fs.read(path) | read_only | fs.exists(path) → NOT_FOUND | none |
| fs.write(path, content) | mutating | not fs.locked(path) → LOCKED; len(content) <= 1048576 → LIMIT | fs.exists(path); fs.read(path) == content |
| fs.delete(path) | mutating | fs.exists(path) → NOT_FOUND; not fs.locked(path) → LOCKED | not fs.exists(path) |
| fs.lock(path) | mutating | fs.exists(path) → NOT_FOUND; not fs.locked(path) → CONFLICT | fs.locked(path) |
| fs.unlock(path) | mutating | fs.locked(path) → NOT_FOUND | not fs.locked(path) |
| fs.move(src, dst) | mutating | fs.exists(src) → NOT_FOUND; not fs.locked(src) → LOCKED; not fs.exists(dst) → CONFLICT | not fs.exists(src); fs.exists(dst) |

### 2.2 kv

| Primitive | Side effect | Preconditions in order | Effects |
|---|---|---|---|
| kv.has(ns, key) | read_only | none | none |
| kv.get(ns, key) | read_only | kv.has(ns, key) → NOT_FOUND | none |
| kv.keys(ns) | read_only | none | none |
| kv.used(ns) | read_only | none | none |
| kv.set(ns, key, value) | mutating | len(value) <= 1024 → INVALID; kv.used(ns) + len(value) <= 65536 → LIMIT | kv.has(ns, key); kv.get(ns, key) == value |
| kv.del(ns, key) | mutating | kv.has(ns, key) → NOT_FOUND | not kv.has(ns, key) |

### 2.3 jobs

A job submitted at world_time t with duration n is queued until t + 1, running until t + n, then done. A job whose `fail_at` fixture field is set becomes failed at that tick instead. Transitions are a pure function of world_time, so the model can predict them from world.now() and jobs.status().

| Primitive | Side effect | Preconditions in order | Effects |
|---|---|---|---|
| jobs.status(id) | read_only | jobs.known(id) → NOT_FOUND | none |
| jobs.known(id) | read_only | none | none |
| jobs.submit(kind, n) | mutating | n >= 1 and n <= 1000 → INVALID; jobs.active() < 8 → LIMIT | jobs.known(result); jobs.status(result) == "queued" |
| jobs.active() | read_only | none | none |
| jobs.result(id) | read_only | jobs.known(id) → NOT_FOUND; jobs.status(id) == "done" → CONFLICT | none |
| jobs.cancel(id) | mutating | jobs.known(id) → NOT_FOUND; jobs.status(id) in ["queued", "running"] → CONFLICT | jobs.status(id) == "failed" |

### 2.4 search and publish (adapters)

| Primitive | Idempotency | Preconditions in order | Declared failures | Timeout |
|---|---|---|---|---|
| search.query(q, limit) | none | len(q) > 0 → INVALID; limit <= 50 → LIMIT | INVALID, LIMIT, RATE_LIMITED, UNAVAILABLE, TIMEOUT | 3000 ms |
| publish.post(key, title, body) | key | len(title) <= 200 → INVALID | INVALID, DUPLICATE_KEY, RATE_LIMITED, UNAVAILABLE, TIMEOUT | 5000 ms |
| publish.status(post_id) | natural | none | NOT_FOUND, UNAVAILABLE, TIMEOUT | 2000 ms |
| publish.delete(post_id) | natural | none | NOT_FOUND, UNAVAILABLE, TIMEOUT | 2000 ms |

All four declare side_effect external and content_guaranteed false. Their return shapes (list[SearchHit], PostReceipt, PostStatus, None) are guaranteed; their payloads are not. The publish rate limit is a stub-side rule: three accepted posts per 100 ticks, after which the stub answers RATE_LIMITED with retry_after equal to the ticks until the window reopens.

### 2.5 world

| Primitive | Side effect | Preconditions | Effects |
|---|---|---|---|
| world.now() | read_only | none | none |
| world.version() | read_only | none | none |
| world.seed() | read_only | none (present only when rng.seed_visible is true) | none |
| world.limits() | read_only | none | none |
| world.random() | mutating | none | rng stream advanced by one draw |
| world.history(n) | read_only | n >= 0 and n <= 500 → INVALID | none |
| world.propose(text) | mutating | len(text) <= 4096 → INVALID | ledger record of kind proposal appended; no world-state effect |

world.random is classified mutating because it advances the RNG state captured in snapshots; its sole effect is on the stream, never on module state.

## 3. Rules worth having

Each rule is a chair rule: documented in the manual, enforced by the kernel, and present because it exercises one pillar or hypothesis.

| Rule | Exercises | Why it is here |
|---|---|---|
| fs.delete requires exists and not locked, in that order | Visibility, predictable failure | Both conditions are queries; the model can always predict the outcome before acting. |
| fs.write refuses content over 1 MiB with LIMIT | Controlled change (U-pair material), H4 | A limit that can be tightened in an upgrade with a documented major bump. |
| fs.move requires the destination not to exist | Stability, CONFLICT reachability | A job finishing mid-task can create the destination; the outcome is still a pure function of visible state at call time. |
| kv namespaces with a 64 KiB quota and 1 KiB value limit | State shape, migrations | The 0.1.0 to 0.2.0 namespace migration is the state_shape change in the example changelog. |
| jobs transition on world_time | Stability of the clock | Waiting is expressed in ticks the model can read; no wall-clock guesswork. |
| jobs.result requires status done, else CONFLICT | H3 rule reuse | Early in a long task the model learns to poll; late in the task it must reuse that rule. |
| search.query returns the same shape for any string | Interface-guaranteed, content-not | The tennis-versus-golf case: the rule is the shape, the hits are content. |
| publish.post idempotency key, DUPLICATE_KEY on reuse with different content | Predictable failure, adapter idempotency | A retry after a partial script is safe only if the model keeps the key. |
| publish rate limit 3 per 100 ticks with retry_after | Predictable failure in ticks | The model can schedule around the limit using world.now(). |
| world.history(n) | H3 long context | Memory is a primitive of the world, not a property of the prompt. |
| world.propose(text) has no effect | Controlled change | The only upward channel; the driver proves it changes nothing observable. |

## 4. Stub servers and cassettes

The search and publish adapters are backed by two stub servers under `benchmark/stubs/`. They exist so that every declared adapter failure is reachable without any real third-party service and so that cassettes can be recorded once and frozen.

| Behaviour | Contract |
|---|---|
| Determinism | Each stub takes a seed and a schedule file; given equal seed, schedule and request sequence it produces equal responses. |
| Rate limiting | publish: three accepted posts per 100 ticks per Instance, then RATE_LIMITED with retry_after in ticks. search: ten queries per 50 ticks, then RATE_LIMITED. The tick is taken from the gateway's world_time header, not host time. |
| Duplicate keys | publish keeps a per-Instance store of key to payload hash and receipt; an identical replay returns the original receipt, a different payload returns DUPLICATE_KEY. |
| Unavailability | The schedule names request indices or tick ranges at which the stub answers 503; the gateway maps this to the declared UNAVAILABLE and nothing else. |
| Timeouts | The schedule can delay a response past timeout_ms; the gateway reports TIMEOUT and the stub still completes the side effect, so the model must use publish.status to reconcile. |
| Declared codes only | The stubs are tested in task 4.1 to emit only statuses the gateway maps to declared codes; an undeclared status is a stub defect and fails CWR-014. |
| Content | search hits are generated from the query string and seed; they are stable within a cassette and intentionally differ between seeds to keep content-not-guaranteed honest. |

Cassettes are recorded in record mode by running every reference solution of the task suite once per seed against the stubs. The resulting set is keyed by adapter, canonical request hash and sequence, and its digest is frozen in `benchmark/suite.kvx`. Drivers and campaigns use replay mode only; a cassette miss is UNAVAILABLE and never a network call. Re-recording produces a new digest and invalidates every result recorded under the old one.

## 5. Task suite layout

Sixty tasks live under `benchmark/tasks/`, one directory each, named `tNNN-slug`.

| File | Content | Rule |
|---|---|---|
| task.md | The instruction given to the model, verbatim, identical across arms | Must not name any rule the task is designed to make the model discover. |
| initial.kvx | Starting Instance state per module, in the World Source KVX format | Loaded by cwr instance create; the same file for every arm and seed. |
| success.py | A function `success(state, ledger) -> bool` | Pure over final state and ledger; imports only benchmark/predicates/common.py. |
| bin | One of short, medium, long | Established from the reference solution's minimum primitive count. |
| rules | Newline-separated list of rule identifiers the task exercises | Used to choose drift targets for H1 and to tag rule-reuse tasks for H3. |

Each task also has a reference solution script kept outside the task directory under `benchmark/solutions/`, which the model never sees. Running the reference solution in replay mode must satisfy success.py; its primitive count establishes the bin. `benchmark/suite.kvx` records, per task, the bin, the primitive count, the rules, whether it is a rule-reuse task and the suite digest over every task directory.

## 6. Bins

| Bin | Reference primitive count | Count | Rule-reuse tasks |
|---|---|---|---|
| short | at most 5 | 20 | 0 |
| medium | 6 to 25 | 20 | 0 |
| long | 26 to 120 | 20 | at least 12 |

A rule-reuse task is a long task in which a rule is first encountered in the first quarter of the reference solution, is not needed for at least ten primitives, and then becomes necessary again in the last quarter. The `rules` file names the reused rule so that the rule reuse metric in [experiments.md](experiments.md) can be computed from the ledger alone. The counts are proposed choices for the first suite, not statistical power claims.

## 7. Success predicates

A predicate is a pure function of the final Instance state and the ledger. It never reads model text, kernel internals, sandbox stdout or adapter payloads. The helpers in `benchmark/predicates/common.py` cover file equality, key presence, job state, ledger record search by primitive and code, and receipt lookup by idempotency key. Two evaluations of one predicate over one state and ledger must return the same value; task 4.2's driver evaluates every predicate twice and compares.

Because adapter payloads are content, a predicate must not assert on search hits or post bodies. It may assert that a post with a given key exists in the ledger with status ok, that a file contains whatever the ledger recorded as the first hit's title, or that the rate limit was respected (no RATE_LIMITED record after the first).

## 8. Illustrative task sketches

These sketches are illustrative; the authored tasks in task 4.2 may differ.

| Field | t003-tennis-note (short) |
|---|---|
| Instruction | Write the title of the top search hit for "tennis" to /notes/tennis.md. |
| Reference primitives | search.query, fs.write (2) |
| Rules | search shape guaranteed, fs.write limit |
| Predicate | /notes/tennis.md exists and equals the title recorded in the ledger's first ok search.query result |

| Field | t027-locked-report (medium) |
|---|---|
| Instruction | Collect three search results on "golf" into /reports/golf.md, lock it, publish its title under key golf-1 and store the receipt id in kv namespace receipts. |
| Reference primitives | search.query, fs.write, fs.lock, publish.post, kv.set and reads (about 9) |
| Rules | fs.lock CONFLICT, publish idempotency, kv quota |
| Predicate | file exists and is locked; ledger has one ok publish.post with key golf-1; kv receipts/golf-1 equals the receipt id from that record |

| Field | t048-inbox-digest (long) |
|---|---|
| Instruction | For every file in /inbox, search its first line, append the top title, move it to /done, and publish a digest every ten files respecting the rate limit; record every receipt in kv. |
| Reference primitives | about 90 across fs, search, publish, kv |
| Rules | fs.move CONFLICT, publish rate limit, kv quota |
| Predicate | /inbox is empty, /done has every file enriched, exactly ceil(n/10) ok posts, no RATE_LIMITED record after the first |

| Field | t055-poll-then-reuse (long, rule reuse) |
|---|---|
| Instruction | Submit a long job, do unrelated file work for a while, then submit a second job and store both results. |
| Reference primitives | about 40; jobs.result is needed near the start and again near the end |
| Reused rule | jobs.result requires status done (CONFLICT otherwise) |
| Predicate | both results stored in kv; the ledger shows at most one CONFLICT on jobs.result for the second job |

## 9. Upgrade pairs

Each pair is two complete World Sources A and B under `examples/refworld-upgrades/UN/`, differing in exactly one rule, with a CHANGELOG.kvx entry and, where state shape changes, a migration. Expected ContractDiffs, bumps and notice texts are frozen in `benchmark/upgrades.kvx`. Each pair has one post-upgrade task whose second half depends on the changed rule.

| Pair | Change kind | Example change | Derived bump | Post-upgrade task | Failure the pre-upgrade solution hits |
|---|---|---|---|---|---|
| U1 | precondition tightened | fs.delete gains not fs.locked(path) → LOCKED | major | delete a locked batch after unlocking | LOCKED on fs.delete |
| U2 | primitive removed | fs.move removed; model must write then delete | major | reorganise /inbox without fs.move | closure refusal of fs.move |
| U3 | failure code changed | jobs.result before done returns CONFLICT instead of NOT_FOUND | major | poll two jobs and store results | the old handler catches the wrong code |
| U4 | limit changed | kv.set value limit halves to 512 bytes | major | store receipts within the new limit | INVALID on kv.set |
| U5 | state shape changed | kv becomes namespaced, with migration | major | read a value stored before the upgrade | NOT_FOUND on kv.get without namespace |
| U6 | documentation only | fs.write doc clarifies replacement semantics | patch | identical task; control for notice noise | none; both solutions succeed |

U6 is the control pair: a notice with no behavioural change should not change the model's actions, and a measured difference there would point at notice wording rather than the mechanism.

## 10. Out of scope for the first world

- Multiple concurrent model sessions in one Instance.
- Binary media beyond opaque byte files.
- Any adapter that contacts a real third-party service; every adapter is stub-backed.
- Filesystem directories as first-class objects; prefixes are string conventions over paths.
- Persistent jobs across upgrades other than through the declared migration.

## 11. Required future evidence

- CWR-014 (task 4.1): refworld compiles; every declared failure of every primitive and adapter is reached by contract tests or stub schedules; the stubs emit only declared codes; the cassette set replays offline under its frozen digest.
- CWR-015 (task 4.2): every reference solution satisfies its predicate in replay mode; bins follow recorded primitive counts; predicates are deterministic and import nothing from the kernel.
- CWR-016 (task 4.3): each pair compiles with the frozen diff and bump; a live Instance upgrades with the frozen notice; the post-upgrade solution succeeds and the pre-upgrade solution fails with the documented code.

Nothing in this document is evidence that refworld exists, compiles or behaves as described. The counts, limits and windows are proposed choices and will be recorded in `benchmark/suite.kvx` before any campaign runs.
