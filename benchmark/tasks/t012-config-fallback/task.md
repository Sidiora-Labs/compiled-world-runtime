<!-- Copyright Sidiora Labs Inc. CWR® 2026-2027 -->
<!-- SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0 -->

# Back up a config file if present

If /config/app.ini exists, copy it to /config/app.ini.bak. If it does not exist, write the single
line missing to /config/STATUS instead.
