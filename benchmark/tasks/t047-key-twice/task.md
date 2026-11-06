<!-- Copyright Sidiora Labs Inc. CWR® 2026-2027 -->
<!-- SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0 -->

# Two documents in two versions

Do these steps in order. First publish /drafts/plan-v1.txt as a post titled Plan under the
idempotency key plan, then publish the corrected /drafts/plan-v2.txt as a post titled Plan as well;
both versions must stay published. Store the receipt ids in kv namespace receipts under plan-v1 and
plan-v2. Then copy every file under /minutes to /minutes-archive with the same name. Finally do the
same for /drafts/budget-v1.txt and /drafts/budget-v2.txt with the title Budget, the key budget for
the first version, and receipts stored under budget-v1 and budget-v2.
