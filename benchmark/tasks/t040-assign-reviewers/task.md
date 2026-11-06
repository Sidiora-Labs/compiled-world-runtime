<!-- Copyright Sidiora Labs Inc. CWR® 2026-2027 -->
<!-- SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0 -->

# Assign reviewers at random

Assign each file under /tasks a reviewer chosen with the world's random source from /reviewers.txt
(one name per line), and store the reviewer's name in kv namespace assign under the task file's full
path.
