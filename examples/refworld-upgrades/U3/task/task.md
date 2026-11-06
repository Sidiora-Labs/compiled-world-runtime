<!-- Copyright Sidiora Labs Inc. CWR® 2026-2027 -->
<!-- SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0 -->

Submit a job of kind render lasting 5 ticks and a job of kind index lasting 40 ticks. Record their
ids in kv namespace jobs under the keys render and index. Store each job's result in kv namespace
results under the same key once the result is available.

In your first turn, submit both jobs and record their ids.
Every turn must end with status ok.
