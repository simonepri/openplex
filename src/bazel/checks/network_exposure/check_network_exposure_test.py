#!/usr/bin/env python3
"""Test network exposure validation logic against compliant and non-compliant Kubernetes service resources."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import yaml
from check_network_exposure import (
    EXTERNAL_DNS_HOSTNAME_SURFACE,
    EXTERNAL_DNS_SOURCE_SURFACE,
    EXTERNAL_IPS,
    INGRESS,
    LOAD_BALANCER,
    NODE_PORT,
    PUBLIC_LOAD_BALANCER_DEFAULT,
    rendered_errors,
    source_errors,
    source_surfaces,
)

TWO_ERRORS = 2
THREE_ERRORS = 3


class BaseSourceExposureTest(unittest.TestCase):
    @staticmethod
    def source_errors(relative_path: str, contents: str) -> list[str]:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / relative_path
            path.parent.mkdir(parents=True)
            path.write_text(contents, encoding="utf-8")
            return source_errors(root)

    @staticmethod
    def local_headscale_errors(policy: str) -> list[str]:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cells = root / "src/infra/cells"
            cells.mkdir(parents=True)
            (cells / "ctrl-eaws-lh1.yaml").write_text(
                "provider: floci\nnetwork: {service_cidr: 172.31.0.0/20}\n",
                encoding="utf-8",
            )
            (cells / "cell-eaws-lh1.yaml").write_text(
                "provider: floci\nnetwork: {service_cidr: 172.31.16.0/20}\n",
                encoding="utf-8",
            )
            policy_path = root / "src/infra/tools/cloud_emulator/stack/headscale/policy.hujson"
            policy_path.parent.mkdir(parents=True)
            policy_path.write_text(policy, encoding="utf-8")
            return source_errors(root)

    @staticmethod
    def managed_api_errors(router: str, policy: str) -> list[str]:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            router_path = root / (
                "src/infra/terraform/components/tailscale_vpc_router_aws/router.sh.tftpl"
            )
            router_path.parent.mkdir(parents=True)
            router_path.write_text(router, encoding="utf-8")
            policy_path = root / (
                "src/infra/argocd/components/tailscale_access/cloud-policy.hujson"
            )
            policy_path.parent.mkdir(parents=True)
            policy_path.write_text(policy, encoding="utf-8")
            return source_errors(root)


class SourceExposureAccessTest(BaseSourceExposureTest):
    def test_managed_api_uses_only_tailscale_service_transport(self) -> None:
        errors = self.managed_api_errors(
            router=(
                "ip_forwarding_enabled = false\n"
                "tailscale serve --service=svc:kube-api-cell-example tcp:443 tcp://api.example:443\n"
            ),
            policy="""
{
  "autoApprovers": {
    "routes": {"172.16.0.0/12": ["tag:k8s", "tag:subnet-router"]},
    "services": {
      "svc:kube-api-${cluster_name}": ["${router_tag}"],
    },
  },
  "grants": [
    {
      "src": ["autogroup:member", "tag:k8s-egress"],
      "dst": ["tag:kube-api"],
      "ip": ["tcp:443"],
    },
  ],
}
""",
        )

        assert errors == []

    def test_managed_api_subnet_routing_is_rejected(self) -> None:
        errors = self.managed_api_errors(
            router='tailscale up --advertise-routes="10.0.0.0/28"\n',
            policy="""
{
  "autoApprovers": {
    "routes": {"10.0.0.0/28": ["tag:vpc-router"]},
    "services": {
      "svc:kube-api-${cluster_name}": ["${router_tag}"],
    },
  },
  "grants": [
    {
      "src": ["autogroup:member", "tag:k8s-egress"],
      "dst": ["tag:kube-api"],
      "ip": ["tcp:443"],
    },
  ],
}
""",
        )

        assert any("--advertise-routes" in error for error in errors)
        assert any("must not approve subnet routes" in error for error in errors)

    def test_managed_api_handoff_cannot_carry_advertised_routes(self) -> None:
        errors = self.managed_api_errors(
            router='api_transport = { advertised_routes = ["10.0.0.0/28"] }\n',
            policy="""
{
  "autoApprovers": {
    "routes": {},
    "services": {"svc:kube-api-${cluster_name}": ["${router_tag}"]},
  },
  "grants": [
    {
      "src": ["autogroup:member", "tag:k8s-egress"],
      "dst": ["tag:kube-api"],
      "ip": ["tcp:443"],
    },
  ],
}
""",
        )

        assert len(errors) == 1
        assert "advertised_routes" in errors[0]

    def test_managed_api_router_cannot_enable_ip_forwarding(self) -> None:
        errors = self.managed_api_errors(
            router="ip_forwarding_enabled = true\n",
            policy="""
{
  "autoApprovers": {
    "routes": {},
    "services": {"svc:kube-api-${cluster_name}": ["${router_tag}"]},
  },
  "grants": [
    {
      "src": ["autogroup:member", "tag:k8s-egress"],
      "dst": ["tag:kube-api"],
      "ip": ["tcp:443"],
    },
  ],
}
""",
        )

        assert len(errors) == 1
        assert "enables IP forwarding" in errors[0]

    def test_managed_api_grant_cannot_broaden_beyond_tcp_443(self) -> None:
        errors = self.managed_api_errors(
            router="ip_forwarding_enabled = false\n",
            policy="""
{
  "autoApprovers": {
    "routes": {},
    "services": {"svc:kube-api-${cluster_name}": ["${router_tag}"]},
  },
  "grants": [
    {
      "src": ["autogroup:member", "tag:k8s-egress"],
      "dst": ["tag:kube-api"],
      "ip": ["*"],
    },
  ],
}
""",
        )

        assert any("exactly one" in error for error in errors)

    def test_cloud_members_cannot_reach_arbitrary_network_destinations(self) -> None:
        for destination in ("172.16.0.0/12", "172.31.0.12/32", "0.0.0.0/0"):
            with self.subTest(destination=destination):
                errors = self.managed_api_errors(
                    router="ip_forwarding_enabled = false\n",
                    policy=f"""
{{
  "autoApprovers": {{
    "routes": {{}},
    "services": {{"svc:kube-api-${{cluster_name}}": ["${{router_tag}}"]}},
  }},
  "grants": [
    {{
      "src": ["autogroup:member", "tag:k8s-egress"],
      "dst": ["tag:kube-api"],
      "ip": ["tcp:443"],
    }},
    {{
      "src": ["autogroup:member"],
      "dst": ["{destination}"],
      "ip": ["*"],
    }},
  ],
}}
""",
                )

                assert len(errors) == 1
                assert "unreviewed grant" in errors[0]

    def test_cloud_members_reach_only_exact_dns_and_gateway_templates(self) -> None:
        errors = self.managed_api_errors(
            router="ip_forwarding_enabled = false\n",
            policy="""
{
  "autoApprovers": {
    "routes": {},
    "services": {"svc:kube-api-${cluster_name}": ["${router_tag}"]},
  },
  "grants": [
    {
      "src": ["autogroup:member", "tag:k8s-egress"],
      "dst": ["tag:kube-api"],
      "ip": ["tcp:443"],
    },
    {
      "src": ["autogroup:member"],
      "dst": [
%{ for route in resolver_route_cidrs ~}
        "${route}",
%{ endfor ~}
      ],
      "ip": ["tcp:53", "udp:53"],
    },
    {
      "src": ["autogroup:member", "tag:k8s-egress"],
      "dst": [
%{ for address in private_gateway_ipv4s ~}
        "${address}/32",
%{ endfor ~}
      ],
      "ip": ["tcp:443"],
    },
  ],
}
""",
        )

        assert errors == []

    def test_managed_api_router_cannot_receive_a_network_grant(self) -> None:
        errors = self.managed_api_errors(
            router="ip_forwarding_enabled = false\n",
            policy="""
{
  "autoApprovers": {
    "routes": {},
    "services": {"svc:kube-api-${cluster_name}": ["${router_tag}"]},
  },
  "grants": [
    {
      "src": ["autogroup:member", "tag:k8s-egress"],
      "dst": ["tag:kube-api"],
      "ip": ["tcp:443"],
    },
    {
      "src": ["autogroup:member"],
      "dst": ["tag:vpc-router"],
      "ip": ["tcp:22"],
    },
  ],
}
""",
        )

        assert any("must not receive or originate" in error for error in errors)

    def test_managed_api_cluster_router_tags_cannot_receive_network_grants(self) -> None:
        for destination in ("tag:vpc-router-cell-example", "${router_tag}"):
            with self.subTest(destination=destination):
                errors = self.managed_api_errors(
                    router="ip_forwarding_enabled = false\n",
                    policy=f"""
{{
  "autoApprovers": {{
    "routes": {{}},
    "services": {{"svc:kube-api-${{cluster_name}}": ["${{router_tag}}"]}},
  }},
  "grants": [
    {{
      "src": ["autogroup:member", "tag:k8s-egress"],
      "dst": ["tag:kube-api"],
      "ip": ["tcp:443"],
    }},
    {{
      "src": ["tag:k8s"],
      "dst": ["{destination}"],
      "ip": ["tcp:22"],
    }},
  ],
}}
""",
                )

                assert len(errors) == 1
                assert "must not receive or originate" in errors[0]

    def test_managed_api_service_cannot_use_a_shared_router_approval(self) -> None:
        errors = self.managed_api_errors(
            router="ip_forwarding_enabled = false\n",
            policy="""
{
  "autoApprovers": {
    "routes": {},
    "services": {"tag:kube-api": ["tag:vpc-router"]},
  },
  "grants": [
    {
      "src": ["autogroup:member", "tag:k8s-egress"],
      "dst": ["tag:kube-api"],
      "ip": ["tcp:443"],
    },
  ],
}
""",
        )

        assert len(errors) == TWO_ERRORS
        assert any("cluster-specific" in error for error in errors)
        assert any("shared" in error for error in errors)

    def test_managed_api_service_cannot_add_another_router_pair(self) -> None:
        errors = self.managed_api_errors(
            router="ip_forwarding_enabled = false\n",
            policy="""
{
  "autoApprovers": {
    "routes": {},
    "services": {
      "svc:kube-api-${cluster_name}": ["${router_tag}"],
      "svc:kube-api-cell-other": ["tag:vpc-router-cell-other"],
    },
  },
  "grants": [
    {
      "src": ["autogroup:member", "tag:k8s-egress"],
      "dst": ["tag:kube-api"],
      "ip": ["tcp:443"],
    },
  ],
}
""",
        )

        assert len(errors) == 1
        assert "only the cluster-specific Service/router-tag pair" in errors[0]

    def test_local_headscale_members_reach_only_private_gateways(self) -> None:
        errors = self.local_headscale_errors(
            """
