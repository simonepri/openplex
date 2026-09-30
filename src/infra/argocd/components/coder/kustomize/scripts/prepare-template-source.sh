#!/bin/sh
# Clones and checks out the declared Git repository revision for Coder template publication.

set -eu

: "${SOURCE_REPOSITORY:?SOURCE_REPOSITORY is required}"
: "${SOURCE_REVISION:?SOURCE_REVISION is required}"

export GIT_LFS_SKIP_SMUDGE=1

if [ -n "${GIT_USERNAME_FILE:-}" ] || [ -n "${GIT_TOKEN_FILE:-}" ]; then
  if [ -s "${GIT_USERNAME_FILE:-}" ] && [ -s "${GIT_TOKEN_FILE:-}" ]; then
    export GIT_ASKPASS=/program/git-askpass.sh
    export GIT_TERMINAL_PROMPT=0
  fi
fi

git init /source/repository
git -C /source/repository remote add origin "${SOURCE_REPOSITORY}"
git -C /source/repository fetch --depth=1 origin "${SOURCE_REVISION}"
git -C /source/repository checkout --detach FETCH_HEAD
