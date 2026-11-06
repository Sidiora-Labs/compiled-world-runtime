<!-- Copyright Sidiora Labs Inc. CWR® 2026-2027 -->
<!-- SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0 -->

refworld is a small world of files, keys, jobs and two external services. You act in it by writing
one Python script per turn. Every action you can take is a primitive listed in this manual, and
every rule that can refuse an action is written next to that primitive as a numbered precondition
with the failure code it produces. Nothing outside this manual is callable from your script.

The world holds path-keyed byte files that can be locked (fs), a key/value store with a
64 KiB quota (kv), background jobs that progress with world time (jobs), an external search
index (search) and an external publishing service (publish). The world module gives you the clock,
the Build version, your remaining budget, a random stream, your own history and a way to write to
the human who owns this world (world.propose).

The rules of this Build do not change while you act. If the owner recompiles the world into a new
version and moves you to it, you are told on your next turn what changed.
