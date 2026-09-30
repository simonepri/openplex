#!/usr/bin/env python3
"""Validate Kubernetes service manifests to forbid unauthorized public NodePort and LoadBalancer exposure."""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import sys
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import yaml

IPV4_VERSION = 4
GATEWAY_IP_OFFSET = 11
DNS_IP_OFFSET = 10

EXTERNAL_DNS_HOSTNAME = "external-dns.alpha.kubernetes.io/hostname"
EXTERNAL_DNS_HOSTNAME_PATCH = "external-dns.alpha.kubernetes.io~1hostname"
EXTERNAL_DNS_SOURCE_LABEL = "app.kubernetes.io/component"
EXTERNAL_DNS_SOURCE_VALUE = "external-dns-source"
SOURCE_SUFFIXES = frozenset({".tpl", ".yaml", ".yml"})
IGNORED_PATH_SEGMENTS = frozenset({
    # keep-sorted start
    ".terraform",
    ".terragrunt-cache",
    ".tmp",
    "node_modules",
    # keep-sorted end
})

INGRESS = "ingress"
LOAD_BALANCER = "load-balancer"
NODE_PORT = "node-port"
EXTERNAL_IPS = "external-ips"
EXTERNAL_DNS_HOSTNAME_SURFACE = "external-dns-hostname"
EXTERNAL_DNS_SOURCE_SURFACE = "external-dns-source"
PUBLIC_LOAD_BALANCER_DEFAULT = "public-load-balancer-default"
LOCAL_HEADSCALE_POLICY = Path("src/infra/tools/cloud_emulator/stack/headscale/policy.hujson")
CLOUD_HEADSCALE_POLICY = Path("src/infra/argocd/components/tailscale_access/cloud-policy.hujson")
MANAGED_API_TRANSPORT_GLOBS = (
    "src/infra/terraform/components/_modules/argocd_registration_payload/*",
    "src/infra/terraform/components/argo_bootstrap/*",
    "src/infra/terraform/components/argocd_registration_record/*",
    "src/infra/terraform/components/coder_*_provisioner_identity/*",
    "src/infra/terraform/components/tailscale_vpc_router_*/*",
    "src/infra/terraform/components/tailscale_vpc_router_*/*/*",
    "src/infra/terraform/live/cell_*/*/*",
    "src/infra/terraform/live/ctrl_*/*/*",
)
MANAGED_API_ROUTE_TOKENS = (
    # keep-sorted start
    "--advertise-routes",
    "--snat-subnet-routes",
    "MASQUERADE",
    "advertisedRoutes",
    "advertised_routes",
    "net.ipv4.ip_forward",
    "net.ipv6.conf.all.forwarding",
    # keep-sorted end
)


@dataclass(frozen=True, order=True)
class ResourceIdentity:
    """Stable identity for one rendered or source Kubernetes resource."""

    api_version: str
    kind: str
    namespace: str
    name: str


@dataclass(frozen=True, order=True)
class SourceSurface:
    """One exposure-sensitive construct in a source manifest."""

    path: str
    surface: str
    resource: ResourceIdentity


@dataclass(frozen=True, order=True)
class RenderedSurface:
    """One exposure-sensitive construct in a rendered manifest."""

    owner: str
    surface: str
    resource: ResourceIdentity


EMPTY_RESOURCE = ResourceIdentity("", "", "", "")

# The source path and resource identity must both match. Counts prevent a
# reviewed file from becoming a bucket for additional direct exposure.
APPROVED_SOURCE_SURFACES = Counter({
    SourceSurface(
        "src/infra/argocd/apps/ctrl.yaml",
        LOAD_BALANCER,
        ResourceIdentity(
            "argoproj.io/v1alpha1",
            "ApplicationSet",
            "argocd",
            "ctrl-apps",
        ),
    ): 1,
    SourceSurface(
        "src/infra/argocd/components/envoy_gateway_instance/helm/values.yaml",
        LOAD_BALANCER,
        EMPTY_RESOURCE,
    ): 1,
    SourceSurface(
        "src/infra/argocd/components/routing_registry/helm/templates/_helpers.tpl",
        EXTERNAL_DNS_SOURCE_SURFACE,
        EMPTY_RESOURCE,
    ): 1,
    SourceSurface(
        "src/infra/argocd/components/s3_gateway/helm/templates/routes.yaml",
        EXTERNAL_DNS_HOSTNAME_SURFACE,
        ResourceIdentity(
            "gateway.networking.k8s.io/v1",
            "HTTPRoute",
            "s3-system",
            "s3-gateway-cross-region",
        ),
    ): 1,
    SourceSurface(
        "src/infra/argocd/components/s3_gateway/helm/templates/routes.yaml",
        EXTERNAL_DNS_SOURCE_SURFACE,
        ResourceIdentity(
            "gateway.networking.k8s.io/v1",
            "HTTPRoute",
            "s3-system",
            "s3-gateway-cross-region",
        ),
    ): 1,
})


