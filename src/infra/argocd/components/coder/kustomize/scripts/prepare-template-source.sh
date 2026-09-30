#!/bin/sh
# Clones and checks out the declared Git repository revision for Coder template publication.

set -eu

: "${SOURCE_REPOSITORY:?SOURCE_REPOSITORY is required}"
: "${SOURCE_REVISION:?SOURCE_REVISION is required}"

export GIT_LFS_SKIP_SMUDGE=1

if [ -n "${GIT_SSH_KEY_FILE:-}" ] && [ -s "${GIT_SSH_KEY_FILE:-}" ]; then
  state_dir="${STATE_DIR:-/state}"
  mkdir -p "${state_dir}/.ssh"
  cp "${GIT_SSH_KEY_FILE}" "${state_dir}/.ssh/id_rsa"
  chmod 600 "${state_dir}/.ssh/id_rsa"
  export GIT_SSH_COMMAND="ssh -i ${state_dir}/.ssh/id_rsa -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=${state_dir}/.ssh/known_hosts"
elif [ -n "${GIT_USERNAME_FILE:-}" ] || [ -n "${GIT_TOKEN_FILE:-}" ]; then
  if [ -s "${GIT_USERNAME_FILE:-}" ] && [ -s "${GIT_TOKEN_FILE:-}" ]; then
    export GIT_ASKPASS=/program/git-askpass.sh
    export GIT_TERMINAL_PROMPT=0
  fi
fi

git init /source/repository
git -C /source/repository remote add origin "${SOURCE_REPOSITORY}"
git -C /source/repository fetch --depth=1 origin "${SOURCE_REVISION}"
git -C /source/repository checkout --detach FETCH_HEAD
