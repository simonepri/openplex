#!/usr/bin/env bash
# Verifies rclone S3 gateway prefix isolation and owner boundary permissions.

# shellcheck disable=SC2310,SC2312
set -euo pipefail

# A cached result must mean the boundary held, so a missing daemon or image
# fails the test instead of passing it.
if ! docker info >/dev/null 2>&1; then
  echo "Docker daemon is not reachable." >&2
  exit 1
fi

# LINT.IfChange(workspace-backup-proxy-pinned-rclone)
readonly rclone_image='rclone/rclone:1.75.1@sha256:45401ad7410db1d67ffdb58e19059ad20b0d8e0285a60e38bbec55cc1019c7a5'
# LINT.ThenChange(//MODULE.bazel:workspace-backup-proxy-base-images)

if ! docker image inspect "${rclone_image}" >/dev/null 2>&1; then
  if ! docker pull "${rclone_image}" >/dev/null 2>&1; then
    echo "rclone image is not cached and cannot be pulled." >&2
    exit 1
  fi
fi
readonly gateway_access_key='GATEWAYBOUNDARY00001'
readonly gateway_secret_key='gateway-fixture-secret-0123456789abcdef0123456789abcdef'
readonly proxy_access_key='PROXYBOUNDARY0000001'
readonly proxy_secret_key='proxy-fixture-secret-0123456789abcdef0123456789abcdef01234567'
readonly owner_id='11111111-1111-4111-8111-111111111111'
readonly sibling_owner_id='22222222-2222-4222-8222-222222222222'
readonly team='examples'
readonly sibling_team='neighbors'
readonly bucket='eaws-lh1'
test_root=$(mktemp -d)
readonly test_root
cd "${test_root}"
readonly network="workspace-backup-boundary-${RANDOM}-$$"
readonly data_volume="workspace-backup-data-${RANDOM}-$$"
readonly gateway_container="workspace-backup-gateway-${RANDOM}-$$"
readonly proxy_container="workspace-backup-proxy-${RANDOM}-$$"

cleanup() {
  status=$?
  trap - EXIT HUP INT TERM
  set +e
  docker rm --force "${proxy_container}" "${gateway_container}" >/dev/null 2>&1
  docker network rm "${network}" >/dev/null 2>&1
  docker volume rm --force "${data_volume}" >/dev/null 2>&1
  rm -rf -- "${test_root}"
  exit "${status}"
}
trap cleanup EXIT HUP INT TERM

repository_root="/data/${bucket}/backups/dev/${team}/repos/${owner_id}"
claims_root="/data/${bucket}/backups/dev/${team}/claims/${owner_id}"
sibling_owner_root="/data/${bucket}/backups/dev/${team}/repos/${sibling_owner_id}"
sibling_team_root="/data/${bucket}/backups/dev/${sibling_team}/repos/${owner_id}"
# LINT.IfChange(workspace-backup-proxy-directory-roots)
# rclone combine treats upstream paths as literal directories; options like :ro
# cannot be passed in the upstream path. Read-only enforcement is done by Envoy.
readonly owner_upstreams="repository=backend:${bucket}/backups/dev/${team}/repos/${owner_id}/ claims=backend:${bucket}/backups/dev/${team}/claims/${owner_id}/"
# LINT.ThenChange(//src/infra/definitions/workspaces/templates/dev/deployment.tf:workspace-backup-proxy-directory-roots)

docker network create "${network}" >/dev/null
docker volume create "${data_volume}" >/dev/null
docker run --rm \
  --volume "${data_volume}:/data" \
  --env BUCKET="${bucket}" \
  --env TEAM="${team}" \
  --env SIBLING_TEAM="${sibling_team}" \
  --env OWNER_ID="${owner_id}" \
  --env SIBLING_OWNER_ID="${sibling_owner_id}" \
  --entrypoint /bin/sh \
  "${rclone_image}" -eu -c '
		mkdir -p \
			"/data/$BUCKET/backups/dev/$TEAM/repos/$OWNER_ID" \
			"/data/$BUCKET/backups/dev/$TEAM/claims" \
			"/data/$BUCKET/backups/dev/$TEAM/repos/$SIBLING_OWNER_ID" \
			"/data/$BUCKET/backups/dev/$SIBLING_TEAM/repos/$OWNER_ID"
		printf %s claims-root-marker >"/data/$BUCKET/backups/dev/$TEAM/claims/$OWNER_ID"
		printf %s sibling-owner >"/data/$BUCKET/backups/dev/$TEAM/repos/$SIBLING_OWNER_ID/sentinel"
		printf %s sibling-team >"/data/$BUCKET/backups/dev/$SIBLING_TEAM/repos/$OWNER_ID/sentinel"
		chmod -R a+rwX /data
	'

