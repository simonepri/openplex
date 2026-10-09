"""Provide macros generating Kustomize manifests, kube-linter tests, and Helm archive integrity checks."""

load("@rules_kustomize//kustomize:kustomize.bzl", _kustomization = "kustomization", _kustomized_resources = "kustomized_resources")
load("@rules_shell//shell:sh_test.bzl", "sh_test")

def helm_chart_files_test(name, chart, files):
    """Verify a packaged chart preserves nonempty chart-relative file assets.

    Args:
        name: generated test target name.
        chart: packaged Helm chart label.
        files: exact file paths relative to the source Chart.yaml parent.
    """
    sh_test(
        name = name,
        srcs = ["//src/bazel/rules/kubernetes_manifests:helm_chart_files_test.sh"],
        args = ["$(location {})".format(chart)] + files,
        data = [chart],
    )

def _kube_lint_impl(ctx):
    report = ctx.actions.declare_file(ctx.label.name + ".report")
    tool = ctx.executable._tool
    args = ctx.actions.args()
    args.add("lint")
    args.add("--config", ctx.file.config)
    args.add(ctx.file.rendered)
    ctx.actions.run_shell(
        inputs = [ctx.file.rendered, ctx.file.config],
        tools = [tool],
        outputs = [report],
        arguments = [args],
        command = '"{tool}" "$@" >"{report}" 2>&1 || {{ cat "{report}" >&2; exit 1; }}'.format(
            tool = tool.path,
            report = report.path,
        ),
        mnemonic = "KubeLint",
        progress_message = "Linting %{label} for workload shape",
    )
    return [DefaultInfo(files = depset([report])), OutputGroupInfo(_validation = depset([report]))]

# A rule rather than an aspect: the input is a rendered manifest, which is a
# build output. An aspect reads a target's declared srcs and would find none.
_kube_lint = rule(
    implementation = _kube_lint_impl,
    doc = "Fails the build when a rendered manifest violates workload shape.",
    attrs = {
        "rendered": attr.label(allow_single_file = True, mandatory = True),
        "config": attr.label(
            allow_single_file = True,
            default = "//src/bazel/rules/kubernetes_manifests:kube-linter.yaml",
        ),
        "_tool": attr.label(
            default = "//src/bazel/tools:kube-linter",
            executable = True,
            cfg = "exec",
        ),
    },
)

# Non-core CRDs have no upstream schema in the Kubernetes JSON schema set.
# Passing -skip ignores them explicitly while failing any removed or obsolete core APIs.
_NON_CORE_CRDS = [
    "AlertRule",
    "Application",
    "ApplicationSet",
    "AppProject",
    "Backend",
    "BackendTLSPolicy",
    "BackendTrafficPolicy",
    "BackupStorageLocation",
    "Bundle",
    "Certificate",
    "Cleaner",
    "CleanupPolicy",
    "ClickHouseInstallation",
    "ClickHouseInstallationTemplate",
    "ClickHouseKeeperInstallation",
    "ClickHouseOperatorConfiguration",
    "ClientTrafficPolicy",
    "Cluster",
    "ClusterCleanupPolicy",
    "ClusterComplianceReport",
    "ClusterConfig",
    "ClusterIssuer",
    "ClusterPolicy",
    "ClusterQueue",
    "ClusterSecretStore",
    "Cohort",
    "Connector",
    "CustomResourceDefinition",
    "Dashboard",
    "DeschedulerPolicy",
    "EC2NodeClass",
    "ECRAuthorizationToken",
    "EnvoyProxy",
    "ExternalSecret",
    "GCENodeClass",
    "GRPCRoute",
    "Gateway",
    "GatewayClass",
    "HealthCheck",
    "HTTPRoute",
    "HTTPRouteFilter",
    "InterceptorRoute",
    "Issuer",
    "LocalQueue",
    "NodePool",
    "ObjectStore",
    "Password",
    "Policy",
    "Profile",
    "Project",
    "ProjectConfig",
    "ProviderConfig",
    "ProxyClass",
    "ProxyGroup",
    "RayCluster",
    "RayJob",
    "RayService",
    "ReferenceGrant",
    "Report",
    "ResourceFlavor",
    "Rule",
    "SavedView",
    "ScaledObject",
    "Schedule",
    "ScheduledBackup",
    "SecretStore",
    "SecurityPolicy",
    "ServiceMonitor",
    "Stage",
    "TLSRoute",
    "Topology",
    "TriggerAuthentication",
    "ValidatingPolicy",
    "ValkeyCluster",
    "VerticalPodAutoscaler",
    "VolumeSnapshotClass",
    "VolumeSnapshotLocation",
    "Warehouse",
    "Workload",
]