def rendered_surface(
    owner: str,
    surface: str,
    resource: ResourceIdentity,
) -> RenderedSurface:
    """Build one readable rendered allowlist entry."""

    return RenderedSurface(owner, surface, resource)


# Render ownership prevents an otherwise-valid identity from being copied into
# another component. Each surface is listed separately so an approved private
# LoadBalancer cannot silently acquire public DNS.
APPROVED_RENDERED_SURFACES = frozenset({
    rendered_surface(
        "//src/infra/argocd/components/routing_registry:helm_render-public",
        EXTERNAL_DNS_SOURCE_SURFACE,
        ResourceIdentity(
            "gateway.networking.k8s.io/v1", "HTTPRoute", "envoy-gateway-system", "atlantis-webhook"
        ),
    ),
    rendered_surface(
        "//src/infra/argocd/components/routing_registry:helm_render-public",
        EXTERNAL_DNS_SOURCE_SURFACE,
        ResourceIdentity(
            "gateway.networking.k8s.io/v1beta1",
            "ReferenceGrant",
            "atlantis",
            "routing-registry-atlantis-webhook",
        ),
    ),
    rendered_surface(
        "//src/infra/argocd/components/routing_registry:helm_render-public",
        EXTERNAL_DNS_SOURCE_SURFACE,
        ResourceIdentity(
            "gateway.envoyproxy.io/v1alpha1",
            "SecurityPolicy",
            "envoy-gateway-system",
            "atlantis-webhook-allowlist",
        ),
    ),
    rendered_surface(
        "//src/infra/argocd/components/routing_registry:helm_render-public",
        EXTERNAL_DNS_SOURCE_SURFACE,
        ResourceIdentity(
            "gateway.networking.k8s.io/v1beta1",
            "ReferenceGrant",
            "atlantis",
            "routing-registry-atlantis-webhook-atlantis",
        ),
    ),
    rendered_surface(
        "//src/infra/argocd/components/routing_registry:helm_render-public",
        EXTERNAL_DNS_SOURCE_SURFACE,
        ResourceIdentity(
            "gateway.networking.k8s.io/v1beta1",
            "ReferenceGrant",
            "argocd",
            "routing-registry-atlantis-webhook-argocd-server",
        ),
    ),
    rendered_surface(
        "//src/infra/argocd/components/s3_gateway:helm_render",
        EXTERNAL_DNS_HOSTNAME_SURFACE,
        ResourceIdentity(
            "gateway.networking.k8s.io/v1",
            "HTTPRoute",
            "s3-system",
            "s3-gateway-cross-region",
        ),
    ),
    rendered_surface(
        "//src/infra/argocd/components/s3_gateway:helm_render",
        EXTERNAL_DNS_SOURCE_SURFACE,
        ResourceIdentity(
            "gateway.networking.k8s.io/v1",
            "HTTPRoute",
            "s3-system",
            "s3-gateway-cross-region",
        ),
    ),
    *(
        rendered_surface(
            owner,
            LOAD_BALANCER,
            ResourceIdentity(
                "gateway.envoyproxy.io/v1alpha1", "EnvoyProxy", "envoy-gateway-system", name
            ),
        )
        for name in ("public", "public-hooks")
        for owner in (
            # keep-sorted start
            "//src/infra/argocd/components/envoy_gateway_instance:helm_render",
            "//src/infra/argocd/components/envoy_gateway_instance:helm_render-cell",
            "//src/infra/argocd/components/envoy_gateway_instance:helm_render-cloud",
            "//src/infra/argocd/components/envoy_gateway_instance:helm_render-local",
            # keep-sorted end
        )
    ),
})


def source_errors(root: Path) -> list[str]:
    """Return direct-exposure violations in authored Argo CD sources."""

    observed: Counter[SourceSurface] = Counter()
    for path in source_paths(root):
        relative_path = path.relative_to(root).as_posix()
        observed.update(source_surfaces(relative_path, path.read_text(encoding="utf-8")))

    errors: list[str] = []
    for item, count in sorted(observed.items()):
        approved = APPROVED_SOURCE_SURFACES[item]
        if count <= approved:
            continue
        errors.append(
            f"{item.path}: {resource_name(item.resource)} uses {item.surface} "
            f"outside the reviewed exposure allowlist"
        )
    errors.extend(local_headscale_policy_errors(root))
    errors.extend(managed_api_service_errors(root))
    return errors


