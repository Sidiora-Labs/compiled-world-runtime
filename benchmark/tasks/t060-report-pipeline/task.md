<!-- Copyright Sidiora Labs Inc. CWR® 2026-2027 -->
<!-- SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0 -->

# Quarterly report pipeline

Run four jobs of kind stats with n = 20, 25, 30 and 35. While they run, search for each of the
regions north, south, east, west, central, coastal, highland and island with a limit of three
results and write the result titles, one per line, to /report/<region>.md. When all four jobs are
done, write /report/summary.md with one line stats-<n>=<result> per job in the order of n, followed
by the eight region file names (for example north.md) in the order given above, one per line. Lock
it, publish it as a post titled Quarterly report under the idempotency key report-q, store the
receipt id in kv namespace receipts under report-q, and store each job result in kv namespace stats
under stats-<n>.
