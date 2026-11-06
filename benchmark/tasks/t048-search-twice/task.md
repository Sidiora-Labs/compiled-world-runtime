<!-- Copyright Sidiora Labs Inc. CWR® 2026-2027 -->
<!-- SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0 -->

# Two search batches around a filing run

Do these steps in order. First run every query in /q/early.txt (one per line) with the largest limit
the search service accepts and write the title of each top hit, one per line in query order, to
/out/early.md. Then move every file under /incoming to /sorted with the same name and lock it there.
Finally do the same as the first step for /q/late.txt, writing /out/late.md.