// Local policy fixture.
{
  "grants": [
    {"src": ["autogroup:member"], "dst": ["autogroup:self"], "ip": ["tcp:2222", "tcp:6767"],},
    {"src": ["autogroup:member"], "dst": ["172.31.0.0/20", "172.31.16.0/20"], "ip": ["tcp:2222"],},
    {"src": ["autogroup:member", "tag:workspace"], "dst": ["172.31.0.11/32", "172.31.16.11/32"], "ip": ["tcp:443"],},
    {"src": ["autogroup:member", "tag:workspace"], "dst": ["172.31.0.10/32", "172.31.16.10/32"], "ip": ["tcp:53", "udp:53"],},
    {"src": ["autogroup:member", "tag:workspace"], "dst": ["tag:subnet-router"], "ip": ["tcp:8444"],},
  ],
}
"""
        )

        assert errors == []

    def test_local_headscale_members_cannot_reach_arbitrary_cluster_ips(self) -> None:
        for destination in ("172.16.0.0/12", "172.31.0.12/32", "172.31.0.9/32"):
            with self.subTest(destination=destination):
                errors = self.local_headscale_errors(
                    f"""
{{
  "grants": [
    {{"src": ["autogroup:member"], "dst": ["autogroup:self"], "ip": ["tcp:2222", "tcp:6767"]}},
    {{"src": ["autogroup:member"], "dst": ["172.31.0.0/20", "172.31.16.0/20"], "ip": ["tcp:2222"]}},
    {{"src": ["autogroup:member", "tag:workspace"], "dst": ["172.31.0.11/32", "172.31.16.11/32"], "ip": ["tcp:443"]}},
    {{"src": ["autogroup:member", "tag:workspace"], "dst": ["172.31.0.10/32", "172.31.16.10/32"], "ip": ["tcp:53", "udp:53"]}},
    {{"src": ["autogroup:member", "tag:workspace"], "dst": ["tag:subnet-router"], "ip": ["tcp:8444"]}},
    {{"src": ["autogroup:member"], "dst": ["{destination}"], "ip": ["*"]}}
  ]
}}
"""
                )

                assert len(errors) == 1
                assert "unreviewed grant" in errors[0]

    def test_local_headscale_members_cannot_reach_non_ssh_ports_on_cluster_subnets(self) -> None:
        errors = self.local_headscale_errors(
            """
{
  "grants": [
    {"src": ["autogroup:member"], "dst": ["autogroup:self"], "ip": ["tcp:2222", "tcp:6767"]},
    {"src": ["autogroup:member"], "dst": ["172.31.0.0/20", "172.31.16.0/20"], "ip": ["tcp:80"]},
    {"src": ["autogroup:member", "tag:workspace"], "dst": ["172.31.0.11/32", "172.31.16.11/32"], "ip": ["tcp:443"]},
    {"src": ["autogroup:member", "tag:workspace"], "dst": ["172.31.0.10/32", "172.31.16.10/32"], "ip": ["tcp:53", "udp:53"]},
    {"src": ["autogroup:member", "tag:workspace"], "dst": ["tag:subnet-router"], "ip": ["tcp:8444"]},
  ]
}
"""
        )

        assert len(errors) == TWO_ERRORS
        assert any("unreviewed grant" in error for error in errors)
        assert any("required autogroup:member grant" in error for error in errors)


class SourceExposureManifestTest(BaseSourceExposureTest):
    def test_unreviewed_node_port_and_external_ips_are_rejected(self) -> None:
        errors = self.source_errors(
            "src/example/service.yaml",
            """