def _validate_local_member_grants(
    policy_or_grants: Mapping[str, object] | Sequence[object] | Path,
    root: Path | None = None,
    *,
    dns_destinations: Sequence[str] | None = None,
    gateway_destinations: Sequence[str] | None = None,
    cluster_cidrs: Sequence[str] | None = None,
) -> list[str]:
    """Approve member grants for private gateway, DNS, and cluster SSH subnet access."""

    if isinstance(policy_or_grants, Path):
        root = policy_or_grants
        policy_path = root / LOCAL_HEADSCALE_POLICY
        if not policy_path.is_file():
            return []
        try:
            policy = parse_hujson(policy_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, TypeError, ValueError, yaml.YAMLError) as error:
            return [
                f"{LOCAL_HEADSCALE_POLICY.as_posix()}: cannot validate local member grants: {error}"
            ]
        grants = sequence(policy.get("grants"))
    elif isinstance(policy_or_grants, Mapping):
        grants = sequence(policy_or_grants.get("grants"))
    else:
        grants = sequence(policy_or_grants)

    if dns_destinations is None and root is not None:
        dns_destinations = local_private_dns_destinations(root)
    if gateway_destinations is None and root is not None:
        gateway_destinations = local_private_gateway_destinations(root)
    if cluster_cidrs is None and root is not None:
        cluster_cidrs = local_cluster_cidrs(root)

    dns_destinations = dns_destinations or ()
    gateway_destinations = gateway_destinations or ()
    cluster_cidrs = cluster_cidrs or ()

    approved = {
        (
            frozenset({"autogroup:member"}),
            frozenset({"autogroup:self"}),
            frozenset({"tcp:2222", "tcp:6767"}),
        ),
        # LINT.IfChange(local-cluster-ssh-member-access)
        (
            frozenset({"autogroup:member"}),
            frozenset(cluster_cidrs),
            frozenset({"tcp:2222"}),
        ),
        # LINT.ThenChange(//src/infra/tools/cloud_emulator/stack/headscale/policy.hujson:local-cluster-ssh-member-access)
        # LINT.IfChange(local-private-gateway-member-access)
        (
            frozenset({"autogroup:member", "tag:workspace"}),
            frozenset(gateway_destinations),
            frozenset({"tcp:443"}),
        ),
        # LINT.ThenChange(//src/infra/tools/cloud_emulator/stack/headscale/policy.hujson:local-private-gateway-member-access)
        # LINT.IfChange(local-private-dns-member-access)
        (
            frozenset({"autogroup:member", "tag:workspace"}),
            frozenset(dns_destinations),
            frozenset({"tcp:53", "udp:53"}),
        ),
        # LINT.ThenChange(//src/infra/tools/cloud_emulator/stack/headscale/policy.hujson:local-private-dns-member-access)
        (
            frozenset({"autogroup:member", "tag:workspace"}),
            frozenset({"tag:subnet-router"}),
            frozenset({"tcp:8444"}),
        ),
    }
    grants_seq = sequence(grants)
    observed = {
        normalized_headscale_grant(grant)
        for grant in grants_seq
        if isinstance(grant, Mapping) and "autogroup:member" in sequence(grant.get("src"))
    }
    errors = []
    for grant in sorted(observed - approved, key=repr):
        errors.append(
            f"{LOCAL_HEADSCALE_POLICY.as_posix()}: autogroup:member has an unreviewed "
            f"grant to {sorted(grant[1])} on {sorted(grant[2])}"
        )
    for grant in sorted(approved - observed, key=repr):
        errors.append(
            f"{LOCAL_HEADSCALE_POLICY.as_posix()}: required autogroup:member grant "
            f"to {sorted(grant[1])} on {sorted(grant[2])} is missing"
        )
    return errors


def local_headscale_policy_errors(root: Path) -> list[str]:
    """Keep local tailnet members behind the authenticated private gateways."""

    policy_path = root / LOCAL_HEADSCALE_POLICY
    if not policy_path.is_file():
        return []

    try:
        policy = parse_hujson(policy_path.read_text(encoding="utf-8"))
        dns_destinations = local_private_dns_destinations(root)
        gateway_destinations = local_private_gateway_destinations(root)
        cluster_cidrs = local_cluster_cidrs(root)
    except (json.JSONDecodeError, TypeError, ValueError, yaml.YAMLError) as error:
        return [
            f"{LOCAL_HEADSCALE_POLICY.as_posix()}: cannot validate local member grants: {error}"
        ]

    return _validate_local_member_grants(
        policy,
        dns_destinations=dns_destinations,
        gateway_destinations=gateway_destinations,
        cluster_cidrs=cluster_cidrs,
    )