docker run --detach \
  --name "${gateway_container}" \
  --network "${network}" \
  --user 1000:1000 \
  --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,size=16m,uid=1000,gid=1000 \
  --cap-drop ALL \
  --security-opt no-new-privileges \
  --volume "${data_volume}:/data" \
  --env "RCLONE_AUTH_KEY=\"${gateway_access_key},${gateway_secret_key}\"" \
  "${rclone_image}" serve s3 \
  --addr 0.0.0.0:9000 \
  --disable=PutStream \
  --force-path-style \
  --vfs-cache-mode off \
  /data >/dev/null

sleep 0.2
if [[ $(docker inspect --format '{{.State.Running}}' "${gateway_container}") != true ]]; then
  docker logs "${gateway_container}" >&2
  exit 1
fi

docker run --detach \
  --name "${proxy_container}" \
  --network "${network}" \
  --user 1000:1000 \
  --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,size=16m,uid=1000,gid=1000 \
  --cap-drop ALL \
  --security-opt no-new-privileges \
  --publish 127.0.0.1::19847 \
  --env RCLONE_CONFIG=/tmp/rclone.conf \
  --env RCLONE_CONFIG_BACKEND_TYPE=s3 \
  --env RCLONE_CONFIG_BACKEND_PROVIDER=Rclone \
  --env RCLONE_CONFIG_BACKEND_ENDPOINT="http://${gateway_container}:9000" \
  --env RCLONE_CONFIG_BACKEND_FORCE_PATH_STYLE=true \
  --env RCLONE_CONFIG_BACKEND_USE_MULTIPART_UPLOADS=false \
  --env RCLONE_CONFIG_BACKEND_ACCESS_KEY_ID="${gateway_access_key}" \
  --env RCLONE_CONFIG_BACKEND_SECRET_ACCESS_KEY="${gateway_secret_key}" \
  --env RCLONE_CONFIG_OWNER_TYPE=combine \
  --env "RCLONE_CONFIG_OWNER_UPSTREAMS=${owner_upstreams}" \
  --env "RCLONE_AUTH_KEY=\"${proxy_access_key},${proxy_secret_key}\"" \
  "${rclone_image}" serve s3 \
  --addr 0.0.0.0:19847 \
  --disable=PutStream \
  --force-path-style \
  --vfs-cache-mode off \
  owner: >/dev/null

if ! proxy_address=$(docker port "${proxy_container}" 19847/tcp); then
  docker logs "${proxy_container}" >&2
  exit 1
fi
case "${proxy_address}" in
  127.0.0.1:[0-9]*) ;;
  *)
    printf 'unexpected Docker port mapping: %s\n' "${proxy_address}" >&2
    exit 1
    ;;
esac
proxy_port=${proxy_address##*:}
readonly proxy_address proxy_port

request() {
  local access_key=$1
  local secret_key=$2
  local method=$3
  local url=$4
  local payload=${5:-}
  local resolve=${6:-}
  local output=${test_root}/response
  local -a arguments=(
    --silent
    --show-error
    --noproxy '*'
    --path-as-is
    --output "${output}"
    --write-out '%{http_code}'
    --request "${method}"
  )
  if [[ -n ${access_key} ]]; then
    arguments+=(
      --aws-sigv4 aws:amz:us-east-1:s3
      --user "${access_key}:${secret_key}"
    )
  fi
  if [[ ${method} == PUT ]]; then
    arguments+=(--data-binary "${payload}")
  fi
  if [[ -n ${resolve} ]]; then
    arguments+=(--resolve "${resolve}")
  fi
  curl "${arguments[@]}" "${url}"
}

read_volume_file() {
  docker run --rm \
    --volume "${data_volume}:/data:ro" \
    --entrypoint /bin/sh \
    "${rclone_image}" -eu -c 'cat "$1"' shell "$1"
}

volume_file_size() {
  docker run --rm \
    --volume "${data_volume}:/data:ro" \
    --entrypoint /bin/sh \
    "${rclone_image}" -eu -c 'stat -c %s "$1"' shell "$1"
}

assert_volume_absent() {
  docker run --rm \
    --volume "${data_volume}:/data:ro" \
    --entrypoint /bin/sh \
    "${rclone_image}" -eu -c 'test ! -e "$1"' shell "$1"
}

replace_claims_root_marker() {
  docker run --rm \
    --volume "${data_volume}:/data" \
    --entrypoint /bin/sh \
    "${rclone_image}" -eu -c '
      test "$(cat "$1")" = claims-root-marker
      rm -- "$1"
      mkdir -- "$1"
      chmod a+rwx "$1"
    ' shell "${claims_root}"
}

wait_for_proxy() {
  local status
  for _ in {1..100}; do
    status=$(request "${proxy_access_key}" "${proxy_secret_key}" GET \
      "http://127.0.0.1:${proxy_port}/" 2>/dev/null) || status=000
    if [[ ${status} == 200 ]]; then
      return
    fi
    sleep 0.1
  done
  printf '%s\n' 'pinned rclone proxy did not become ready' >&2
  docker logs "${proxy_container}" >&2
  exit 1
}

assert_allowed() {
  local path=$1
  local payload=$2
  local status
  status=$(request "${proxy_access_key}" "${proxy_secret_key}" PUT \
    "http://127.0.0.1:${proxy_port}${path}" "${payload}")
  if [[ ${status} != 200 ]]; then
    printf 'expected %s to be allowed, got HTTP %s\n' "${path}" "${status}" >&2
    exit 1
  fi
}

assert_denied() {
  local path=$1
  local status
  status=$(request "${proxy_access_key}" "${proxy_secret_key}" PUT \
    "http://127.0.0.1:${proxy_port}${path}" denied) || status=000
  case "${status}" in
    000)
      printf 'request to %s did not reach the proxy\n' "${path}" >&2
      exit 1
      ;;
    2*)
      printf 'expected %s to be denied, got HTTP %s\n' "${path}" "${status}" >&2
      exit 1
      ;;
    *) ;;
  esac
}

