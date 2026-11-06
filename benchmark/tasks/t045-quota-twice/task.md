<!-- Copyright Sidiora Labs Inc. CWR® 2026-2027 -->
<!-- SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0 -->

# Cache entries around a mail merge

Do these steps in order. First store the content of /in/e1.txt in kv namespace cache under e1. Then
add the line Dear team, at the start of every file under /letters. Finally store /in/e2.txt and
/in/e3.txt in the same namespace under e2 and e3. Entries whose keys start with stale- may be
removed; no other existing entry may be removed.
