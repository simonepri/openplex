#!/usr/bin/env bash
# Test JSON schema validation wrapper against conforming schema definitions and invalid schemas.

set -euo pipefail

json_schemas_check="${PWD}/${1:?missing json_schemas check path}"
check_jsonschema="${PWD}/${2:?missing check-jsonschema path}"
fixture_dir="$(mktemp -d /tmp/json_schemas_test.XXXXXX)"
trap 'rm -rf "${fixture_dir}"' EXIT

valid_fixture="${fixture_dir}/valid.schema.json"
invalid_fixture="${fixture_dir}/invalid.schema.json"

cat <<'EOF' >"${valid_fixture}"
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "properties": {
    "name": { "type": "string" }
  },
  "additionalProperties": false
}
EOF

cat <<'EOF' >"${invalid_fixture}"
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": 12345
}
EOF

# Test valid fixture
if ! "${json_schemas_check}" "${check_jsonschema}" "${valid_fixture}"; then
  printf 'expected valid schema fixture to pass metaschema check\n' >&2
  exit 1
fi

# Test invalid fixture fails
if "${json_schemas_check}" "${check_jsonschema}" "${invalid_fixture}" 2>/dev/null; then
  printf 'expected invalid metaschema fixture to fail\n' >&2
  exit 1
fi
