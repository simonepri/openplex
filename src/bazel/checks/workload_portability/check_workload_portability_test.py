#!/usr/bin/env python3
"""Test workload manifest portability enforcement against node affinity, tolerations, and placement bindings."""

from __future__ import annotations

import re
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

from check_workload_portability import (
    WorkloadPortabilityError,
    accelerator_toleration_violations,
    cpu_capability_labels,
    discover_manifests,
    discover_projects,
    gpu_class_ids,
    gpu_class_max_counts,
    gpu_class_violations,
    infrastructure_placement_label,
    manifest_errors,
    tpu_class_counts,
    validate_repository,
)

DUAL_PLACEMENT_VIOLATION_COUNT = 2
L4_MAX_GPU_COUNT = 8


@contextmanager
def _assert_raises(expected_type: type[BaseException], match: str | None = None) -> Iterator[None]:
    try:
        yield
    except expected_type as err:
        if match is not None and not re.search(match, str(err)):
            msg = f"Expected exception matching {match!r}, got {err!r}"
            raise AssertionError(msg) from err
    else:
        msg = f"Expected {expected_type.__name__} but no exception was raised"
        raise AssertionError(msg)


class BaseWorkloadPortabilityTest(unittest.TestCase):
    @staticmethod
    def deployment(pod_spec: dict[str, object]) -> dict[str, object]:
        return {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "spec": {"template": {"spec": {"containers": [], **pod_spec}}},
        }

    @staticmethod
    def tpu_pod_spec(
        *,
        containers: list[dict[str, object]] | None = None,
        init_containers: list[dict[str, object]] | None = None,
        tpu_class: str = "v5e-2x2",
    ) -> dict[str, object]:
        pod_spec: dict[str, object] = {
            "containers": containers
            if containers is not None
            else [
                {
                    "name": "worker",
                    "resources": {"limits": {"google.com/tpu": "4"}},
                }
            ],
            "nodeSelector": {"tpu-class": tpu_class},
        }
        if init_containers is not None:
            pod_spec["initContainers"] = init_containers
        return pod_spec