def parse_hujson(text: str) -> Mapping[str, object]:
    """Parse the comment and trailing-comma subset used by the Headscale policy."""

    without_comments = re.sub(r"(?m)//.*$", "", text)
    normalized = re.sub(r",(?=\s*[}\]])", "", without_comments)
    document = json.loads(normalized)
    if not isinstance(document, Mapping):
        raise TypeError("policy must be an object")
    return {str(k): v for k, v in document.items()}


def _cell_yaml_cidrs(cell_paths: list[Path]) -> list[tuple[str, str]]:
    results = []
    for path in cell_paths:
        record = yaml.safe_load(path.read_text(encoding="utf-8"))
        if isinstance(record, Mapping) and record.get("provider") == "floci":
            network = mapping(record.get("network"))
            results.append((str(path), str(network.get("service_cidr", ""))))
    return results


def _cluster_yaml_cidrs(root: Path) -> list[tuple[str, str]]:
    deployment_path = root / "src/infra/terraform/deployments/local/deployment.yaml"
    if deployment_path.is_file():
        doc = yaml.safe_load(deployment_path.read_text(encoding="utf-8"))
        if isinstance(doc, Mapping):
            clusters = doc.get("clusters", {})
            if isinstance(clusters, Mapping):
                results = []
                for name, record in sorted(clusters.items()):
                    if isinstance(record, Mapping) and record.get("provider") == "floci":
                        network = mapping(record.get("network"))
                        results.append((str(name), str(network.get("service_cidr", ""))))
                if results:
                    return results

    clusters_path = root / "src/infra/terraform/deployments/local/deployment.yaml"
    if not clusters_path.is_file():
        return []
    clusters_doc = yaml.safe_load(clusters_path.read_text(encoding="utf-8"))
    if not isinstance(clusters_doc, Mapping):
        return []
    clusters = clusters_doc.get("clusters", {})
    if not isinstance(clusters, Mapping):
        return []
    results = []
    for name, record in sorted(clusters.items()):
        if isinstance(record, Mapping) and record.get("provider") == "floci":
            network = mapping(record.get("network"))
            results.append((str(name), str(network.get("service_cidr", ""))))
    return results


def _iter_floci_service_cidrs(root: Path) -> list[tuple[str, str]]:
    cells_dir = root / "src/infra/cells"
    cell_paths = sorted(cells_dir.glob("*.yaml")) if cells_dir.is_dir() else []
    if cell_paths:
        return _cell_yaml_cidrs(cell_paths)
    return _cluster_yaml_cidrs(root)


def _derive_destinations_with_offset(root: Path, offset: int) -> tuple[str, ...]:
    destinations = []
    for source_label, service_cidr in _iter_floci_service_cidrs(root):
        service_network = ipaddress.ip_network(service_cidr)
        if service_network.version != IPV4_VERSION:
            raise ValueError(f"{source_label}: local service_cidr must be IPv4")
        destinations.append(f"{service_network.network_address + offset}/32")
    if not destinations:
        raise ValueError("no local cluster service CIDRs found")
    return tuple(sorted(destinations))


def local_private_gateway_destinations(root: Path) -> tuple[str, ...]:
    """Derive exact local private Gateway addresses from canonical cell records."""

    return _derive_destinations_with_offset(root, GATEWAY_IP_OFFSET)


def local_private_dns_destinations(root: Path) -> tuple[str, ...]:
    """Derive exact local CoreDNS addresses from canonical cell records."""

    return _derive_destinations_with_offset(root, DNS_IP_OFFSET)


def local_cluster_cidrs(root: Path) -> tuple[str, ...]:
    """Derive exact local cluster service CIDRs from canonical cell records."""

    cidrs = []
    for source_label, service_cidr in _iter_floci_service_cidrs(root):
        service_network = ipaddress.ip_network(service_cidr)
        if service_network.version != IPV4_VERSION:
            raise ValueError(f"{source_label}: local service_cidr must be IPv4")
        cidrs.append(str(service_network))
    if not cidrs:
        raise ValueError("no local cluster service CIDRs found")
    return tuple(sorted(cidrs))


def normalized_headscale_grant(
    grant: Mapping[object, object],
) -> tuple[frozenset[str], frozenset[str], frozenset[str]]:
    """Return one Headscale grant as an order-independent contract."""

    return (
        frozenset(str(value) for value in sequence(grant.get("src"))),
        frozenset(str(value) for value in sequence(grant.get("dst"))),
        frozenset(str(value) for value in sequence(grant.get("ip"))),
    )


