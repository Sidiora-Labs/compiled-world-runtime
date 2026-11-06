<!-- Copyright Sidiora Labs Inc. CWR® 2026-2027 -->
<!-- SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0 -->

# Lock two folders around a memo import

Do these steps in order. First make sure every file under /legal is locked. Then store the content
of every file under /memos in kv namespace memos under the file's full path. Finally make sure every
file under /finance is locked.
