#!/usr/bin/env bash
# Answer Bazel credential helper requests with the BuildBuddy API key that `bb login` stores in git config, failing when missing.

set -euo pipefail

command="${1:-}"

if [[ ${command} != get ]]; then
  printf 'unsupported credential helper command: %s\n' "${command}" >&2
  exit 1
fi

# Bazel writes the request to stdin; drain it so the write never fails.
cat >/dev/null

# Bazel runs the helper from the workspace root. CI and explicit rc configs
# yield an empty response, while a missing key fails with an actionable error.
python3 - <<'EOF'
import json
import os
import re
import subprocess
import sys

# 1. On BuildBuddy CI runners, runner credentials are already provided via
# --remote_header from buildbuddy.bazelrc. Emitting identity headers here causes
# "PERMISSION_DENIED: multiple identity headers present".
if os.environ.get("BUILDBUDDY_INVOCATION_ID"):
    print("{}")
    sys.exit(0)

if "TEST_TMPDIR" not in os.environ:
    if os.path.exists("/home/buildbuddy"):
        print("{}")
        sys.exit(0)

# 2. If an rc file already configures x-buildbuddy-api-key, Bazel natively sends
# it via --remote_header. Emitting it here would produce duplicate headers.
rc_files = [os.path.join(os.getcwd(), "buildbuddy.bazelrc")]

if "TEST_TMPDIR" not in os.environ:
    rc_files.extend([
        "/home/buildbuddy/workspace/repo-root/buildbuddy.bazelrc",
        "/home/buildbuddy/workspace/buildbuddy.bazelrc",
        "/var/run/workspace/buildbuddy/buildbuddy.bazelrc",
        "/var/run/workspace/buildbuddy/credentials.bazelrc",
        os.path.expanduser("~/.bazelrc"),
        "/etc/bazel.bazelrc",
    ])
    script_dir = os.path.dirname(os.path.abspath(__file__))
    cur = script_dir
    for _ in range(6):
        if os.path.isfile(os.path.join(cur, "MODULE.bazel")) or os.path.isfile(os.path.join(cur, "WORKSPACE")):
            rc_files.append(os.path.join(cur, "buildbuddy.bazelrc"))
            break
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent

for rc in rc_files:
    if os.path.isfile(rc):
        try:
            with open(rc, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    stripped = line.strip()
                    if stripped.startswith("#"):
                        continue
                    if "x-buildbuddy-api-key" in stripped:
                        print("{}")
                        sys.exit(0)
        except Exception:
            pass

# 3. Read key from git config (set by `bb login`)
key = None
try:
    res = subprocess.run(
        ["git", "config", "--get", "buildbuddy.api-key"],
        capture_output=True,
        text=True,
        check=False,
    )
    val = res.stdout.strip()
    if val and re.match(r"^[A-Za-z0-9_-]+$", val):
        key = val
except Exception:
    pass

# 4. Fall back to BUILDBUDDY_API_KEY environment variable
if not key:
    env_key = os.environ.get("BUILDBUDDY_API_KEY", "").strip()
    if env_key and re.match(r"^[A-Za-z0-9_-]+$", env_key):
        key = env_key

if key:
    print(json.dumps({"headers": {"x-buildbuddy-api-key": [key]}}, separators=(',', ':')))
else:
    print("BuildBuddy API key missing: run `mise run login` in the depot checkout.", file=sys.stderr)
    sys.exit(1)
EOF