class WorkloadPortabilityPlacementTest(BaseWorkloadPortabilityTest):
    def test_accelerator_tolerations_are_canonical_when_authored(self) -> None:
        cases = (
            ("google.com/tpu", "tpu-class", "v5e-2x2", "4"),
            ("nvidia.com/gpu", "gpu-class", "l4", "1"),
        )

        for resource, class_label, class_id, count in cases:
            with self.subTest(resource=resource):
                document = self.deployment({
                    "containers": [
                        {
                            "name": "worker",
                            "resources": {"limits": {resource: count}},
                        }
                    ],
                    "nodeSelector": {class_label: class_id},
                    "tolerations": [
                        {
                            "effect": "NoSchedule",
                            "key": resource,
                            "operator": "Equal",
                            "value": "present",
                        }
                    ],
                })

                assert accelerator_toleration_violations(document) == []

    def test_accelerator_tolerations_cannot_bypass_portable_placement(self) -> None:
        cases = (
            (
                "gpu-unpaired",
                {
                    "effect": "NoSchedule",
                    "key": "nvidia.com/gpu",
                    "operator": "Equal",
                    "value": "present",
                },
                {},
                "requires nodeSelector",
            ),
            (
                "tpu-unpaired",
                {
                    "effect": "NoSchedule",
                    "key": "google.com/tpu",
                    "operator": "Equal",
                    "value": "present",
                },
                {},
                "requires nodeSelector",
            ),
            (
                "gpu-exists",
                {"effect": "NoSchedule", "key": "nvidia.com/gpu", "operator": "Exists"},
                {
                    "containers": [
                        {
                            "name": "worker",
                            "resources": {"limits": {"nvidia.com/gpu": "1"}},
                        }
                    ],
                    "nodeSelector": {"gpu-class": "l4"},
                },
                "must use operator Equal",
            ),
            (
                "tpu-wrong-value",
                {
                    "effect": "NoSchedule",
                    "key": "google.com/tpu",
                    "operator": "Equal",
                    "value": "true",
                },
                {
                    "containers": [
                        {
                            "name": "worker",
                            "resources": {"limits": {"google.com/tpu": "4"}},
                        }
                    ],
                    "nodeSelector": {"tpu-class": "v5e-2x2"},
                },
                "must use operator Equal",
            ),
            (
                "wildcard",
                {"effect": "NoSchedule", "operator": "Exists"},
                {},
                "wildcard NoSchedule tolerations",
            ),
        )

        for name, toleration, pod_spec, reason in cases:
            with self.subTest(name=name):
                document = self.deployment({
                    **pod_spec,
                    "tolerations": [toleration],
                })

                violations = accelerator_toleration_violations(document)

                assert len(violations) == 1
                assert reason in violations[0].reason

    def test_infrastructure_placement_labels_are_recognized(self) -> None:
        labels = (
            # keep-sorted start
            "agentpool",
            "beta.kubernetes.io/instance-type",
            "cloud.google.com/gke-accelerator",
            "cloud.google.com/gke-nodepool",
            "cloud.google.com/gke-spot",
            "cloud.google.com/gke-tpu-accelerator",
            "cloud.google.com/gke-tpu-topology",
            "cloud.google.com/machine-family",
            "eks.amazonaws.com/capacityType",
            "eks.amazonaws.com/nodegroup",
            "failure-domain.beta.kubernetes.io/region",
            "failure-domain.beta.kubernetes.io/zone",
            "feature.node.kubernetes.io/cpu-cpuid.AVX2",
            "karpenter.k8s.aws/instance-category",
            "karpenter.k8s.aws/instance-family",
            "karpenter.k8s.aws/instance-generation",
            "karpenter.k8s.aws/instance-gpu-memory",
            "karpenter.k8s.aws/instance-gpu-name",
            "karpenter.k8s.aws/instance-size",
            "karpenter.k8s.aws/instance-type",
            "karpenter.k8s.gcp/instance-cpu-count",
            "karpenter.k8s.gcp/instance-gpu-count",
            "karpenter.k8s.gcp/instance-memory",
            "karpenter.sh/capacity-type",
            "karpenter.sh/nodepool",
            "kubernetes.io/hostname",
            "node.kubernetes.io/instance-type",
            "nvidia.com/gpu.product",
            "topology.kubernetes.io/region",
            "topology.kubernetes.io/zone",
            # keep-sorted end
        )

        for label in labels:
            with self.subTest(label=label):
                assert infrastructure_placement_label(label)

    def test_provider_placement_prefixes_cover_future_skus(self) -> None:
        labels = (
            # keep-sorted start
            "cloud.google.com/future-placement",
            "eks.amazonaws.com/future-placement",
            "k8s.amazonaws.com" + "/accelerator",
            "karpenter.k8s.aws/future-placement",
            "karpenter.k8s.gcp/future-placement",
            "karpenter.sh/future-placement",
            "nvidia.com/cuda.driver.major",
            # keep-sorted end
        )

        for label in labels:
            with self.subTest(label=label):
                assert infrastructure_placement_label(label)

    @staticmethod
    def test_unrelated_provider_identity_label_is_allowed() -> None:
        assert not infrastructure_placement_label("iam.gke.io/gcp-service-account")

    def test_provider_annotations_outside_placement_are_allowed(self) -> None:
        document = self.deployment({})
        document["metadata"] = {
            "annotations": {
                "eks.amazonaws.com/role-arn": "arn:aws:iam::123456789012:role/example",
                "iam.gke.io/gcp-service-account": "workload@example.iam.gserviceaccount.com",
                "karpenter.sh/do-not-disrupt": "true",
            }
        }

        assert manifest_errors(Path("annotations.k8s.yaml"), [document]) == []

    def test_portable_capability_labels_are_allowed(self) -> None:
        document = self.deployment({
            "nodeSelector": {
                "kubernetes.io/arch": "amd64",
                "cpu-capability.avx2": "true",
            },
            "topologySpreadConstraints": [
                {"maxSkew": 1, "topologyKey": "topology.kubernetes.io/zone"}
            ],
        })

        assert manifest_errors(Path("portable.k8s.yaml"), [document]) == []

    def test_provider_topology_is_rejected_as_direct_placement(self) -> None:
        for label in (
            "failure-domain.beta.kubernetes.io/region",
            "failure-domain.beta.kubernetes.io/zone",
            "topology.kubernetes.io/region",
            "topology.kubernetes.io/zone",
        ):
            with self.subTest(label=label):
                selector = self.deployment({"nodeSelector": {label: "provider-location"}})
                affinity = self.deployment({
                    "affinity": {
                        "nodeAffinity": {
                            "requiredDuringSchedulingIgnoredDuringExecution": {
                                "nodeSelectorTerms": [
                                    {
                                        "matchExpressions": [
                                            {
                                                "key": label,
                                                "operator": "In",
                                                "values": ["provider-location"],
                                            }
                                        ]
                                    }
                                ]
                            }
                        }
                    }
                })

                selector_errors = manifest_errors(Path("selector.k8s.yaml"), [selector])
                affinity_errors = manifest_errors(Path("affinity.k8s.yaml"), [affinity])

                assert len(selector_errors) == 1
                assert label in selector_errors[0]
                assert len(affinity_errors) == 1
                assert label in affinity_errors[0]

    def test_provider_topology_remains_valid_for_spreading(self) -> None:
        for label in (
            "failure-domain.beta.kubernetes.io/region",
            "failure-domain.beta.kubernetes.io/zone",
            "kubernetes.io/hostname",
            "topology.kubernetes.io/region",
            "topology.kubernetes.io/zone",
        ):
            with self.subTest(label=label):
                document = self.deployment({
                    "topologySpreadConstraints": [{"maxSkew": 1, "topologyKey": label}]
                })

                assert manifest_errors(Path("spread.k8s.yaml"), [document]) == []

    @staticmethod
    def test_cpu_capability_keys_come_from_the_catalog() -> None:
        assert cpu_capability_labels() == frozenset({"cpu-capability.avx2"})

    def test_unknown_cpu_capability_is_rejected(self) -> None:
        document = self.deployment({"nodeSelector": {"cpu-capability.avx512": "true"}})

        errors = manifest_errors(Path("unknown.k8s.yaml"), [document])

        assert len(errors) == 1
        assert "cpu-capability.avx512" in errors[0]
        assert "not declared by the CPU capability catalog" in errors[0]

    def test_cpu_capability_value_must_be_string_true(self) -> None:
        for value in (True, False, "false"):
            with self.subTest(value=value):
                document = self.deployment({"nodeSelector": {"cpu-capability.avx2": value}})

                errors = manifest_errors(Path("value.k8s.yaml"), [document])

                assert len(errors) == 1
                assert "must use the string value 'true'" in errors[0]

    def test_cpu_capability_required_and_preferred_affinity_are_rejected(self) -> None:
        expression = {
            "key": "cpu-capability.avx2",
            "operator": "In",
            "values": ["true"],
        }
        document = self.deployment({
            "affinity": {
                "nodeAffinity": {
                    "preferredDuringSchedulingIgnoredDuringExecution": [
                        {
                            "preference": {"matchExpressions": [expression]},
                            "weight": 1,
                        }
                    ],
                    "requiredDuringSchedulingIgnoredDuringExecution": {
                        "nodeSelectorTerms": [{"matchExpressions": [expression]}]
                    },
                }
            }
        })

        errors = manifest_errors(Path("affinity.k8s.yaml"), [document])

        assert len(errors) == DUAL_PLACEMENT_VIOLATION_COUNT
        assert all("allowed only in nodeSelector" in error for error in errors)

    def test_cpu_capability_topology_key_is_rejected(self) -> None:
        document = self.deployment({
            "topologySpreadConstraints": [
                {
                    "maxSkew": 1,
                    "topologyKey": "cpu-capability.avx2",
                }
            ]
        })

        errors = manifest_errors(Path("topology.k8s.yaml"), [document])

        assert len(errors) == 1
        assert "allowed only in nodeSelector" in errors[0]

    def test_cpu_capability_cannot_combine_with_accelerators(self) -> None:
        for resource in ("google.com/tpu", "nvidia.com/gpu"):
            with self.subTest(resource=resource):
                node_selector = {"cpu-capability.avx2": "true"}
                if resource == "nvidia.com/gpu":
                    node_selector["gpu-class"] = "l4"
                else:
                    node_selector["tpu-class"] = "v5e-2x2"
                amount = "1" if resource == "nvidia.com/gpu" else "4"
                pod_spec: dict[str, object] = {
                    "containers": [
                        {
                            "name": "worker",
                            "resources": {"limits": {resource: amount}},
                        }
                    ],
                    "nodeSelector": node_selector,
                }
                document = self.deployment(pod_spec)

                errors = manifest_errors(Path("accelerator.k8s.yaml"), [document])

                assert len(errors) == 1
                assert "cannot be combined" in errors[0]


