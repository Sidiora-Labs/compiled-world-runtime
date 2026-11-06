#!/usr/bin/env bash
# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
#
# Run one pre-registered hypothesis campaign through the experiment harness.
# Usage: scripts/campaign.sh h1|h2|h3|h4 [--seeds N]
# The campaign file is benchmark/campaigns/hN.kvx (tasks 5.2 for h1 and h2, 5.3 for h3 and h4).
# The harness reads credentials itself; this script never touches them. Exit status: the
# harness's, or 2 on a usage error or a missing input.

set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

usage() {
    printf 'usage: %s h1|h2|h3|h4 [--seeds N]\n' "$0" >&2
    exit 2
}

die() {
    printf 'campaign: %s\n' "$1" >&2
    exit 2
}

(($# >= 1)) || usage
hypothesis=$1
shift
case "$hypothesis" in
    h1 | h2) owner=5.2 ;;
    h3 | h4) owner=5.3 ;;
    *) usage ;;
esac

seeds=()
while (($# > 0)); do
    case "$1" in
        --seeds)
            (($# >= 2)) || usage
            [[ "$2" =~ ^[1-9][0-9]*$ ]] || die "--seeds needs a positive integer, got '$2'"
            seeds=(--seeds "$2")
            shift 2
            ;;
        *) usage ;;
    esac
done

campaign="benchmark/campaigns/${hypothesis}.kvx"
runner="benchmark/harness/run.py"
python=${PYTHON:-python3}

[[ -f "$campaign" ]] || die "$campaign does not exist yet; task $owner creates it from benchmark/preregistration.kvx"
[[ -f "$runner" ]] || die "$runner is missing; task 5.1 provides the runner"
grep -q -- '"--campaign"' "$runner" || die "$runner does not accept --campaign yet; task 5.1 binds the runner to campaign files"

exec "$python" -I "$runner" --hypothesis "${hypothesis^^}" --campaign "$campaign" "${seeds[@]}"
