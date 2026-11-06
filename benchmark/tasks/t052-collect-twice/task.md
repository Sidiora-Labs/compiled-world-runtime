<!-- Copyright Sidiora Labs Inc. CWR® 2026-2027 -->
<!-- SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0 -->

# Collect nightly jobs around a conversion

Do these steps in order. First store the result of job nightly-a in kv namespace nightly under
nightly-a. Then, for every file under /csv named NAME.csv, write /tsv/NAME.tsv with every comma
replaced by a tab character. Finally store the results of jobs nightly-b and nightly-c in the same
namespace under their own names.