class WorkloadPortabilityGpuTest(BaseWorkloadPortabilityTest):
    @staticmethod
    def test_gpu_classes_and_maxima_come_from_the_catalog() -> None:
        assert "l4" in gpu_class_ids()
        assert gpu_class_max_counts()["l4"] == L4_MAX_GPU_COUNT
        assert "a10" not in gpu_class_max_counts()

    def test_gpu_class_accepts_limit_only_or_matching_request(self) -> None:
        resources = (
            {"limits": {"nvidia.com/gpu": "1"}},
            {
                "limits": {"nvidia.com/gpu": "2"},
                "requests": {"nvidia.com/gpu": "2"},
            },
        )

        for resource_spec in resources:
            with self.subTest(resources=resource_spec):
                document = self.deployment({
                    "containers": [{"name": "worker", "resources": resource_spec}],
                    "nodeSelector": {"gpu-class": "l4"},
                })

                assert manifest_errors(Path("gpu.k8s.yaml"), [document]) == []

    def test_unknown_or_unavailable_gpu_class_is_rejected(self) -> None:
        cases = (
            ("future-gpu", "must name a model declared by the GPU catalog"),
            ("a10", "has no admitted provider capacity shape"),
        )

        for class_id, reason in cases:
            with self.subTest(class_id=class_id):
                document = self.deployment({
                    "containers": [
                        {
                            "name": "worker",
                            "resources": {"limits": {"nvidia.com/gpu": "1"}},
                        }
                    ],
                    "nodeSelector": {"gpu-class": class_id},
                })

                errors = manifest_errors(Path("gpu.k8s.yaml"), [document])

                assert len(errors) == 1
                assert reason in errors[0]

    def test_gpu_class_is_allowed_only_in_node_selector(self) -> None:
        expression = {
            "key": "gpu-class",
            "operator": "In",
            "values": ["l4"],
        }
        document = self.deployment({
            "affinity": {
                "nodeAffinity": {
                    "requiredDuringSchedulingIgnoredDuringExecution": {
                        "nodeSelectorTerms": [{"matchExpressions": [expression]}]
                    }
                }
            },
            "topologySpreadConstraints": [{"maxSkew": 1, "topologyKey": "gpu-class"}],
        })

        errors = manifest_errors(Path("gpu-placement.k8s.yaml"), [document])

        assert len(errors) == DUAL_PLACEMENT_VIOLATION_COUNT
        assert all("allowed only in nodeSelector" in error for error in errors)

    def test_gpu_class_and_native_resource_must_be_paired(self) -> None:
        documents = (
            self.deployment({"nodeSelector": {"gpu-class": "l4"}}),
            self.deployment({
                "containers": [
                    {
                        "name": "worker",
                        "resources": {"limits": {"nvidia.com/gpu": "1"}},
                    }
                ]
            }),
        )

        for document in documents:
            with self.subTest(document=document):
                errors = manifest_errors(Path("gpu-pair.k8s.yaml"), [document])

                assert len(errors) == 1
                assert "require" in errors[0]

    def test_gpu_request_and_limit_are_validated(self) -> None:
        cases = (
            (
                {"requests": {"nvidia.com/gpu": "1"}},
                "require a positive limit",
            ),
            (
                {"limits": {"nvidia.com/gpu": "0"}},
                "must be a positive whole number",
            ),
            (
                {"limits": {"nvidia.com/gpu": "0.5"}},
                "must be a positive whole number",
            ),
            (
                {
                    "limits": {"nvidia.com/gpu": "2"},
                    "requests": {"nvidia.com/gpu": "1"},
                },
                "request and limit must be equal",
            ),
        )

        for resources, reason in cases:
            with self.subTest(resources=resources):
                document = self.deployment({
                    "containers": [{"name": "worker", "resources": resources}],
                    "nodeSelector": {"gpu-class": "l4"},
                })

                errors = manifest_errors(Path("gpu-quantity.k8s.yaml"), [document])

                assert len(errors) == 1
                assert reason in errors[0]

    def test_gpu_count_uses_the_selected_class_maximum(self) -> None:
        document = self.deployment({
            "containers": [
                {
                    "name": "worker-a",
                    "resources": {"limits": {"nvidia.com/gpu": "5"}},
                },
                {
                    "name": "worker-b",
                    "resources": {"limits": {"nvidia.com/gpu": "4"}},
                },
            ],
            "nodeSelector": {"gpu-class": "l4"},
        })

        errors = manifest_errors(Path("gpu-max.k8s.yaml"), [document])

        assert len(errors) == 1
        assert "class 'l4' has a catalog maximum of 8" in errors[0]

    def test_gpu_count_uses_kubernetes_init_container_semantics(self) -> None:
        cases = (
            (("1", "1", "4"), False),
            (("3", "3", "4"), True),
        )

        for counts, rejected in cases:
            with self.subTest(counts=counts):
                first, second, init = counts
                document = self.deployment({
                    "containers": [
                        {
                            "name": "worker-a",
                            "resources": {"limits": {"nvidia.com/gpu": first}},
                        },
                        {
                            "name": "worker-b",
                            "resources": {"limits": {"nvidia.com/gpu": second}},
                        },
                    ],
                    "initContainers": [
                        {
                            "name": "init",
                            "resources": {"limits": {"nvidia.com/gpu": init}},
                        }
                    ],
                    "nodeSelector": {"gpu-class": "l4"},
                })

                violations = gpu_class_violations(
                    document,
                    known_classes=frozenset({"l4"}),
                    max_counts={"l4": 4},
                )

                assert len(violations) == int(rejected)
                if rejected:
                    assert "catalog maximum of 4" in violations[0].reason

    def test_restartable_gpu_init_container_is_rejected(self) -> None:
        document = self.deployment({
            "initContainers": [
                {
                    "name": "sidecar",
                    "resources": {"limits": {"nvidia.com/gpu": "1"}},
                    "restartPolicy": "Always",
                }
            ],
            "nodeSelector": {"gpu-class": "l4"},
        })

        errors = manifest_errors(Path("gpu-sidecar.k8s.yaml"), [document])

        assert len(errors) == 1
        assert "restartable init containers cannot request NVIDIA GPUs" in errors[0]

    @staticmethod
    def test_nested_ray_gpu_request_requires_a_class() -> None:
        document = {
            "apiVersion": "ray.io/v1",
            "kind": "RayJob",
            "spec": {
                "rayClusterSpec": {
                    "workerGroupSpecs": [
                        {
                            "template": {
                                "spec": {
                                    "containers": [
                                        {
                                            "name": "ray-worker",
                                            "resources": {"limits": {"nvidia.com/gpu": "1"}},
                                        }
                                    ]
                                }
                            }
                        }
                    ]
                }
            },
        }

        errors = manifest_errors(Path("ray-gpu.k8s.yaml"), [document])

        assert len(errors) == 1
        assert "workerGroupSpecs[0].template.spec" in errors[0]
        assert "require nodeSelector 'gpu-class'" in errors[0]