def _kubeconform_impl(ctx):
    report = ctx.actions.declare_file(ctx.label.name + ".report")
    tool = ctx.executable._tool
    schemas = ctx.files.schemas

    # Every schema is flat in one version directory, so any file names it.
    location = schemas[0].dirname + "/{{ .ResourceKind }}{{ .KindSuffix }}.json"
    args = ctx.actions.args()
    args.add("-strict")
    args.add("-skip", ",".join(_NON_CORE_CRDS))

    # LINT.IfChange(kubernetes-version)
    args.add("-kubernetes-version", "1.36.0")

    # LINT.ThenChange(//MODULE.bazel:kubernetes-version)
    args.add("-schema-location", location)
    args.add("-summary")
    ctx.actions.run_shell(
        inputs = depset([ctx.file.rendered], transitive = [depset(schemas)]),
        tools = [tool],
        outputs = [report],
        arguments = [ctx.file.rendered.path, args],
        command = """
awk '
  /^---([[:space:]]|$)/ {{
    if (!is_crd && doc != "") {{
      printf "%s", doc
    }}
    doc = $0 "\\n"
    is_crd = 0
    next
  }}
  /^[[:space:]]*kind:[[:space:]]*CustomResourceDefinition([[:space:]]|$)/ {{
    is_crd = 1
  }}
  {{
    doc = doc $0 "\\n"
  }}
  END {{
    if (!is_crd && doc != "") {{
      printf "%s", doc
    }}
  }}
' "$1" | (shift && "{tool}" "$@" >"{report}" 2>&1) || (cat "{report}" >&2; exit 1)
""".format(
            tool = tool.path,
            report = report.path,
        ),
        mnemonic = "Kubeconform",
        progress_message = "Validating %{label} against the Kubernetes schema",
    )
    return [DefaultInfo(files = depset([report])), OutputGroupInfo(_validation = depset([report]))]

# A rule, not an aspect: the input is a rendered manifest, which is a build
# output an aspect cannot see.
_kubeconform = rule(
    implementation = _kubeconform_impl,
    doc = "Fails the build when a rendered manifest does not match its schema.",
    attrs = {
        "rendered": attr.label(allow_single_file = True, mandatory = True),
        "schemas": attr.label(default = "@kubernetes_json_schema//:schemas", allow_files = True),
        "_tool": attr.label(
            default = "//src/bazel/tools:kubeconform",
            executable = True,
            cfg = "exec",
        ),
    },
)

def _pdb_availability_impl(ctx):
    report = ctx.actions.declare_file(ctx.label.name + ".report")
    ctx.actions.run_shell(
        inputs = [ctx.file.rendered],
        tools = [ctx.executable._tool],
        outputs = [report],
        arguments = [ctx.file.rendered.path, report.path],
        command = '"{tool}" "$1" >"$2"'.format(tool = ctx.executable._tool.path),
        mnemonic = "PdbAvailability",
        progress_message = "Checking %{label} disruption availability",
    )
    return [DefaultInfo(files = depset([report])), OutputGroupInfo(_validation = depset([report]))]

_pdb_availability = rule(
    implementation = _pdb_availability_impl,
    attrs = {
        "rendered": attr.label(allow_single_file = True, mandatory = True),
        "_tool": attr.label(
            default = "//src/bazel/rules/kubernetes_manifests:check_pdb_availability",
            executable = True,
            cfg = "exec",
        ),
    },
)