def managed_api_transport_errors(root: Path) -> list[str]:
    """Reject subnet-routing behavior in managed Kubernetes API transports."""

    errors = []
    transport_paths = {
        path
        for pattern in MANAGED_API_TRANSPORT_GLOBS
        for path in root.glob(pattern)
        if path.is_file() and not path.name.endswith(".tftest.hcl")
    }
    for path in sorted(transport_paths):
        text = path.read_text(encoding="utf-8")
        for token in MANAGED_API_ROUTE_TOKENS:
            if token in text:
                errors.append(
                    f"{path.relative_to(root).as_posix()}: managed API transport uses "
                    f"forbidden subnet-routing token {token!r}"
                )
        if re.search(r"\bip_forwarding_enabled\s*=\s*true\b", text):
            errors.append(
                f"{path.relative_to(root).as_posix()}: managed API transport enables IP forwarding"
            )
    return errors


def _validate_managed_api_services(services: str) -> list[str]:
    errors: list[str] = []
    service_approval = re.findall(
        r'"svc:kube-api-\$\{cluster_name\}"\s*:\s*\[\s*"\$\{router_tag\}"\s*\]',
        services,
    )
    if len(service_approval) != 1:
        errors.append(
            f"{CLOUD_HEADSCALE_POLICY.as_posix()}: each managed API Service requires "
            "its corresponding cluster-specific router-tag autoApproval"
        )
    service_keys = re.findall(r'"([^"\\]*(?:\\.[^"\\]*)*)"\s*:', services)
    if len(service_approval) == 1 and service_keys != ["svc:kube-api-${cluster_name}"]:
        errors.append(
            f"{CLOUD_HEADSCALE_POLICY.as_posix()}: managed API Service "
            "autoApprovals must contain only the cluster-specific Service/router-tag pair"
        )
    if re.search(r'"tag:kube-api"\s*:', services) or re.search(
        r'\[\s*"tag:vpc-router"\s*\]', services
    ):
        errors.append(
            f"{CLOUD_HEADSCALE_POLICY.as_posix()}: shared managed API Service "
            "autoApproval is forbidden"
        )
    return errors


def managed_api_service_errors(root: Path) -> list[str]:
    """Keep managed Kubernetes API access on Tailscale Services, not subnet routes."""

    errors = managed_api_transport_errors(root)

    policy_path = root / CLOUD_HEADSCALE_POLICY
    if not policy_path.is_file():
        return errors
    policy = policy_path.read_text(encoding="utf-8")
    auto_approvers = hujson_object_body(policy, "autoApprovers")
    routes = hujson_object_body(auto_approvers, "routes") if auto_approvers else None
    services = hujson_object_body(auto_approvers, "services") if auto_approvers else None
    if routes is None or services is None:
        errors.append(
            f"{CLOUD_HEADSCALE_POLICY.as_posix()}: cannot identify route and service "
            "autoApprover blocks"
        )
        return errors
    if "tag:vpc-router" in routes:
        errors.append(
            f"{CLOUD_HEADSCALE_POLICY.as_posix()}: tag:vpc-router must not approve subnet routes"
        )
    errors.extend(_validate_managed_api_services(services))
    grants = hujson_array_body(policy, "grants")
    if grants is None:
        errors.append(f"{CLOUD_HEADSCALE_POLICY.as_posix()}: cannot identify the grant list")
        return errors
    errors.extend(cloud_member_grant_errors(grants))
    kube_api_grant = re.findall(
        r"""\{
            \s*"src"\s*:\s*\[\s*"autogroup:member"\s*,\s*"tag:k8s-egress"\s*,?\s*\]\s*,
            \s*"dst"\s*:\s*\[\s*"tag:kube-api"\s*,?\s*\]\s*,
            \s*"ip"\s*:\s*\[\s*"tcp:443"\s*,?\s*\]\s*,?
            \s*\}""",
        grants,
        flags=re.VERBOSE,
    )
    if len(kube_api_grant) != 1 or grants.count('"tag:kube-api"') != 1:
        errors.append(
            f"{CLOUD_HEADSCALE_POLICY.as_posix()}: managed API consumers require "
            "exactly one autogroup:member/tag:k8s-egress to tag:kube-api TCP/443 grant"
        )
    if re.search(r'"(?:tag:vpc-router(?:-[^"]*)?|\$\{router_tag\})"', grants):
        errors.append(
            f"{CLOUD_HEADSCALE_POLICY.as_posix()}: managed API router tags must not "
            "receive or originate a network grant"
        )
    return errors


