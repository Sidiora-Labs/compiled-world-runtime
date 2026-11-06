<!-- Copyright Sidiora Labs Inc. CWR® 2026-2027 -->
<!-- SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0 -->

# Release bundles

`make release` writes release bundles here through [tools/release.py](../tools/release.py). A bundle
is byte-reproducible: the same tree always produces the same archive.

| File | Content |
| --- | --- |
| `cwr-<version>.tar.gz` | The tree under `cwr-<version>/`, the release `cwr` binary as `bin/cwr` when it has been built, and `MANIFEST.sha256` with one sha256 per bundled file |
| `cwr-<version>.tar.gz.sha256` | The sha256 of the archive itself |

## Verify a bundle

```sh
sha256sum -c cwr-<version>.tar.gz.sha256
tar xzf cwr-<version>.tar.gz
cd cwr-<version> && sha256sum -c MANIFEST.sha256
```

## Snapshots

`python3 tools/release.py --snapshot` bundles a tree whose release is not pinned. The files are named
`cwr-<version>-snapshot.*`. A snapshot is never a release.

## Rules

- Everything here except this file and `.gitkeep` is ignored by version control.
- Publishing a bundle is a release-owner decision, made through the procedure in
  [RELEASE.md](../RELEASE.md).
- A bundle proves that the tree was packaged. It is not evidence that any qualification case passed.