def _network_exposure_impl(ctx):
    report = ctx.actions.declare_file(ctx.label.name + ".report")
    args = ctx.actions.args()
    args.add("rendered")
    args.add("--owner", str(ctx.attr.rendered.label))
    args.add("--manifest", ctx.file.rendered)
    ctx.actions.run_shell(
        inputs = [ctx.file.rendered],
        tools = [ctx.executable._tool],
        outputs = [report],
        arguments = [args],
        command = '"{tool}" "$@" >"{report}"'.format(
            tool = ctx.executable._tool.path,
            report = report.path,
        ),
        mnemonic = "NetworkExposure",
        progress_message = "Checking %{label} direct network exposure",
    )
    return [DefaultInfo(files = depset([report])), OutputGroupInfo(_validation = depset([report]))]

_network_exposure = rule(
    implementation = _network_exposure_impl,
    doc = "Fails the build when a rendered manifest creates unreviewed network exposure.",
    attrs = {
        "rendered": attr.label(allow_single_file = True, mandatory = True),
        "_tool": attr.label(
            default = "//src/bazel/checks/network_exposure:network_exposure",
            executable = True,
            cfg = "exec",
        ),
    },
)

def _secret_delivery_impl(ctx):
    report = ctx.actions.declare_file(ctx.label.name + ".report")
    args = ctx.actions.args()
    args.add("--rendered", ctx.file.rendered)
    ctx.actions.run_shell(
        inputs = [ctx.file.rendered],
        tools = [ctx.executable._tool],
        outputs = [report],
        arguments = [args],
        command = '"{tool}" "$@" >"{report}" 2>&1 || (cat "{report}" >&2; exit 1)'.format(
            tool = ctx.executable._tool.path,
            report = report.path,
        ),
        mnemonic = "SecretDelivery",
        progress_message = "Checking %{label} secret delivery mechanism",
    )
    return [DefaultInfo(files = depset([report])), OutputGroupInfo(_validation = depset([report]))]

_secret_delivery = rule(
    implementation = _secret_delivery_impl,
    doc = "Fails the build when a rendered manifest delivers secrets via environment variables without an annotation.",
    attrs = {
        "rendered": attr.label(allow_single_file = True, mandatory = True),
        "_tool": attr.label(
            default = "//src/bazel/checks/secret_delivery:secret_delivery",
            executable = True,
            cfg = "exec",
        ),
    },
)

def pdb_availability_check(name, rendered):
    """Reject one rendered manifest whose PDB blocks every replica."""
    _pdb_availability(name = name, rendered = rendered)

def manifest_checks(name, rendered, kube_lint_config = None):
    """Schema, workload-shape, and exposure checks for one rendered manifest.

    Args:
        name: base name for the generated targets.
        rendered: label of a rendered Kubernetes manifest file.
        kube_lint_config: optional label of kube-linter configuration file.
    """

    _kubeconform(
        name = name + ".kubeconform",
        rendered = rendered,
    )
    _kube_lint(
        name = name + ".kube-linter",
        rendered = rendered,
        config = kube_lint_config,
    )
    _network_exposure(
        name = name + ".network-exposure",
        rendered = rendered,
    )
    _pdb_availability(
        name = name + ".pdb-availability",
        rendered = rendered,
    )
    _secret_delivery(
        name = name + ".secret-delivery",
        rendered = rendered,
    )

def workload_manifests(
        name,
        srcs,
        kustomization = "deployment/kustomization.yaml",
        visibility = ["//visibility:public"]):
    """Render and validate a workload deployment bundle with explicit sources.

    Args:
        name: base name for the rendered manifest target (e.g. "manifest").
        srcs: explicit list of all manifest and configuration sources.
        kustomization: path to the root kustomization.yaml.
        visibility: visibility of the rendered manifest.
    """
    if not srcs:
        fail("workload_manifests requires explicit srcs")

    kustomize_target = name + ".kustomization"
    _kustomization(
        name = kustomize_target,
        file = kustomization,
        srcs = srcs,
    )
    _kustomized_resources(
        name = name,
        kustomization = ":" + kustomize_target,
        visibility = visibility,
    )
    manifest_checks(
        name = name,
        rendered = ":" + name,
    )
