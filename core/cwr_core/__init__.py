# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Shared Python reference implementations for CWR tooling.

`cwr_core.kvx` is the strict KVX grammar and `cwr_core.envelope` is the closed eleven-code
failure envelope. Both are stdlib only and safe under `python3 -I`: a consumer adds the `core/`
directory to `sys.path` explicitly instead of relying on the current directory.
"""

from cwr_core import envelope, kvx

__all__ = ["envelope", "kvx"]
