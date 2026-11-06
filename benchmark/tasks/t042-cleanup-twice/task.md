<!-- Copyright Sidiora Labs Inc. CWR® 2026-2027 -->
<!-- SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0 -->

# Clean reports between two cleanups

Do these steps in order. First remove /scratch/session.tmp. Then, for every file under /reports/raw,
write a copy with the same name under /reports/clean in which the spaces at the end of every line
are removed. Finally remove /scratch/build.tmp and /scratch/cache.tmp. /scratch/keep.txt and the raw
reports must stay as they are.