apiVersion: v1
kind: Service
metadata: {name: direct-door, namespace: team}
spec:
  type: NodePort
  externalIPs: [192.0.2.10]
""",
        )

        assert len(errors) == TWO_ERRORS
        assert any(NODE_PORT in error for error in errors)
        assert any(EXTERNAL_IPS in error for error in errors)

    def test_external_ips_remove_patch_is_not_an_exposure(self) -> None:
        errors = self.source_errors(
            "src/example/application.yaml",
            """
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata: {name: example, namespace: argocd}
spec:
  patches:
    - op: remove
      path: /spec/externalIPs
""",
        )

        assert errors == []

    def test_controller_wide_public_load_balancer_default_is_rejected(self) -> None:
        errors = self.source_errors(
            "src/infra/argocd/components/aws_load_balancer_controller/helm/values.yaml",
            "defaultLoadBalancerScheme: internet-facing\n",
        )

        assert len(errors) == 1
        assert PUBLIC_LOAD_BALANCER_DEFAULT in errors[0]

    def test_deferred_s3_path_and_resource_are_allowed(self) -> None:
        errors = self.source_errors(
            "src/infra/argocd/components/s3_gateway/helm/templates/routes.yaml",
            """
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: s3-gateway-cross-region
  namespace: s3-system
  annotations:
    external-dns.alpha.kubernetes.io/hostname: {{ $target.crossRegionServer | quote }}
  labels:
    app.kubernetes.io/component: external-dns-source