def cloud_member_grant_errors(grants: str) -> list[str]:
    """Reject cloud member grants outside exact private service and DNS access."""

    approved = {
        (
            frozenset({"autogroup:member"}),
            frozenset({"autogroup:self"}),
            frozenset({"tcp:2222", "tcp:6767"}),
        ),
        (
            frozenset({"autogroup:member", "tag:k8s-egress"}),
            frozenset({"tag:kube-api"}),
            frozenset({"tcp:443"}),
        ),
        (
            frozenset({"autogroup:member"}),
            frozenset({"${route}"}),
            frozenset({"tcp:53", "udp:53"}),
        ),
        (
            frozenset({"autogroup:member", "tag:k8s-egress"}),
            frozenset({"${address}/32"}),
            frozenset({"tcp:443"}),
        ),
    }
    observed = {
        normalized_template_grant(grant)
        for grant in hujson_object_bodies(grants)
        if '"autogroup:member"' in grant
    }
    errors = []
    for grant in sorted(observed - approved, key=repr):
        errors.append(
            f"{CLOUD_HEADSCALE_POLICY.as_posix()}: autogroup:member has an unreviewed "
            f"grant to {sorted(grant[1])} on {sorted(grant[2])}"
        )
    return errors


def normalized_template_grant(
    grant: str,
) -> tuple[frozenset[str], frozenset[str], frozenset[str]]:
    """Return one templated HuJSON grant as an order-independent contract."""

    return (
        hujson_array_strings(grant, "src"),
        hujson_array_strings(grant, "dst"),
        hujson_array_strings(grant, "ip"),
    )


def hujson_array_strings(text: str, key: str) -> frozenset[str]:
    """Return the quoted strings from one named HuJSON array."""

    body = hujson_array_body(text, key) or ""
    return frozenset(str(s) for s in re.findall(r'"([^"\\]*(?:\\.[^"\\]*)*)"', body))


def _strip_template_directives(text: str) -> str | None:
    parts: list[str] = []
    idx = 0
    while idx < len(text):
        if text.startswith("%{", idx):
            end = text.find("}", idx + 2)
            if end < 0:
                return None
            idx = end + 1
        else:
            parts.append(text[idx])
            idx += 1
    return "".join(parts)


def _scan_top_level_objects(text: str) -> tuple[str, ...]:
    objects = []
    start = None
    depth = 0
    escaped = False
    in_string = False
    for index, char in enumerate(text):
        if escaped:
            escaped = False
        elif in_string:
            escaped = char == "\\"
            in_string = char != '"'
        elif char == '"':
            in_string = True
        elif char == "{":
            start = index if depth == 0 else start
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0 and start is not None:
                objects.append(text[start : index + 1])
                start = None
    return tuple(objects) if depth == 0 else ()


def hujson_object_bodies(text: str) -> tuple[str, ...]:
    """Return top-level object bodies while ignoring Terraform template directives."""

    cleaned = _strip_template_directives(text)
    if cleaned is None:
        return ()
    return _scan_top_level_objects(cleaned)


def hujson_object_body(text: str, key: str) -> str | None:
    """Return a named object's body while ignoring braces inside strings."""

    return hujson_collection_body(text, key, "{", "}")


def hujson_array_body(text: str, key: str) -> str | None:
    """Return a named array's body while ignoring brackets inside strings."""

    return hujson_collection_body(text, key, "[", "]")


def _scan_collection_body(text: str, start: int, opening: str, closing: str) -> str | None:
    depth = 1
    escaped = False
    in_string = False
    for index in range(start, len(text)):
        character = text[index]
        if escaped:
            escaped = False
        elif in_string:
            escaped = character == "\\"
            in_string = character != '"'
        elif character == '"':
            in_string = True
        elif character == opening:
            depth += 1
        elif character == closing:
            depth -= 1
            if depth == 0:
                return text[start:index]
    return None


def hujson_collection_body(text: str, key: str, opening: str, closing: str) -> str | None:
    """Return a named collection's body while ignoring delimiters in strings."""

    match = re.search(rf'"{re.escape(key)}"\s*:\s*{re.escape(opening)}', text)
    if match is None:
        return None
    return _scan_collection_body(text, match.end(), opening, closing)


def sequence(value: object) -> Sequence[object]:
    """Return a non-string sequence or an empty sequence for malformed fields."""

    return value if isinstance(value, Sequence) and not isinstance(value, (str, bytes)) else ()


