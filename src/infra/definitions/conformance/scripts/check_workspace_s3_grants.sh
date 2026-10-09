#!/bin/sh
# Asserts admission rules govern workspace S3 credentials, grant integrity, and volume bindings.

# shellcheck disable=SC2312
set -eu

namespace="workspace-s3-grants-e2e"
argo_identity="system:serviceaccount:kube-system:argocd-manager"
provisioner="system:serviceaccount:kube-system:coder-provisioner"
provisioner_group="cluster:coder-provisioners"

member_uid="11111111-1111-4111-8111-111111111111"
non_member_uid="33333333-3333-4333-8333-333333333333"
workspace_id="22222222-2222-4222-8222-222222222222"
other_workspace_id="44444444-4444-4444-8444-444444444444"

temp_dir="$(mktemp -d)"

cleanup() {
  status=$?
  set +e
  kubectl delete namespace "${namespace}" --ignore-not-found=true --wait=false >/dev/null 2>&1
  rm -rf -- "${temp_dir}"
  exit "${status}"
}

trap cleanup EXIT HUP INT TERM

expect_allowed() {
  description="$1"
  shift
  set +e
  output="$("$@" 2>&1)"
  status=$?
  set -e
  if [ "${status}" -ne 0 ]; then
    printf '%s unexpectedly failed:\n%s\n' "${description}" "${output}" >&2
    return 1
  fi
}

expect_denied() {
  description="$1"
  expected="$2"
  shift 2
  set +e
  output="$("$@" 2>&1)"
  status=$?
  set -e
  if [ "${status}" -eq 0 ]; then
    printf '%s unexpectedly succeeded\n' "${description}" >&2
    return 1
  fi
  if ! printf '%s\n' "${output}" | tr -s '[:space:]' ' ' | grep -F "${expected}" >/dev/null; then
    printf '%s failed outside the expected policy:\n%s\n' "${description}" "${output}" >&2
    return 1
  fi
}

# 1. Create an isolated namespace labelled app.kubernetes.io/part-of=coder-workspaces
kubectl create namespace "${namespace}" --dry-run=client -o yaml \
  | kubectl apply --filename=- >/dev/null
kubectl label namespace "${namespace}" \
  app.kubernetes.io/part-of=coder-workspaces \
  cell=cell-aws-usw2 --overwrite >/dev/null

# 2. Create two grant ConfigMaps as the Argo identity
legacy_grant="${temp_dir}/legacy-grant.yaml"
team_grant="${temp_dir}/team-grant.yaml"

cat <<EOF >"${legacy_grant}"
apiVersion: v1
kind: ConfigMap
metadata:
  name: workspace-s3-grant-legacy
  namespace: ${namespace}
  labels:
    app.kubernetes.io/component: workspace-s3-grant
    app.kubernetes.io/part-of: coder-workspaces
    app.kubernetes.io/managed-by: argocd
data:
  grant: legacy
  recordName: cell-aws-usw2-s3-legacy-research-data
EOF

cat <<EOF >"${team_grant}"
apiVersion: v1
kind: ConfigMap
metadata:
  name: workspace-s3-grant-team-examples
  namespace: ${namespace}
  labels:
    app.kubernetes.io/component: workspace-s3-grant
    app.kubernetes.io/part-of: coder-workspaces
    app.kubernetes.io/managed-by: argocd
data:
  grant: team
  team: examples
  recordName: cell-aws-usw2-s3-team-examples
  members: "${member_uid},aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
  globalStorage: "true"
EOF

read_grant="${temp_dir}/read-grant.yaml"
cat <<EOF >"${read_grant}"
apiVersion: v1
kind: ConfigMap
metadata:
  name: workspace-s3-read-grant-team-open
  namespace: ${namespace}
  labels:
    app.kubernetes.io/component: workspace-s3-grant
    app.kubernetes.io/part-of: coder-workspaces
    app.kubernetes.io/managed-by: argocd
data:
  grant: team-reader
  team: open
  recordName: cell-aws-usw2-s3-team-open-reader
  readers: "all"
  allOwners: "true"
  globalStorage: "true"
EOF

