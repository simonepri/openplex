#!/usr/bin/env bash
# Test dotenv linting execution by verifying acceptance of valid templates and rejection of syntax errors.

set -euo pipefail

dotenv_check="${PWD}/${1:?missing dotenv check path}"
dotenv_linter="${PWD}/${2:?missing dotenv-linter path}"
fixture_dir="${TEST_TMPDIR:-$(mktemp -d)}"
valid_fixture="${fixture_dir}/.env.example"
invalid_fixture="${fixture_dir}/invalid.env"

printf 'ALPHA=one\nBETA=two\n' >"${valid_fixture}"
printf 'ALPHA=one\nALPHA=two\n' >"${invalid_fixture}"

if ! "${dotenv_check}" "${dotenv_linter}" "${valid_fixture}"; then
  printf 'expected valid dotenv fixture to pass\n' >&2
  exit 1
fi

if "${dotenv_check}" "${dotenv_linter}" "${invalid_fixture}"; then
  printf 'expected duplicated dotenv key to fail\n' >&2
  exit 1
fi
