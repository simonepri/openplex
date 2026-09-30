#!/usr/bin/env bash
# Adapts Rust and Cargo linker invocations to route through the mise-managed Zig C compiler toolchain.

set -euo pipefail

arguments=()
for argument in "$@"; do
  if [[ ${argument} != "-Wl,--fix-cortex-a53-843419" ]]; then
    arguments+=("${argument}")
  fi
done

zig_bin="$(mise where zig)/zig"
exec "${zig_bin}" cc "${arguments[@]}"
