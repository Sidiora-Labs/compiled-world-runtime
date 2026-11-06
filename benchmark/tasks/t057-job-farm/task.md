<!-- Copyright Sidiora Labs Inc. CWR® 2026-2027 -->
<!-- SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0 -->

# Run a shard farm

Run twelve jobs of kind shard, the i-th with n = i + 2 for i = 1 to 12. Store each result in kv
namespace shards under shard-i, and finally write /shards/summary.txt with one line shard-i=<result>
per job, for i = 1 to 12 in order.