spec: {}
""",
        )

        assert errors == []

    def test_raw_service_is_rejected_even_at_a_former_bootstrap_path(self) -> None:
        errors = self.source_errors(
            "src/infra/argocd/components/headscale/service.yaml",
            """
apiVersion: v1
kind: Service
metadata:
  name: headscale
  namespace: headscale
spec:
  type: LoadBalancer
""",
        )

        assert len(errors) == 1
        assert "Service headscale/headscale" in errors[0]

    def test_public_gateway_generated_service_is_allowed(self) -> None:
        errors = self.source_errors(
            "src/infra/argocd/components/envoy_gateway_instance/helm/values.yaml",
            """
proxy:
  service:
    name: gateway
    type: LoadBalancer
""",
        )

        assert errors == []

    def test_deferred_s3_path_does_not_allow_another_resource(self) -> None:
        errors = self.source_errors(
            "src/infra/argocd/components/s3_gateway/helm/templates/routes.yaml",
            """
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: shadow-door
  namespace: s3-system
  annotations:
    external-dns.alpha.kubernetes.io/hostname: shadow.unit.test
  labels:
    app.kubernetes.io/component: external-dns-source
spec: {}
""",
        )

        assert len(errors) == TWO_ERRORS
        assert all("HTTPRoute s3-system/shadow-door" in error for error in errors)

    def test_approved_values_file_cannot_add_a_second_load_balancer(self) -> None:
        errors = self.source_errors(
            "src/infra/argocd/components/envoy_gateway_instance/helm/values.yaml",
            """
