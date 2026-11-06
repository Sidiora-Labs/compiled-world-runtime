<!-- Copyright Sidiora Labs Inc. CWR® 2026-2027 -->
<!-- SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0 -->

# Two job batches around a conversion

Do these steps in order. First run three jobs of kind etl with n = 5 and store their results in kv
namespace etl under etl-1, etl-2 and etl-3 in submission order. Then, for every file under /csv,
write a copy with the same name under /csv-clean in which every semicolon is replaced by a comma.
Finally run three more jobs of kind etl with n = 5 and store their results under etl-4, etl-5 and
etl-6 in submission order.
