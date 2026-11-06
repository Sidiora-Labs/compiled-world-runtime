#!/usr/bin/env bash
# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
#
# Release wrapper. Without options it runs `make release` (check, test, qualify, then
# tools/release.py) and tees the full log to artifacts/release.log. With --dry-run it runs only
# the release checks of tools/release.py and writes nothing.
# Usage: scripts/release.sh [--dry-run] [MAKE_VARIABLE=value ...]
# Exit status: that of make or tools/release.py; 2 on a usage error.

set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

python=${PYTHON:-python3}

if (($# > 0)) && [[ "$1" == --dry-run ]]; then
    shift
    (($# == 0)) || {
        printf 'usage: %s [--dry-run] [MAKE_VARIABLE=value ...]\n' "$0" >&2
        exit 2
    }
    exec "$python" -I tools/release.py --dry-run
fi

for argument in "$@"; do
    [[ "$argument" == *=* ]] || {
        printf 'release: unexpected argument %s; only MAKE_VARIABLE=value is passed to make\n' "$argument" >&2
        exit 2
    }
done

mkdir -p artifacts
make release "$@" 2>&1 | tee artifacts/release.log