proxy:
  service:
    type: LoadBalancer
otherProxy:
  service:
    type: LoadBalancer
""",
        )

        assert len(errors) == 1
        assert LOAD_BALANCER in errors[0]

    def test_unreviewed_service_and_external_dns_are_rejected(self) -> None:
        errors = self.source_errors(
            "src/infra/argocd/components/example/kustomize/base/service.yaml",
            """
apiVersion: v1
kind: Service
metadata:
  name: direct-door
  namespace: observability
  annotations:
    external-dns.alpha.kubernetes.io/hostname: direct.unit.test
  labels:
    app.kubernetes.io/component: external-dns-source
spec:
  type: LoadBalancer
""",
        )

        assert len(errors) == THREE_ERRORS
        assert any(LOAD_BALANCER in error for error in errors)
        assert any(EXTERNAL_DNS_HOSTNAME_SURFACE in error for error in errors)
        assert any(EXTERNAL_DNS_SOURCE_SURFACE in error for error in errors)

    def test_external_dns_remove_patch_is_not_an_exposure(self) -> None:
        errors = self.source_errors(
            "src/infra/argocd/fleet/cluster/templates/applications/example.yaml",
            """
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: example
spec:
  source:
    kustomize:
      patches:
        - target: {version: v1, kind: Service, name: example}
          patch: |-
            - op: remove
              path: /metadata/annotations/external-dns.alpha.kubernetes.io~1hostname
""",
        )

        assert errors == []

    def test_external_dns_replace_patch_is_rejected(self) -> None:
        errors = self.source_errors(
            "src/infra/argocd/fleet/cluster/templates/applications/example.yaml",
            """
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: example
spec:
  source:
    kustomize:
      patches:
        - target: {version: v1, kind: Service, name: example}
          patch: |-
            - op: replace
              path: /metadata/annotations/external-dns.alpha.kubernetes.io~1hostname
              value: direct.unit.test
""",
        )

        assert len(errors) == 1
        assert EXTERNAL_DNS_HOSTNAME_SURFACE in errors[0]

    def test_external_dns_source_label_patch_is_rejected(self) -> None:
        errors = self.source_errors(
            "src/infra/argocd/fleet/cluster/templates/applications/example.yaml",
            """
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata: {name: example, namespace: argocd}
spec:
  source:
    kustomize:
      patches:
        - target: {version: v1, kind: Service, name: example}
          patch: |-
            - op: add
              path: /metadata/labels/app.kubernetes.io~1component
              value: external-dns-source
""",
        )

        assert len(errors) == 1
        assert EXTERNAL_DNS_SOURCE_SURFACE in errors[0]


class RenderedExposureTest(unittest.TestCase):
    def test_raw_node_port_and_external_ips_are_rejected(self) -> None:
        errors = self.errors(
            "//src/example:render",
            """
apiVersion: v1
kind: Service
metadata: {name: direct-door, namespace: team}
spec:
  type: NodePort
  externalIPs: [192.0.2.10]
""",
        )

        assert len(errors) == TWO_ERRORS
        assert any(NODE_PORT in error for error in errors)
        assert any(EXTERNAL_IPS in error for error in errors)

    def test_generated_envoy_node_port_is_rejected_without_a_render_owner(self) -> None:
        errors = self.errors(
            "//src/infra/argocd/components/envoy_gateway_instance:helm_render-local",
            """
apiVersion: gateway.envoyproxy.io/v1alpha1
kind: EnvoyProxy
metadata: {name: public, namespace: envoy-gateway-system}
spec:
  provider:
    kubernetes:
      envoyService: {type: NodePort}
""",
        )

        assert len(errors) == 1
        assert NODE_PORT in errors[0]

    def test_raw_service_is_rejected_even_for_a_former_bootstrap_owner(self) -> None:
        errors = self.errors(
            "//src/infra/argocd/components/headscale:manifests_render",
            """
