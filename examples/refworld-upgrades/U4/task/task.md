<!-- Copyright Sidiora Labs Inc. CWR® 2026-2027 -->
<!-- SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0 -->

Copy every file under /receipts/ into kv namespace receipts, one value per file, keyed by the file
name: the content of /receipts/r1.txt goes under key r1.txt. When a file's content is too long for a
single kv value, store it in consecutive parts under the keys <name>.1, <name>.2 and so on instead, so
that the parts concatenated in order equal the file's content. Leave the files in place.

In your first turn, copy r1.txt and r2.txt only.
From your second turn on, copy the remaining files.
