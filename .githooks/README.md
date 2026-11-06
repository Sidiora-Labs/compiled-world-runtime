<!-- Copyright Sidiora Labs Inc. CWR® 2026-2027 -->
<!-- SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0 -->

# Git hooks that enforce the commit rules

Git runs these hooks once you point it at this directory:

```sh
git config core.hooksPath .githooks
```

| Hook | Refuses |
| --- | --- |
| `pre-commit` | A staged credential file, a staged hunk that looks like a credential value, or a staged file without the Sidiora source header (checked by `tools/header.py`). |
| `commit-msg` | A message that does not follow the format in [CONTRIBUTING.md](../CONTRIBUTING.md), checked by `tools/commit_msg.py`. |

Git ignores this README: only files named after a hook run.