apiVersion: v1
kind: Service
metadata: {name: headscale, namespace: headscale}
spec: {type: LoadBalancer}
""",
        )

        assert len(errors) == 1
        assert LOAD_BALANCER in errors[0]

    def test_approved_resource_from_another_owner_is_rejected(self) -> None:
        errors = self.errors(
            "//src/infra/argocd/components/example:base_render",
            """
apiVersion: gateway.envoyproxy.io/v1alpha1
kind: EnvoyProxy
metadata: {name: public, namespace: envoy-gateway-system}
spec:
  provider:
    kubernetes:
      envoyService: {type: LoadBalancer}
""",
        )

        assert len(errors) == 1
        assert LOAD_BALANCER in errors[0]

    def test_private_dragonfly_service_cannot_use_load_balancer_or_dns(self) -> None:
        errors = self.errors(
            "//src/infra/argocd/components/dragonfly:topology_v2_cell_render",
            """
apiVersion: v1
kind: Service
metadata:
  name: dragonfly-scheduler-private
  namespace: dragonfly-system
  annotations:
    external-dns.alpha.kubernetes.io/hostname: dragonfly.unit.test
spec: {type: LoadBalancer}
""",
        )

        assert len(errors) == TWO_ERRORS
        assert any(LOAD_BALANCER in error for error in errors)
        assert any(EXTERNAL_DNS_HOSTNAME_SURFACE in error for error in errors)

    def test_public_gateway_generated_load_balancer_is_allowed(self) -> None:
        errors = self.errors(
            "//src/infra/argocd/components/envoy_gateway_instance:helm_render-cloud",
            """
apiVersion: gateway.envoyproxy.io/v1alpha1
kind: EnvoyProxy
metadata: {name: public, namespace: envoy-gateway-system}
spec:
  provider:
    kubernetes:
      envoyService: {type: LoadBalancer}
""",
        )

        assert errors == []

    def test_another_generated_load_balancer_is_rejected(self) -> None:
        errors = self.errors(
            "//src/infra/argocd/components/envoy_gateway_instance:helm_render-cloud",
            """
apiVersion: gateway.envoyproxy.io/v1alpha1
kind: EnvoyProxy
metadata: {name: bypass, namespace: envoy-gateway-system}
spec:
  provider:
    kubernetes:
      envoyService: {type: LoadBalancer}
""",
        )

        assert len(errors) == 1
        assert "EnvoyProxy envoy-gateway-system/bypass" in errors[0]

    def test_observability_door_is_rejected(self) -> None:
        errors = self.errors(
            "//src/infra/argocd/components/observability_door:base_render",
            """
apiVersion: v1
kind: Service
metadata:
  name: observability-door
  namespace: observability
  annotations:
    external-dns.alpha.kubernetes.io/hostname: observability.unit.test
  labels:
    app.kubernetes.io/component: external-dns-source
spec: {type: LoadBalancer}
""",
        )

        assert len(errors) == THREE_ERRORS

    def test_deferred_s3_route_is_allowed(self) -> None:
        errors = self.errors(
            "//src/infra/argocd/components/s3_gateway:helm_render",
            """
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: s3-gateway-cross-region
  namespace: s3-system
  annotations:
    external-dns.alpha.kubernetes.io/hostname: s3.unit.test
  labels:
    app.kubernetes.io/component: external-dns-source
spec: {}
""",
        )

        assert errors == []

    def test_ingress_is_rejected(self) -> None:
        errors = self.errors(
            "//src/infra/argocd/components/test_app:base_render",
            """
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: unreviewed-ingress
  namespace: default
spec: {}
""",
        )

        assert len(errors) == 1
        assert "Ingress default/unreviewed-ingress uses ingress" in errors[0]

    @staticmethod
    def test_source_ingress_is_detected() -> None:
        surfaces = source_surfaces(
            "src/test/manifest.yaml",
            """
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: test-ingress
  namespace: test
spec: {}
""",
        )
        assert len(surfaces) == 1
        assert surfaces[0].surface == INGRESS

    @staticmethod
    def errors(owner: str, manifest: str) -> list[str]:
        return rendered_errors(owner, yaml.safe_load_all(manifest))


if __name__ == "__main__":
    unittest.main()