wait_for_proxy
replace_claims_root_marker

assert_allowed /repository/repository-probe repository-ok
assert_allowed /claims/claims-probe claims-ok
[[ $(read_volume_file "${repository_root}/repository-probe") == repository-ok ]]
[[ $(read_volume_file "${claims_root}/claims-probe") == claims-ok ]]

stream_payload="${test_root}/stream-payload"
readonly stream_payload
dd if=/dev/zero of="${stream_payload}" bs=1048576 count=6 2>/dev/null
assert_allowed /repository/streamed-probe "@${stream_payload}"
[[ $(volume_file_size "${repository_root}/streamed-probe") == 6291456 ]]

assert_denied /third-bucket/probe
assert_denied "/repository/../../../${sibling_owner_id}/probe"
assert_denied "/repository/../../../../${sibling_team}/repos/${owner_id}/probe"
assert_denied "/repository/%2e%2e/%2e%2e/%2e%2e/${sibling_owner_id}/probe"
assert_denied /repository//probe
[[ $(read_volume_file "${sibling_owner_root}/sentinel") == sibling-owner ]]
[[ $(read_volume_file "${sibling_team_root}/sentinel") == sibling-team ]]
assert_volume_absent "${sibling_owner_root}/probe"
assert_volume_absent "${sibling_team_root}/probe"

missing_status=$(request '' '' PUT \
  "http://127.0.0.1:${proxy_port}/repository/missing-auth" denied) || missing_status=000
wrong_status=$(request WRONGACCESSKEY0000000 "${proxy_secret_key}" PUT \
  "http://127.0.0.1:${proxy_port}/repository/wrong-auth" denied) || wrong_status=000
for status in "${missing_status}" "${wrong_status}"; do
  case "${status}" in
    000)
      printf '%s\n' 'authentication probe did not reach the proxy' >&2
      exit 1
      ;;
    2*)
      printf 'invalid authentication was accepted with HTTP %s\n' "${status}" >&2
      exit 1
      ;;
    *) ;;
  esac
done
assert_volume_absent "${repository_root}/missing-auth"
assert_volume_absent "${repository_root}/wrong-auth"

virtual_status=$(request "${proxy_access_key}" "${proxy_secret_key}" PUT \
  "http://repository.proxy.test:${proxy_port}/virtual-probe" denied \
  "repository.proxy.test:${proxy_port}:127.0.0.1") || virtual_status=000
case "${virtual_status}" in
  000)
    printf '%s\n' 'virtual-host probe did not reach the proxy' >&2
    exit 1
    ;;
  2*)
    printf 'virtual-host bucket bypass was accepted with HTTP %s\n' "${virtual_status}" >&2
    exit 1
    ;;
  *) ;;
esac
assert_volume_absent "${repository_root}/virtual-probe"

version=$(docker run --rm "${rclone_image}" version | sed -n '1s/^rclone //p')
[[ ${version} == v1.75.1 ]]
