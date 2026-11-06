<!-- Copyright Sidiora Labs Inc. CWR® 2026-2027 -->
<!-- SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0 -->

# Sports roundup

Search for golf and for tennis, three results each. Write the golf result titles to /reports/golf.md
and the tennis result titles to /reports/tennis.md, one title per line, and both lists, golf first,
to /reports/sports.md. Lock /reports/sports.md, publish its content as the body of a post titled
Sports roundup under the idempotency key sports-1, and store the receipt id in kv namespace receipts
under the key sports-1.