class WorkloadPortabilityTpuTest(BaseWorkloadPortabilityTest):
    @staticmethod
    def test_tpu_class_count_comes_from_the_catalog() -> None:
        assert tpu_class_counts() == {"v5e-2x2": 4}

    def test_tpu_class_accepts_limit_only_or_matching_request(self) -> None:
        resources = (
            {"limits": {"google.com/tpu": "4"}},
            {
                "limits": {"google.com/tpu": "4"},
                "requests": {"google.com/tpu": "4"},
            },
        )

        for resource_spec in resources:
            with self.subTest(resources=resource_spec):
                document = self.deployment(
                    self.tpu_pod_spec(containers=[{"name": "worker", "resources": resource_spec}])
                )

                assert manifest_errors(Path("tpu.k8s.yaml"), [document]) == []

    def test_unknown_tpu_class_is_rejected(self) -> None:
        document = self.deployment(self.tpu_pod_spec(tpu_class="future-tpu"))

        errors = manifest_errors(Path("tpu.k8s.yaml"), [document])

        assert len(errors) == 1
        assert "must name a class declared by the TPU catalog" in errors[0]

    def test_tpu_class_is_allowed_only_in_node_selector(self) -> None:
        expression = {
            "key": "tpu-class",
            "operator": "In",
            "values": ["v5e-2x2"],
        }
        document = self.deployment({
            "affinity": {
                "nodeAffinity": {
                    "requiredDuringSchedulingIgnoredDuringExecution": {
                        "nodeSelectorTerms": [{"matchExpressions": [expression]}]
                    }
                }
            },
            "topologySpreadConstraints": [{"maxSkew": 1, "topologyKey": "tpu-class"}],
        })

        errors = manifest_errors(Path("tpu-placement.k8s.yaml"), [document])

        assert len(errors) == DUAL_PLACEMENT_VIOLATION_COUNT
        assert all("allowed only in nodeSelector" in error for error in errors)

    def test_tpu_class_and_native_resource_must_be_paired(self) -> None:
        class_only = self.tpu_pod_spec()
        class_only["containers"] = []
        resource_only = self.tpu_pod_spec()
        resource_only.pop("nodeSelector")

        for pod_spec in (class_only, resource_only):
            with self.subTest(pod_spec=pod_spec):
                errors = manifest_errors(Path("tpu-pair.k8s.yaml"), [self.deployment(pod_spec)])

                assert len(errors) == 1
                assert "require" in errors[0]

    def test_tpu_request_and_limit_are_validated(self) -> None:
        cases = (
            (
                {"requests": {"google.com/tpu": "4"}},
                "require a positive limit",
            ),
            (
                {"limits": {"google.com/tpu": "0"}},
                "must be a positive whole number",
            ),
            (
                {"limits": {"google.com/tpu": "0.5"}},
                "must be a positive whole number",
            ),
            (
                {
                    "limits": {"google.com/tpu": "4"},
                    "requests": {"google.com/tpu": "2"},
                },
                "request and limit must be equal",
            ),
        )

        for resources, reason in cases:
            with self.subTest(resources=resources):
                document = self.deployment(
                    self.tpu_pod_spec(containers=[{"name": "worker", "resources": resources}])
                )

                errors = manifest_errors(Path("tpu-quantity.k8s.yaml"), [document])

                assert len(errors) == 1
                assert reason in errors[0]

    def test_tpu_class_requires_the_full_slice(self) -> None:
        document = self.deployment(
            self.tpu_pod_spec(
                containers=[
                    {
                        "name": "worker",
                        "resources": {"limits": {"google.com/tpu": "2"}},
                    }
                ]
            )
        )

        errors = manifest_errors(Path("tpu-count.k8s.yaml"), [document])

        assert len(errors) == 1
        assert "requires exactly 4 chips, got 2" in errors[0]

    def test_standard_gke_tpu_class_allows_only_one_consumer(self) -> None:
        document = self.deployment(
            self.tpu_pod_spec(
                containers=[
                    {
                        "name": name,
                        "resources": {"limits": {"google.com/tpu": "4"}},
                    }
                    for name in ("worker-a", "worker-b")
                ]
            )
        )

        errors = manifest_errors(Path("tpu-consumer.k8s.yaml"), [document])

        assert len(errors) == 1
        assert "exactly one TPU-consuming container" in errors[0]

    def test_tpu_declaration_does_not_require_a_toleration(self) -> None:
        document = self.deployment(self.tpu_pod_spec())

        assert manifest_errors(Path("tpu-toleration.k8s.yaml"), [document]) == []

    def test_restartable_tpu_init_container_is_rejected(self) -> None:
        document = self.deployment(
            self.tpu_pod_spec(
                containers=[],
                init_containers=[
                    {
                        "name": "sidecar",
                        "resources": {"limits": {"google.com/tpu": "4"}},
                        "restartPolicy": "Always",
                    }
                ],
            )
        )

        errors = manifest_errors(Path("tpu-sidecar.k8s.yaml"), [document])

        assert len(errors) == 1
        assert "restartable init containers cannot request Google TPUs" in errors[0]

    def test_tpu_class_cannot_combine_with_gpu_resources(self) -> None:
        pod_spec = self.tpu_pod_spec()
        pod_spec["nodeSelector"] = {
            "gpu-class": "l4",
            "tpu-class": "v5e-2x2",
        }
        containers = pod_spec["containers"]
        assert isinstance(containers, list)
        containers.append({
            "name": "gpu-worker",
            "resources": {"limits": {"nvidia.com/gpu": "1"}},
        })
        document = self.deployment(pod_spec)

        errors = manifest_errors(Path("mixed-accelerator.k8s.yaml"), [document])

        assert len(errors) == 1
        assert "cannot be combined with positive NVIDIA GPU resources" in errors[0]

    def test_nested_ray_tpu_request_requires_a_class(self) -> None:
        pod_spec = self.tpu_pod_spec()
        pod_spec.pop("nodeSelector")
        document = {
            "apiVersion": "ray.io/v1",
            "kind": "RayJob",
            "spec": {"rayClusterSpec": {"workerGroupSpecs": [{"template": {"spec": pod_spec}}]}},
        }

        errors = manifest_errors(Path("ray-tpu.k8s.yaml"), [document])

        assert len(errors) == 1
        assert "workerGroupSpecs[0].template.spec" in errors[0]
        assert "require nodeSelector 'tpu-class'" in errors[0]