restricted_read_grant="${temp_dir}/restricted-read-grant.yaml"
cat <<EOF >"${restricted_read_grant}"
apiVersion: v1
kind: ConfigMap
metadata:
  name: workspace-s3-read-grant-team-restricted
  namespace: ${namespace}
  labels:
    app.kubernetes.io/component: workspace-s3-grant
    app.kubernetes.io/part-of: coder-workspaces
    app.kubernetes.io/managed-by: argocd
data:
  grant: team-reader
  team: restricted
  recordName: cell-aws-usw2-s3-team-restricted-reader
  readers: "members"
  globalStorage: "true"
EOF

expect_allowed 'argocd-manager creates legacy grant' \
  kubectl apply --filename="${legacy_grant}" --as="${argo_identity}"

expect_allowed 'argocd-manager creates team grant' \
  kubectl apply --filename="${team_grant}" --as="${argo_identity}"

expect_allowed 'argocd-manager creates read grant' \
  kubectl apply --filename="${read_grant}" --as="${argo_identity}"

expect_allowed 'argocd-manager creates restricted read grant' \
  kubectl apply --filename="${restricted_read_grant}" --as="${argo_identity}"

# 3. Test Cases:

# Case 1: Member allowed
member_es="${temp_dir}/member-externalsecret.yaml"
cat <<EOF >"${member_es}"
apiVersion: external-secrets.io/v1
kind: ExternalSecret
metadata:
  name: coder-${workspace_id}-s3
  namespace: ${namespace}
  labels:
    app.kubernetes.io/component: workspace-s3-credentials
    com.coder.user.id: "${member_uid}"
    com.coder.workspace.id: "${workspace_id}"
spec:
  refreshInterval: 1h
  secretStoreRef:
    kind: ClusterSecretStore
    name: workspace-s3-workspaces
  target:
    name: coder-${workspace_id}-s3
    creationPolicy: Owner
    deletionPolicy: Retain
    template:
      engineVersion: v2
      data:
        credentials: "profile"
        configData: "rclone"
  data:
    - secretKey: legacy_access_key_id
      remoteRef:
        key: cell-aws-usw2-s3-legacy-research-data
        property: access_key_id
    - secretKey: legacy_secret_access_key
      remoteRef:
        key: cell-aws-usw2-s3-legacy-research-data
        property: secret_access_key
    - secretKey: team_examples_access_key_id
      remoteRef:
        key: cell-aws-usw2-s3-team-examples
        property: access_key_id
    - secretKey: team_examples_secret_access_key
      remoteRef:
        key: cell-aws-usw2-s3-team-examples
        property: secret_access_key
EOF

expect_allowed 'member ExternalSecret creation by provisioner' \
  kubectl create --dry-run=server --output=name \
  --filename="${member_es}" \
  --as="${provisioner}" --as-group="${provisioner_group}"

# Case 2: Non-member denied
non_member_es="${temp_dir}/non-member-externalsecret.yaml"
sed "s/${member_uid}/${non_member_uid}/g" "${member_es}" >"${non_member_es}"

expect_denied 'non-member ExternalSecret creation' \
  "Team secret keys must reference the recordName of the matching team grant and require the user to be a member." \
  kubectl create --dry-run=server --output=name \
  --filename="${non_member_es}" \
  --as="${provisioner}" --as-group="${provisioner_group}"

# Case 2a: Reader allowed with all-owners marker
reader_es="${temp_dir}/reader-externalsecret.yaml"
cat <<EOF >"${reader_es}"
apiVersion: external-secrets.io/v1
kind: ExternalSecret
metadata:
  name: coder-${workspace_id}-s3
  namespace: ${namespace}
  labels:
    app.kubernetes.io/component: workspace-s3-credentials
    com.coder.user.id: "${non_member_uid}"
    com.coder.workspace.id: "${workspace_id}"
spec:
  refreshInterval: 1h
  secretStoreRef:
    kind: ClusterSecretStore
    name: workspace-s3-workspaces
  target:
    name: coder-${workspace_id}-s3
    creationPolicy: Owner
    deletionPolicy: Retain
    template:
      engineVersion: v2
      data:
        credentials: "profile"
        configData: "rclone"
  data:
    - secretKey: legacy_access_key_id
      remoteRef:
        key: cell-aws-usw2-s3-legacy-research-data
        property: access_key_id
    - secretKey: legacy_secret_access_key
      remoteRef:
        key: cell-aws-usw2-s3-legacy-research-data
        property: secret_access_key
    - secretKey: team_open_access_key_id
      remoteRef:
        key: cell-aws-usw2-s3-team-open-reader
        property: access_key_id
    - secretKey: team_open_secret_access_key
      remoteRef:
        key: cell-aws-usw2-s3-team-open-reader
        property: secret_access_key
