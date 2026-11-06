<!-- Copyright Sidiora Labs Inc. CWR® 2026-2027 -->
<!-- SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0 -->

## Calling primitives

Import the modules you need at the top of the script, for example `import fs, kv, jobs, world`.
Call primitives with keyword arguments named exactly as in the reference, for example
`fs.write(path="/notes/a.md", content=b"hello")`. Each call is atomic: it either happens completely
or not at all. A script is not atomic: when a later call fails, earlier successful calls stand and
the turn reports status partial with the full trace.

Python reserves the word del, so the primitive kv.del is bound to the attribute `del_` of the kv
module in your script. Its rules are exactly those listed under kv.del.

## Checking before acting

Every precondition is written in terms of read_only primitives you can call yourself. Calling
fs.exists before fs.delete tells you exactly whether fs.delete will succeed. Read_only
primitives still count against primitive_calls and still advance world time by one tick.

## Failures

A refused call raises an exception carrying `code`, `primitive`, `precondition_index`, `detail` and,
for RATE_LIMITED only, `retry_after` in ticks. You may catch it and continue. Preconditions are
numbered in evaluation order; only the first failing one is reported.

## Paths, bytes and text

Paths are strings beginning with `/`. Directories do not exist as objects: fs.list with a prefix
such as `/inbox/` returns every path starting with it, sorted. File contents and kv values are
bytes; encode text with `.encode()` and decode with `.decode()`. Lengths in rules are byte lengths
for bytes and character lengths for text.

## Time

World time is an integer number of ticks, read with world.now. Every executed primitive advances it
by one tick and every turn adds ten more. A call refused at its preconditions does not advance it.
Jobs, rate-limit windows and retry_after are all measured in ticks, so waiting means ending the turn
or doing other work, never sleeping.

## Jobs

A job submitted at tick t with duration n is queued until tick t + 1, running until tick t + n, and
done from then on. jobs.result refuses with CONFLICT until jobs.status reports done. A cancelled job
is failed. At most 8 jobs are active at once.

## External services

search and publish are external. Their result shapes and failure codes are rules; their content is
not guaranteed. The world never retries an external call. publish.post takes an idempotency key:
repeating the identical call with the same key returns the original receipt, while reusing a key
with different content is DUPLICATE_KEY. After a TIMEOUT on publish.post the post may or may not
exist; check with publish.status before posting again under a new key. The publishing service
accepts three posts per 100 ticks; past that it answers RATE_LIMITED with retry_after.

## Notation in rules

In an Ensures line, `result` stands for the value the primitive returned.
