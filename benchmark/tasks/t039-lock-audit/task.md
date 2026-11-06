<!-- Copyright Sidiora Labs Inc. CWR® 2026-2027 -->
<!-- SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0 -->

# Audit vault locks

For every file under /vault, store in kv namespace vault, under the file's full path, the word
locked or unlocked according to its current state. Do not change any lock.