def source_paths(root: Path) -> Iterator[Path]:
    """Yield authored manifest and template sources."""

    source_root = root / "src"
    if not source_root.is_dir():
        raise FileNotFoundError(f"source root does not exist: {source_root}")
    for path in sorted(source_root.rglob("*")):
        if (
            path.is_file()
            and path.suffix in SOURCE_SUFFIXES
            and not IGNORED_PATH_SEGMENTS.intersection(path.parts)
        ):
            yield path


def source_surfaces(path: str, text: str) -> list[SourceSurface]:
    """Find exposure-sensitive constructs in one authored source."""

    surfaces: list[SourceSurface] = []
    for document in re.split(r"(?m)^---\s*$", text):
        resource = source_resource(document)
        lines = document.splitlines()
        load_balancers = sum(
            len(re.findall(r"\b(?:type|value):\s*[\"']?LoadBalancer[\"']?\b", code))
            for line in lines
            if not line.lstrip().startswith("#")
            for code in (line.split("#", 1)[0],)
        )
        surfaces.extend(SourceSurface(path, LOAD_BALANCER, resource) for _ in range(load_balancers))
        node_ports = sum(
            len(re.findall(r"\b(?:type|value):\s*[\"']?NodePort[\"']?\b", code))
            for line in lines
            if not line.lstrip().startswith("#")
            for code in (line.split("#", 1)[0],)
        )
        surfaces.extend(SourceSurface(path, NODE_PORT, resource) for _ in range(node_ports))
        ingresses = sum(
            len(re.findall(r"\bkind:\s*[\"']?Ingress[\"']?\b", code))
            for line in lines
            if not line.lstrip().startswith("#")
            for code in (line.split("#", 1)[0],)
        )
        surfaces.extend(SourceSurface(path, INGRESS, resource) for _ in range(ingresses))
        public_load_balancer_defaults = sum(
            len(re.findall(r"\bdefaultLoadBalancerScheme:\s*[\"']?internet-facing[\"']?\b", code))
            for line in lines
            if not line.lstrip().startswith("#")
            for code in (line.split("#", 1)[0],)
        )
        surfaces.extend(
            SourceSurface(path, PUBLIC_LOAD_BALANCER_DEFAULT, resource)
            for _ in range(public_load_balancer_defaults)
        )
        for line_number, line in enumerate(lines):
            if line.lstrip().startswith("#"):
                continue
            code = line.split("#", 1)[0]
            if re.search(r"\bexternalIPs\s*:", code) or (
                re.search(r"/spec/externalIPs(?:/\d+)?(?=[\s,}}]|$)", code)
                and not re.search(r"\bop:\s*[\"']?remove[\"']?(?=[\s,}}]|$)", code)
                and not removal_patch(lines, line_number)
            ):
                surfaces.append(SourceSurface(path, EXTERNAL_IPS, resource))
            if EXTERNAL_DNS_HOSTNAME in line or (
                EXTERNAL_DNS_HOSTNAME_PATCH in line and not removal_patch(lines, line_number)
            ):
                surfaces.append(SourceSurface(path, EXTERNAL_DNS_HOSTNAME_SURFACE, resource))
            if re.search(
                rf"{re.escape(EXTERNAL_DNS_SOURCE_LABEL)}:\s*[\"']?"
                rf"{re.escape(EXTERNAL_DNS_SOURCE_VALUE)}[\"']?(?=[\s,}}]|$)",
                line,
            ) or re.search(
                rf"\bvalue:\s*[\"']?{re.escape(EXTERNAL_DNS_SOURCE_VALUE)}"
                rf"[\"']?(?=[\s,}}]|$)",
                line,
            ):
                surfaces.append(SourceSurface(path, EXTERNAL_DNS_SOURCE_SURFACE, resource))
    return surfaces


def removal_patch(lines: Sequence[str], line_number: int) -> bool:
    """Return whether an encoded hostname path belongs to a remove operation."""

    if line_number == 0:
        return False
    return re.match(r"^\s*-\s*op:\s*remove\s*$", lines[line_number - 1]) is not None


def source_resource(document: str) -> ResourceIdentity:
    """Read stable top-level identity fields without evaluating templates."""

    api_version = scalar(document, "apiVersion", 0)
    kind = scalar(document, "kind", 0)
    metadata = re.search(r"(?ms)^metadata:\s*$\n(?P<body>(?:^[ \t]+.*(?:\n|$))*)", document)
    metadata_body = metadata.group("body") if metadata else ""
    name = scalar(metadata_body, "name", 2)
    namespace = scalar(metadata_body, "namespace", 2) or "default"
    if not api_version and not kind and not name:
        return EMPTY_RESOURCE
    return ResourceIdentity(api_version, kind, namespace, name)


