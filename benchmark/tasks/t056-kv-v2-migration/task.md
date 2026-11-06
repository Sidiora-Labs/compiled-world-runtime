<!-- Copyright Sidiora Labs Inc. CWR® 2026-2027 -->
<!-- SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0 -->

# Migrate user records

Migrate kv namespace v1 to namespace v2: every key user:NN in v1 becomes key NN in v2, with the
value rewritten from name|email to email|name. Write /migrations/v2.txt listing the keys of v2, one
per line, sorted. Namespace v1 must end up empty.
