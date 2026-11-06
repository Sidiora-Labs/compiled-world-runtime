<!-- Copyright Sidiora Labs Inc. CWR® 2026-2027 -->
<!-- SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0 -->

# Resubmit mis-sized jobs

Jobs enc-a, enc-b and enc-c were submitted with the wrong size. Cancel them, run three new jobs of
kind encode with n = 4 instead, and store the new results in kv namespace results under enc-a, enc-b
and enc-c respectively.