def scalar(text: str, key: str, indentation: int) -> str:
    """Return one plain scalar at an exact indentation level."""

    indent = " " * indentation
    match = re.search(
        rf"(?m)^{indent}{re.escape(key)}:\s*[\"']?(?P<value>[^\s\"'{{}}]+)[\"']?\s*$",
        text,
    )
    if match is None:
        return ""
    val = match.group("value")
    return str(val) if val is not None else ""


def rendered_errors(owner: str, documents: Iterable[object]) -> list[str]:
    """Return direct-exposure violations in rendered Kubernetes resources."""

    owner = owner.removeprefix("@@").removeprefix("@")
    errors: list[str] = []
    for document in documents:
        if not isinstance(document, Mapping):
            continue
        resource = rendered_resource(document)
        for surface in rendered_surfaces(document):
            item = RenderedSurface(owner, surface, resource)
            if item not in APPROVED_RENDERED_SURFACES:
                errors.append(
                    f"{owner}: {resource_name(resource)} uses {surface} "
                    "outside the reviewed exposure allowlist"
                )
    return errors


def rendered_resource(document: Mapping[object, object]) -> ResourceIdentity:
    """Return one rendered resource's stable identity."""

    metadata = document.get("metadata", {})
    if not isinstance(metadata, Mapping):
        metadata = {}
    return ResourceIdentity(
        str(document.get("apiVersion", "")),
        str(document.get("kind", "")),
        str(metadata.get("namespace", "default")),
        str(metadata.get("name", "")),
    )


def _envoy_proxy_surfaces(spec: Mapping[object, object]) -> list[str]:
    provider = mapping(spec.get("provider"))
    kubernetes = mapping(provider.get("kubernetes"))
    envoy_service = mapping(kubernetes.get("envoyService"))
    stype = envoy_service.get("type")
    res: list[str] = []
    if stype == "LoadBalancer":
        res.append(LOAD_BALANCER)
    elif stype == "NodePort":
        res.append(NODE_PORT)
    return res


def rendered_surfaces(document: Mapping[object, object]) -> list[str]:
    """Return the exposure-sensitive constructs on one rendered resource."""

    metadata = mapping(document.get("metadata"))
    annotations = mapping(metadata.get("annotations"))
    labels = mapping(metadata.get("labels"))
    spec = mapping(document.get("spec"))
    surfaces: list[str] = []
    kind = document.get("kind")
    if kind == "Ingress":
        surfaces.append(INGRESS)
    elif kind == "Service":
        stype = spec.get("type")
        if stype == "LoadBalancer":
            surfaces.append(LOAD_BALANCER)
        elif stype == "NodePort":
            surfaces.append(NODE_PORT)
        if spec.get("externalIPs"):
            surfaces.append(EXTERNAL_IPS)
    elif kind == "EnvoyProxy":
        surfaces.extend(_envoy_proxy_surfaces(spec))
    if annotations.get(EXTERNAL_DNS_HOSTNAME) is not None:
        surfaces.append(EXTERNAL_DNS_HOSTNAME_SURFACE)
    if labels.get(EXTERNAL_DNS_SOURCE_LABEL) == EXTERNAL_DNS_SOURCE_VALUE:
        surfaces.append(EXTERNAL_DNS_SOURCE_SURFACE)
    return surfaces


def mapping(value: object) -> Mapping[object, object]:
    """Return a mapping or an empty mapping for malformed optional fields."""

    if not isinstance(value, Mapping):
        return {}
    res: Mapping[object, object] = dict(value)
    return res


def resource_name(resource: ResourceIdentity) -> str:
    """Format one stable resource identity for diagnostics."""

    if resource == EMPTY_RESOURCE:
        return "template helper"
    return f"{resource.kind} {resource.namespace}/{resource.name}"


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    """Parse the source or rendered validation command."""

    if not argv:
        argv = ["source"]
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="mode", required=True)
    source_parser = subparsers.add_parser("source")
    source_parser.add_argument(
        "--root",
        type=Path,
        default=Path(os.environ.get("BUILD_WORKSPACE_DIRECTORY", ".")),
    )
    rendered_parser = subparsers.add_parser("rendered")
    rendered_parser.add_argument("--owner", required=True)
    rendered_parser.add_argument("--manifest", required=True, type=Path)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Validate authored sources or one rendered manifest."""

    args = parse_args(sys.argv[1:] if argv is None else argv)
    if args.mode == "source":
        errors = source_errors(args.root.resolve())
    else:
        documents = yaml.safe_load_all(args.manifest.read_text(encoding="utf-8"))
        errors = rendered_errors(args.owner, documents)
    if errors:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
