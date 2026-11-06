# Copyright Sidiora Labs Inc. CWR® 2026-2027
# SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0
#
# CWR: Compiled World Runtime. Targets:
#   build      compile the Rust workspace in release mode
#   test       cargo tests plus harness and stub self-tests
#   fmt        rewrite Rust and Python sources into the repository format
#   fmt-check  fail when any source is not formatted
#   lint       clippy (warnings are errors) and ruff
#   headers    fail when any file lacks the Sidiora source header
#   headers-apply insert the header where it is missing
#   validate   structural validation of the specification pack
#   check      headers, fmt-check, lint, validate (the pre-merge gate)
#   qualify    run every task driver through the qualification runner
#   closure    assemble evidence for all cases and print the release verdict
#   vendor     vendor crate sources for offline reproducible builds
#   release    build, qualify and bundle release artifacts into artifacts/
#   clean      remove build outputs

CARGO      ?= cargo
PYTHON     ?= python3
RUFF       ?= ruff
TARGET_DIR ?= target
export CARGO_TARGET_DIR := $(TARGET_DIR)

PY_DIRS = benchmark core sdk spec tests tools scripts

.PHONY: all build test fmt fmt-check lint headers headers-apply validate check qualify closure vendor release clean

all: build

build:
	$(CARGO) build --workspace --release

test: build
	$(CARGO) test --workspace
	$(PYTHON) -m unittest discover -s benchmark/harness/tests -t .
	$(PYTHON) -m unittest discover -s benchmark/predicates/tests -t .
	$(PYTHON) -m unittest discover -s core/tests -t .
	$(PYTHON) -m unittest discover -s tools/tests -t .
	$(PYTHON) benchmark/stubs/selftest.py

fmt:
	$(CARGO) fmt --all
	$(RUFF) format $(PY_DIRS)
	$(RUFF) check --fix $(PY_DIRS)

fmt-check:
	$(CARGO) fmt --all -- --check
	$(RUFF) format --check $(PY_DIRS)

lint:
	$(CARGO) clippy --workspace --all-targets -- -D warnings
	$(RUFF) check $(PY_DIRS)

headers:
	$(PYTHON) tools/header.py --check

headers-apply:
	$(PYTHON) tools/header.py --apply

validate:
	$(PYTHON) spec/cwr/tools/validate_spec.py

check: headers fmt-check lint validate

qualify: build
	@for n in $$(seq -f "%03g" 1 20); do echo "== CWR-$$n"; $(PYTHON) tests/cwr/qualify.py --case CWR-$$n || exit 1; done

closure:
	$(PYTHON) tests/cwr/closure.py

vendor:
	$(CARGO) vendor vendor/cargo > /dev/null
	@echo "vendor/cargo refreshed; to build from it, add the source replacement shown in vendor/README.md to .cargo/config.toml"

release: check test qualify
	$(PYTHON) tools/release.py

clean:
	$(CARGO) clean
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
