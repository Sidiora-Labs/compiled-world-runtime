#!/usr/bin/env python3
# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
"""Validate a commit message against the CWR format.

Usage:
    python3 tools/commit_msg.py <message-file>      # as the commit-msg hook
    python3 tools/commit_msg.py --example           # print a valid example

Format:

    [<scope>] <Imperative summary, capitalised, no final period, 72 chars max>

    <Body: what changed and why. Wrapped at 72 columns.>

    Task: W.N                       (required for spec:cwr/W.N scopes)
    Cases: CWR-NNN[, CWR-NNN]       (required for spec:cwr/W.N scopes)
    Verify: <command> (exit <code>) (required for spec:cwr/W.N scopes)
    Co-Authored-By: Name <email>    (required when an AI agent wrote the change)

Scopes: spec:cwr/W.N for work on a specification task; spec:cwr for the pack
itself; docs, tooling, review:<area>, suite, campaign, release, chore.
Exit 0 when valid, 1 with one line per violation otherwise.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

SCOPE = r"spec:cwr/\d+\.\d+|spec:cwr|docs|tooling|review:[a-z0-9-]+|suite|campaign|release|chore"
SUBJECT = re.compile(rf"^\[({SCOPE})\] [A-Z][^\n]*[^.\s]$")
TASK_SCOPE = re.compile(r"^\[spec:cwr/(\d+\.\d+)\]")
TRAILER = re.compile(r"^(Task|Cases|Verify|Co-Authored-By|Signed-off-by|Refs): \S")
MAX_SUBJECT = 72
MAX_BODY = 72

EXAMPLE = """[spec:cwr/2.4] Recover a killed turn and replay the ledger

Recovery keeps every primitive the ledger committed and discards the
one in flight; replay rebuilds state from the ledger and recorded
cassettes and refuses a tampered chain.

Task: 2.4
Cases: CWR-010
Verify: python3 tests/cwr/cases/2_4.py (exit 0)
Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
"""


def validate(message: str) -> list[str]:
    """Return every violation in `message`; an empty list means the message is valid."""
    lines = [line for line in message.splitlines() if not line.startswith("#")]
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines:
        return ["empty message"]
    errors: list[str] = []
    subject = lines[0]
    if not SUBJECT.match(subject):
        errors.append(
            "subject must be '[<scope>] Capitalised summary' without a final period; "
            f"scopes: {SCOPE}"
        )
    if len(subject) > MAX_SUBJECT:
        errors.append(f"subject is {len(subject)} chars; maximum {MAX_SUBJECT}")
    if len(lines) > 1 and lines[1].strip():
        errors.append("second line must be blank")
    body = lines[2:]
    for number, line in enumerate(body, start=3):
        if len(line) > MAX_BODY and not TRAILER.match(line) and "://" not in line:
            errors.append(f"line {number} is {len(line)} chars; wrap at {MAX_BODY}")
    trailers = {m.group(1) for m in (TRAILER.match(line) for line in body) if m}
    task = TASK_SCOPE.match(subject)
    if task:
        for name in ("Task", "Cases", "Verify"):
            if name not in trailers:
                errors.append(f"{name}: trailer is required for scope spec:cwr/{task.group(1)}")
        task_line = next((line for line in body if line.startswith("Task: ")), "")
        if task_line and task_line != f"Task: {task.group(1)}":
            errors.append("Task: trailer must equal the task id in the subject scope")
        cases_line = next((line for line in body if line.startswith("Cases: ")), "")
        if cases_line and not re.fullmatch(r"Cases: CWR-\d{3}(, CWR-\d{3})*", cases_line):
            errors.append("Cases: trailer must list CWR-NNN ids separated by ', '")
        verify_line = next((line for line in body if line.startswith("Verify: ")), "")
        if verify_line and not re.search(r"\(exit \d+\)$", verify_line):
            errors.append("Verify: trailer must end with '(exit <code>)'")
    if not body:
        errors.append("a body explaining what changed and why is required")
    return errors


def main(argv: list[str]) -> int:
    if argv == ["--example"]:
        sys.stdout.write(EXAMPLE)
        return 0
    if len(argv) != 1:
        sys.stderr.write(__doc__ or "")
        return 2
    message = Path(argv[0]).read_text(encoding="utf-8")
    errors = validate(message)
    for error in errors:
        print(f"commit-msg: {error}", file=sys.stderr)
    if errors:
        print("commit-msg: see `python3 tools/commit_msg.py --example`", file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