EOF

expect_allowed 'reader ExternalSecret creation by provisioner' \
  kubectl create --dry-run=server --output=name \
  --filename="${reader_es}" \
  --as="${provisioner}" --as-group="${provisioner_group}"

# Case 2b: Reader denied when no read grant exists
no_grant_reader_es="${temp_dir}/no-grant-reader-externalsecret.yaml"
sed "s/team-open-reader/team-closed-reader/g; s/team_open_/team_closed_/g" "${reader_es}" >"${no_grant_reader_es}"

expect_denied 'reader ExternalSecret without grant denied' \
  "Team reader secret keys may only reference reader records when a read grant with the all-owners marker exists for that team." \
  kubectl create --dry-run=server --output=name \
  --filename="${no_grant_reader_es}" \
  --as="${provisioner}" --as-group="${provisioner_group}"

# Case 2c: Reader denied when read grant lacks all-owners marker
restricted_reader_es="${temp_dir}/restricted-reader-externalsecret.yaml"
sed "s/team-open-reader/team-restricted-reader/g; s/team_open_/team_restricted_/g" "${reader_es}" >"${restricted_reader_es}"

expect_denied 'reader ExternalSecret without all-owners marker denied' \
  "Team reader secret keys may only reference reader records when a read grant with the all-owners marker exists for that team." \
  kubectl create --dry-run=server --output=name \
  --filename="${restricted_reader_es}" \
  --as="${provisioner}" --as-group="${provisioner_group}"

# Case 3: Legacy-only allowed (even for non-member)
legacy_only_es="${temp_dir}/legacy-only-externalsecret.yaml"
cat <<EOF >"${legacy_only_es}"
apiVersion: external-secrets.io/v1
kind: ExternalSecret
metadata:
  name: coder-${workspace_id}-s3
  namespace: ${namespace}
  labels:
    app.kubernetes.io/component: workspace-s3-credentials
    com.coder.user.id: "${non_member_uid}"
    com.coder.workspace.id: "${workspace_id}"
spec:
  refreshInterval: 1h
  secretStoreRef:
    kind: ClusterSecretStore
    name: workspace-s3-workspaces
  target:
    name: coder-${workspace_id}-s3
    creationPolicy: Owner
    deletionPolicy: Retain
    template:
      engineVersion: v2
      data:
        credentials: "profile"
        configData: "rclone"
  data:
    - secretKey: legacy_access_key_id
      remoteRef:
        key: cell-aws-usw2-s3-legacy-research-data
        property: access_key_id
    - secretKey: legacy_secret_access_key
      remoteRef:
        key: cell-aws-usw2-s3-legacy-research-data
        property: secret_access_key
EOF

expect_allowed 'legacy-only ExternalSecret creation by provisioner' \
  kubectl create --dry-run=server --output=name \
  --filename="${legacy_only_es}" \
  --as="${provisioner}" --as-group="${provisioner_group}"

# Case 4: global_* property denied
global_prop_es="${temp_dir}/global-property-externalsecret.yaml"
cat <<EOF >"${global_prop_es}"
apiVersion: external-secrets.io/v1
kind: ExternalSecret
metadata:
  name: coder-${workspace_id}-s3
  namespace: ${namespace}
  labels:
    app.kubernetes.io/component: workspace-s3-credentials
    com.coder.user.id: "${member_uid}"
    com.coder.workspace.id: "${workspace_id}"
spec:
  refreshInterval: 1h
  secretStoreRef:
    kind: ClusterSecretStore
    name: workspace-s3-workspaces
  target:
    name: coder-${workspace_id}-s3
    creationPolicy: Owner
    deletionPolicy: Retain
    template:
      engineVersion: v2
      data:
        credentials: "profile"
        configData: "rclone"
  data:
    - secretKey: team_examples_global_access_key_id
      remoteRef:
        key: cell-aws-usw2-s3-team-examples
        property: global_access_key_id
EOF