class WorkloadPortabilityNodeAndRepositoryTest(BaseWorkloadPortabilityTest):
    def test_direct_capacity_placement_is_rejected(self) -> None:
        labels = {
            # keep-sorted start
            "cloud.google.com/gke-tpu-topology": "2x2",
            "feature.node.kubernetes.io/cpu-cpuid.AVX2": "true",
            "karpenter.k8s.gcp/instance-family": "g2",
            "karpenter.sh/capacity-type": "spot",
            "karpenter.sh/nodepool": "gpu-spot",
            "node.kubernetes.io/instance-type": "m7i.2xlarge",
            "nvidia.com/cuda.driver.major": "550",
            # keep-sorted end
        }

        for label, value in labels.items():
            with self.subTest(label=label):
                document = self.deployment({"nodeSelector": {label: value}})
                errors = manifest_errors(Path("deployment.k8s.yaml"), [document])

                assert len(errors) == 1
                assert "$.spec.template.spec.nodeSelector" in errors[0]
                assert label in errors[0]

    def test_raw_node_feature_affinity_is_rejected(self) -> None:
        document = self.deployment({
            "affinity": {
                "nodeAffinity": {
                    "preferredDuringSchedulingIgnoredDuringExecution": [
                        {
                            "preference": {
                                "matchExpressions": [
                                    {
                                        "key": "feature.node.kubernetes.io/cpu-cpuid.AVX2",
                                        "operator": "In",
                                        "values": ["true"],
                                    }
                                ]
                            },
                            "weight": 1,
                        }
                    ]
                }
            }
        })

        errors = manifest_errors(Path("node-feature.k8s.yaml"), [document])

        assert len(errors) == 1
        assert "feature.node.kubernetes.io/cpu-cpuid.AVX2" in errors[0]
        assert "nodeAffinity" in errors[0]

    def test_nonempty_node_name_is_rejected(self) -> None:
        document = self.deployment({"nodeName": "worker-1"})

        errors = manifest_errors(Path("node-name.k8s.yaml"), [document])

        assert len(errors) == 1
        assert "$.spec.template.spec.nodeName" in errors[0]
        assert "direct node pinning" in errors[0]

    def test_empty_node_name_is_allowed(self) -> None:
        document = self.deployment({"nodeName": ""})

        assert manifest_errors(Path("node-name.k8s.yaml"), [document]) == []

    def test_application_and_init_container_host_ports_are_rejected(self) -> None:
        cases = (
            ("containers", "app"),
            ("initContainers", "init"),
        )

        for field, name in cases:
            with self.subTest(field=field):
                document = self.deployment({
                    field: [
                        {
                            "name": name,
                            "ports": [{"containerPort": 8080, "hostPort": 18080}],
                        }
                    ]
                })

                errors = manifest_errors(Path("host-port.k8s.yaml"), [document])

                assert len(errors) == 1
                assert f".{field}[0].ports[0].hostPort" in errors[0]
                assert "nonzero hostPort is forbidden" in errors[0]

    def test_zero_host_port_is_allowed(self) -> None:
        document = self.deployment({
            "containers": [
                {
                    "name": "app",
                    "ports": [{"containerPort": 8080, "hostPort": 0}],
                }
            ]
        })

        assert manifest_errors(Path("host-port.k8s.yaml"), [document]) == []

    def test_hostname_node_selector_and_affinity_are_rejected(self) -> None:
        pod_specs: tuple[dict[str, object], ...] = (
            {"nodeSelector": {"kubernetes.io/hostname": "worker-1"}},
            {
                "affinity": {
                    "nodeAffinity": {
                        "requiredDuringSchedulingIgnoredDuringExecution": {
                            "nodeSelectorTerms": [
                                {
                                    "matchExpressions": [
                                        {
                                            "key": "kubernetes.io/hostname",
                                            "operator": "In",
                                            "values": ["worker-1"],
                                        }
                                    ]
                                }
                            ]
                        }
                    }
                }
            },
        )

        for pod_spec in pod_specs:
            with self.subTest(pod_spec=pod_spec):
                errors = manifest_errors(Path("hostname.k8s.yaml"), [self.deployment(pod_spec)])

                assert len(errors) == 1
                assert "kubernetes.io/hostname" in errors[0]

    def test_hostname_topology_key_is_allowed(self) -> None:
        document = self.deployment({
            "topologySpreadConstraints": [{"maxSkew": 1, "topologyKey": "kubernetes.io/hostname"}]
        })

        assert manifest_errors(Path("topology.k8s.yaml"), [document]) == []

    def test_metadata_name_required_and_preferred_affinity_are_rejected(self) -> None:
        field = {"key": "metadata.name", "operator": "In", "values": ["worker-1"]}
        document = self.deployment({
            "affinity": {
                "nodeAffinity": {
                    "preferredDuringSchedulingIgnoredDuringExecution": [
                        {"preference": {"matchFields": [field]}, "weight": 1}
                    ],
                    "requiredDuringSchedulingIgnoredDuringExecution": {
                        "nodeSelectorTerms": [{"matchFields": [field]}]
                    },
                }
            }
        })

        errors = manifest_errors(Path("match-fields.k8s.yaml"), [document])

        assert len(errors) == DUAL_PLACEMENT_VIOLATION_COUNT
        assert all("metadata.name" in error for error in errors)

    @staticmethod
    def test_ray_nested_metadata_name_affinity_is_rejected() -> None:
        document = {
            "apiVersion": "ray.io/v1",
            "kind": "RayJob",
            "spec": {
                "rayClusterSpec": {
                    "workerGroupSpecs": [
                        {
                            "template": {
                                "spec": {
                                    "affinity": {
                                        "nodeAffinity": {
                                            "requiredDuringSchedulingIgnoredDuringExecution": {
                                                "nodeSelectorTerms": [
                                                    {
                                                        "matchFields": [
                                                            {
                                                                "key": "metadata.name",
                                                                "operator": "In",
                                                                "values": ["worker-1"],
                                                            }
                                                        ]
                                                    }
                                                ]
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    ]
                }
            },
        }

        errors = manifest_errors(Path("ray.k8s.yaml"), [document])

        assert len(errors) == 1
        assert "workerGroupSpecs[0].template.spec.affinity.nodeAffinity" in errors[0]
        assert "metadata.name" in errors[0]

    @staticmethod
    def test_ray_nested_node_affinity_is_rejected() -> None:
        document = {
            "apiVersion": "ray.io/v1",
            "kind": "RayJob",
            "spec": {
                "rayClusterSpec": {
                    "workerGroupSpecs": [
                        {
                            "template": {
                                "spec": {
                                    "affinity": {
                                        "nodeAffinity": {
                                            "requiredDuringSchedulingIgnoredDuringExecution": {
                                                "nodeSelectorTerms": [
                                                    {
                                                        "matchExpressions": [
                                                            {
                                                                "key": "karpenter.k8s.aws/instance-family",
                                                                "operator": "In",
                                                                "values": ["p5"],
                                                            }
                                                        ]
                                                    }
                                                ]
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    ]
                }
            },
        }

        errors = manifest_errors(Path("ray.k8s.yaml"), [document])

        assert len(errors) == 1
        assert "$.spec.rayClusterSpec.workerGroupSpecs[0].template.spec" in errors[0]
        assert "karpenter.k8s.aws/instance-family" in errors[0]

    def test_instance_type_topology_key_is_rejected(self) -> None:
        document = self.deployment({
            "topologySpreadConstraints": [
                {"maxSkew": 1, "topologyKey": "node.kubernetes.io/instance-type"}
            ]
        })

        errors = manifest_errors(Path("topology.k8s.yaml"), [document])

        assert len(errors) == 1
        assert "topologySpreadConstraints[0].topologyKey" in errors[0]

    @staticmethod
    def test_infrastructure_capacity_manifest_is_outside_workload_scope() -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "src/workloads/example/project.yaml"
            project.parent.mkdir(parents=True)
            project.write_text("delivery: submitted\n", encoding="utf-8")
            portable = project.parent / "portable.k8s.yaml"
            portable.write_text(
                """apiVersion: v1
kind: Pod
spec:
  nodeSelector: {kubernetes.io/arch: amd64}
  containers: [{name: app, image: example.invalid/app}]
""",
                encoding="utf-8",
            )
            capacity = root / "src/infra/capacity/node-pool.k8s.yaml"
            capacity.parent.mkdir(parents=True)
            capacity.write_text(
                """apiVersion: karpenter.sh/v1
kind: NodePool
spec:
  template:
    spec:
      requirements:
        - key: node.kubernetes.io/instance-type
          operator: In
          values: [m7i.2xlarge]
""",
                encoding="utf-8",
            )

            assert discover_projects(root / "src") == [project]
            assert discover_manifests(project) == [portable]
            validate_repository(root)

    @staticmethod
    def test_repository_gate_rejects_a_project_manifest() -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "src/workloads/example/project.yaml"
            project.parent.mkdir(parents=True)
            project.write_text("delivery: submitted\n", encoding="utf-8")
            (project.parent / "job.k8s.yaml").write_text(
                """apiVersion: batch/v1
kind: Job
spec:
  template:
    spec:
      nodeSelector: {karpenter.k8s.aws/instance-type: m5.large}
      containers: [{name: app, image: example.invalid/app}]
      restartPolicy: Never
""",
                encoding="utf-8",
            )

            with _assert_raises(
                WorkloadPortabilityError, match=r"karpenter\.k8s\.aws/instance-type"
            ):
                validate_repository(root)


if __name__ == "__main__":
    unittest.main()
