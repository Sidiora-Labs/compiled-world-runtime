#!/usr/bin/env bash
# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
#
# Prepare a development host for CWR: install the formatters and linters `make check` runs, then
# check the interpreters and inputs the build, drivers and harness need. Idempotent; installs only
# what is missing so an existing ruff version is never changed underneath the formatter.
# Usage: scripts/bootstrap.sh [--dry-run]
# --dry-run runs every check and installs nothing; a missing tool is reported as a failure.
# Exit status: 0 when every required check passes, 1 otherwise, 2 on a usage error. Warnings do
# not fail.

set -euo pipefail

dry_run=0
case "$*" in
    "") ;;
    --dry-run) dry_run=1 ;;
    *)
        printf 'usage: %s [--dry-run]\n' "$0" >&2
        exit 2
        ;;
esac

cd "$(dirname "${BASH_SOURCE[0]}")/.."

failures=0
ok() { printf 'ok    %s\n' "$1"; }
warn() { printf 'warn  %s\n' "$1"; }
fail() {
    printf 'FAIL  %s\n' "$1"
    failures=$((failures + 1))
}

if command -v cargo >/dev/null 2>&1; then
    ok "cargo: $(cargo --version)"
else
    fail "cargo not found; install the Rust toolchain from rustup.rs"
fi

for component in rustfmt clippy; do
    if [[ "$component" == rustfmt ]]; then probe=(cargo fmt --version); else probe=(cargo clippy --version); fi
    if "${probe[@]}" >/dev/null 2>&1; then
        ok "$component: $("${probe[@]}")"
    elif ((dry_run)); then
        fail "$component missing; scripts/bootstrap.sh would install it with rustup"
    elif command -v rustup >/dev/null 2>&1 && rustup component add "$component"; then
        ok "$component: installed with rustup"
    else
        fail "$component missing and rustup could not install it"
    fi
done

if command -v python3.12 >/dev/null 2>&1; then
    ok "python3.12: $(python3.12 --version 2>&1)"
else
    fail "python3.12 not found; scripts, drivers and the turn sandbox require Python 3.12"
fi

if python3 -c 'import sys; sys.exit(0 if sys.version_info[:2] >= (3, 12) else 1)' 2>/dev/null; then
    ok "python3: $(python3 --version 2>&1)"
else
    fail "python3 is older than 3.12 or missing; make uses python3 by default"
fi

if command -v ruff >/dev/null 2>&1; then
    ok "ruff: $(ruff --version)"
elif ((dry_run)); then
    fail "ruff missing; scripts/bootstrap.sh would install it with pip"
elif python3 -m pip install --user --break-system-packages ruff; then
    if command -v ruff >/dev/null 2>&1; then
        ok "ruff: installed $(ruff --version)"
    else
        fail "ruff installed but not on PATH; add $(python3 -m site --user-base)/bin to PATH"
    fi
else
    fail "pip could not install ruff"
fi

if command -v bwrap >/dev/null 2>&1; then
    ok "bwrap: $(bwrap --version 2>&1)"
else
    warn "bwrap not found; cwr instance turn refuses to run scripts without it (crates/cwr-kernel/SANDBOX.md)"
fi

# Presence and permissions of the credentials file only: it is never read, printed or copied here.
if [[ -f .env ]]; then
    ok ".env present"
    mode=$(stat -c '%a' .env)
    if [[ "${mode: -2}" != "00" ]]; then
        warn ".env mode is $mode; consider chmod 600 .env"
    fi
else
    fail ".env missing; model campaigns (CWR-017, CWR-018) are BLOCKED without gateway credentials"
fi

if ((failures > 0)); then
    printf '%d required check(s) failed\n' "$failures"
    exit 1
fi
printf 'bootstrap complete; next: make check\n'