expect_denied 'global property ExternalSecret creation' \
  "Workspace S3 ExternalSecret data keys must match ^(legacy|team_[a-z][a-z0-9]{1,30})_(access_key_id|secret_access_key)$ with matching remoteRef property." \
  kubectl create --dry-run=server --output=name \
  --filename="${global_prop_es}" \
  --as="${provisioner}" --as-group="${provisioner_group}"

# Case 5: Name/label mismatch denied
mismatch_es="${temp_dir}/mismatch-externalsecret.yaml"
sed "s/com.coder.workspace.id: \"${workspace_id}\"/com.coder.workspace.id: \"${other_workspace_id}\"/g" \
  "${member_es}" >"${mismatch_es}"

expect_denied 'name/label mismatch ExternalSecret creation' \
  "Workspace S3 ExternalSecret name must match coder-<com.coder.workspace.id>-s3 and declare required workspace, user, and component labels." \
  kubectl create --dry-run=server --output=name \
  --filename="${mismatch_es}" \
  --as="${provisioner}" --as-group="${provisioner_group}"

# Case 6: dataFrom denied
datafrom_es="${temp_dir}/datafrom-externalsecret.yaml"
cat <<EOF >"${datafrom_es}"
apiVersion: external-secrets.io/v1
kind: ExternalSecret
metadata:
  name: coder-${workspace_id}-s3
  namespace: ${namespace}
  labels:
    app.kubernetes.io/component: workspace-s3-credentials
    com.coder.user.id: "${member_uid}"
    com.coder.workspace.id: "${workspace_id}"
spec:
  refreshInterval: 1h
  secretStoreRef:
    kind: ClusterSecretStore
    name: workspace-s3-workspaces
  target:
    name: coder-${workspace_id}-s3
    creationPolicy: Owner
    deletionPolicy: Retain
    template:
      engineVersion: v2
      data:
        credentials: "profile"
        configData: "rclone"
  dataFrom:
    - extract:
        key: cell-aws-usw2-s3-legacy-research-data
EOF

expect_denied 'dataFrom ExternalSecret creation' \
  "Workspace S3 ExternalSecret must not specify dataFrom." \
  kubectl create --dry-run=server --output=name \
  --filename="${datafrom_es}" \
  --as="${provisioner}" --as-group="${provisioner_group}"

# Case 7: Provisioner editing a grant denied
forged_grant="${temp_dir}/forged-grant.yaml"
cat <<EOF >"${forged_grant}"
apiVersion: v1
kind: ConfigMap
metadata:
  name: workspace-s3-grant-team-forged
  namespace: ${namespace}
  labels:
    app.kubernetes.io/component: workspace-s3-grant
    app.kubernetes.io/part-of: coder-workspaces
    app.kubernetes.io/managed-by: argocd
data:
  grant: team
  team: forged
  recordName: cell-aws-usw2-s3-team-forged
  members: "${member_uid}"
  globalStorage: "false"
EOF

expect_denied 'provisioner editing a grant ConfigMap' \
  "Workspace S3 grant ConfigMaps may only be created, modified, or deleted by Argo CD or namespace-controller." \
  kubectl create --dry-run=server --output=name \
  --filename="${forged_grant}" \
  --as="${provisioner}" --as-group="${provisioner_group}"

# Case 8: Argo CD manager allowed (verified in step 2 above)

# Case 9: Pod mounting another workspace's secret denied
foreign_pod="${temp_dir}/foreign-pod.yaml"
cat <<EOF >"${foreign_pod}"
apiVersion: v1
kind: Pod
metadata:
  name: coder-${workspace_id}-probe
  namespace: ${namespace}
  labels:
    com.coder.workspace.id: "${workspace_id}"
spec:
  restartPolicy: Never
  containers:
    - name: app
      image: alpine:latest
      command: ["sh", "-c", "true"]
  volumes:
    - name: foreign-s3
      secret:
        secretName: coder-${other_workspace_id}-s3
EOF

expect_denied 'pod mounting another workspace secret' \
  "Workspace S3 secrets may only be mounted by the matching Coder workspace Pod or Deployment." \
  kubectl create --dry-run=server --output=name \
  --filename="${foreign_pod}" \
  --as="${provisioner}" --as-group="${provisioner_group}"

trap - EXIT HUP INT TERM
cleanup
