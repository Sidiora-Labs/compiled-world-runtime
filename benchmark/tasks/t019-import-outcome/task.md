<!-- Copyright Sidiora Labs Inc. CWR® 2026-2027 -->
<!-- SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0 -->

# Record a job outcome

Record the outcome of job job-import in kv namespace jobs under the key job-import: its result if it
finished successfully, otherwise the word failed. If it is still running, wait until it is not.
