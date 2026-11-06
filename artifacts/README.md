<!-- Copyright Sidiora Labs Inc. CWR® 2026-2027 -->
<!-- SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0 -->

# artifacts: release bundles

`make release` (through [tools/release.py](../tools/release.py)) writes release bundles here:

```text
artifacts/cwr-<version>.tar.gz          the tree under cwr-<version>/, bin/cwr when built,
                                        and MANIFEST.sha256 (one sha256 per bundled file)
artifacts/cwr-<version>.tar.gz.sha256   the tarball's own sha256
```

Verify with `sha256sum -c cwr-<version>.tar.gz.sha256` here, and with
`sha256sum -c MANIFEST.sha256` inside the extracted directory. The archive is byte-reproducible
from the same tree. `python3 tools/release.py --snapshot` produces `cwr-<version>-snapshot.*` for
an unpinned tree; a snapshot is never a release.

Everything in this directory except this file and `.gitkeep` is ignored by `.gitignore`.
A bundle is reproducible from the tagged tree; publishing one is a release-owner decision recorded
in [RELEASE.md](../RELEASE.md), and a bundle is not evidence that any qualification case passed.
