<!-- Copyright Sidiora Labs Inc. CWR® 2026-2027 -->
<!-- SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0 -->

# Enrich and digest an inbox

For every file in /inbox, in path order: search for its first line with a limit of one result,
append a line top: <title of the top hit> to the file, and move it to /done under the same name.
After every fifth file moved, publish a digest post titled Digest N (N = 1, 2, 3) whose body lists
the names of those five files, one per line, using the idempotency key digest-N, and store its
receipt id in kv namespace digests under digest-N.
