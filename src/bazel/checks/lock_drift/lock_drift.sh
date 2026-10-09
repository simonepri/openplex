#!/usr/bin/env bash
# Verify that package manager lockfiles match manifest specifications without modifying files.

set -euo pipefail

pnpm_bin=""
uv_bin=""
if [[ $# -ge 1 ]] && [[ $1 == *pnpm* ]] && [[ -f $1 ]]; then
  pnpm_bin="$1"
  if [[ ${pnpm_bin#/} == "${pnpm_bin}" ]]; then pnpm_bin="${PWD}/${pnpm_bin}"; fi
  shift
fi
if [[ $# -ge 1 ]] && [[ $1 == *uv* ]] && [[ -f $1 ]]; then
  uv_bin="$1"
  if [[ ${uv_bin#/} == "${uv_bin}" ]]; then uv_bin="${PWD}/${uv_bin}"; fi
  shift
fi

root="${BUILD_WORKSPACE_DIRECTORY:-$(git rev-parse --show-toplevel)}"
cd "${root}"

# 1. Verify pnpm-lock.yaml matches package.json without modifying anything
if [[ -f "${root}/pnpm-lock.yaml" && -f "${root}/package.json" ]]; then
  echo "Checking pnpm lockfile drift..."
  if [[ -z ${pnpm_bin} ]]; then
    if command -v pnpm >/dev/null 2>&1; then
      pnpm_bin="$(command -v pnpm)"
    else
      for candidate in "${HOME}/.local/share/mise/shims/pnpm" "${HOME}/.local/share/mise/installs/pnpm"/*/pnpm /usr/local/bin/pnpm; do
        if [[ -x ${candidate} ]]; then
          pnpm_bin="${candidate}"
          break
        fi
      done
    fi
  fi
  if [[ -z ${pnpm_bin} ]]; then
    echo "ERROR: pnpm binary not found in PATH or mise installations. Please run 'mise install' to ensure pnpm is available." >&2
    exit 1
  fi

  if ! BAZEL_BINDIR="${BAZEL_BINDIR:-.}" "${pnpm_bin}" install --lockfile-only --frozen-lockfile --ignore-scripts; then
    echo "ERROR: pnpm-lock.yaml is not up to date with package.json." >&2
    echo "Run 'pnpm install --lockfile-only' to regenerate." >&2
    exit 1
  fi
fi

# 2. Verify requirements locks match pyproject.toml without modifying them
if [[ -f "${root}/pyproject.toml" ]]; then
  if [[ -z ${uv_bin} ]]; then
    if command -v uv >/dev/null 2>&1; then
      uv_bin="$(command -v uv)"
    else
      for candidate in "${HOME}/.local/share/mise/shims/uv" "${HOME}/.local/share/mise/installs/uv"/*/uv /usr/local/bin/uv; do
        if [[ -x ${candidate} ]]; then
          uv_bin="${candidate}"
          break
        fi
      done
    fi
  fi
  if [[ -z ${uv_bin} ]]; then
    echo "ERROR: uv binary not found in PATH or mise installations." >&2
    exit 1
  fi

  tmp_dir="$(mktemp -d)"
  trap 'rm -rf "${tmp_dir}"' EXIT

  if [[ -f "${root}/requirements_lock.txt" ]]; then
    echo "Checking requirements_lock.txt drift..."
    cp "${root}/requirements_lock.txt" "${tmp_dir}/requirements_lock.txt"
    if ! "${uv_bin}" pip compile "${root}/pyproject.toml" \
      --python-version 3.13 \
      --universal \
      --no-strip-extras \
      --generate-hashes \
      --custom-compile-command "uv pip compile pyproject.toml --python-version 3.13 --universal --no-strip-extras --generate-hashes -o requirements_lock.txt" \
      -o "${tmp_dir}/requirements_lock.txt" >/dev/null 2>&1; then
      echo "ERROR: Failed to compile pyproject.toml to requirements_lock.txt" >&2
      exit 1
    fi
    if ! diff -u "${root}/requirements_lock.txt" "${tmp_dir}/requirements_lock.txt" >&2; then
      echo "ERROR: requirements_lock.txt does not match pyproject.toml." >&2
      echo "Run 'uv pip compile pyproject.toml --python-version 3.13 --universal --no-strip-extras --generate-hashes -o requirements_lock.txt' or 'mise run fix' to regenerate." >&2
      exit 1
    fi
  fi

  if [[ -f "${root}/requirements_dev_lock.txt" ]]; then
    echo "Checking requirements_dev_lock.txt drift..."
    cp "${root}/requirements_dev_lock.txt" "${tmp_dir}/requirements_dev_lock.txt"
    if ! "${uv_bin}" pip compile --group dev \
      --python-version 3.13 \
      --universal \
      --no-strip-extras \
      --generate-hashes \
      --custom-compile-command "uv pip compile --group dev --python-version 3.13 --universal --no-strip-extras --generate-hashes -o requirements_dev_lock.txt" \
      -o "${tmp_dir}/requirements_dev_lock.txt" >/dev/null 2>&1; then
      echo "ERROR: Failed to compile pyproject.toml dev group to requirements_dev_lock.txt" >&2
      exit 1
    fi
    if ! diff -u "${root}/requirements_dev_lock.txt" "${tmp_dir}/requirements_dev_lock.txt" >&2; then
      echo "ERROR: requirements_dev_lock.txt does not match pyproject.toml." >&2
      echo "Run 'uv pip compile --group dev --python-version 3.13 --universal --no-strip-extras --generate-hashes -o requirements_dev_lock.txt' or 'mise run fix' to regenerate." >&2
      exit 1
    fi
  fi

  if [[ -f "${root}/uv.lock" ]]; then
    echo "Checking uv.lock drift..."
    if ! "${uv_bin}" lock --check; then
      echo "ERROR: uv.lock is not up to date with pyproject.toml." >&2
      echo "Run 'uv lock' to regenerate." >&2
      exit 1
    fi
  fi
fi

echo "All lockfiles are in sync with manifests."
