#!/usr/bin/env python3
"""Validate Kubernetes workload manifests to ensure node selectors and tolerations avoid cluster-specific bindings."""

from __future__ import annotations

import argparse
import json
import os
import re
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from functools import cache
from pathlib import Path

import yaml

try:
    from python.runfiles import runfiles
except ImportError:
    runfiles = None

# LINT.IfChange(infrastructure_owned_selector_keys)
INFRASTRUCTURE_PLACEMENT_LABELS = frozenset({
    # keep-sorted start
    "agentpool",
    "beta.kubernetes.io/instance-type",
    "node.kubernetes.io/instance-type",
    # keep-sorted end
})
INFRASTRUCTURE_PLACEMENT_PREFIXES = (
    # keep-sorted start
    "cloud.google.com/",
    "eks.amazonaws.com/",
    "feature.node.kubernetes.io/",
    "k8s.amazonaws.com/",
    "karpenter.k8s.aws/",
    "karpenter.k8s.gcp/",
    "karpenter.sh/",
    "nvidia.com/",
    # keep-sorted end
)
# LINT.ThenChange(//src/infra/argocd/components/kueue/kustomize/team-governance.yaml:infrastructure_owned_selector_keys)
DIRECT_NODE_PLACEMENT_LABELS = frozenset({
    # keep-sorted start
    "failure-domain.beta.kubernetes.io/region",
    "failure-domain.beta.kubernetes.io/zone",
    "kubernetes.io/hostname",
    "topology.kubernetes.io/region",
    "topology.kubernetes.io/zone",
    # keep-sorted end
})
CPU_CAPABILITY_PREFIX = "cpu-capability."
CPU_CAPABILITIES_RUNFILE = "_main/src/infra/compute/cpu-capabilities.json"
GOOGLE_TPU_RESOURCE = "google.com/tpu"
GPU_CATALOG_RUNFILE = "_main/src/infra/compute/gpu-catalog.json"
GPU_CLASS_LABEL = "gpu-class"
NVIDIA_GPU_RESOURCE = "nvidia.com/gpu"
TPU_CATALOG_RUNFILE = "_main/src/infra/compute/tpu-catalog.json"
TPU_CLASS_LABEL = "tpu-class"
IGNORED_PATH_SEGMENTS = frozenset({
    # keep-sorted start
    ".terraform",
    ".terragrunt-cache",
    ".tmp",
    "node_modules",
    # keep-sorted end
})
FIELD_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")

PathPart = str | int


class WorkloadPortabilityError(ValueError):
    """A workload manifest selects infrastructure-owned placement."""


@dataclass(frozen=True, order=True)
class AcceleratorTolerationViolation:
    """One accelerator taint toleration that bypasses portable placement."""

    location: tuple[PathPart, ...]
    reason: str


@dataclass(frozen=True, order=True)
class InfrastructureSelector:
    """One infrastructure-owned label used by workload scheduling."""

    location: tuple[PathPart, ...]
    label: str


@dataclass(frozen=True, order=True)
class CpuCapabilityViolation:
    """One invalid portable CPU capability scheduling expression."""

    location: tuple[PathPart, ...]
    label: str
    reason: str


@dataclass(frozen=True, order=True)
class GpuClassViolation:
    """One invalid portable GPU class or native resource expression."""

    location: tuple[PathPart, ...]
    reason: str


@dataclass(frozen=True)
class GpuResourceDeclaration:
    """One container's effective native GPU declaration."""

    location: tuple[PathPart, ...]
    count: int | None
    init_container: bool


@dataclass(frozen=True, order=True)
class TpuClassViolation:
    """One invalid portable TPU class or native resource expression."""

    location: tuple[PathPart, ...]
    reason: str


@dataclass(frozen=True)
class TpuResourceDeclaration:
    """One container's native TPU declaration."""

    location: tuple[PathPart, ...]
    count: int | None
    init_container: bool


PORTABLE_CPU_CAPABILITY_LABELS = frozenset({
    "cpu-capability.avx2",
})
PORTABLE_GPU_CLASS_IDS = frozenset({
    "a10",
    "a100-40gb",
    "a100-80gb",
    "a10g",
    "b200",
    "b300",
    "h100",
    "h100-nvl-94gb",
    "h200",
    "l4",
    "l40s",
    "rtx-pro-server-6000",
    "t4",
    "v100",
})
PORTABLE_GPU_MAX_COUNTS = {
    "a100-40gb": 8,
    "a100-80gb": 8,
    "a10g": 8,
    "b200": 8,
    "b300": 8,
    "h100": 8,
    "h100-nvl-94gb": 1,
    "h200": 8,
    "l4": 8,
    "l40s": 8,
    "rtx-pro-server-6000": 8,
    "t4": 8,
    "v100": 8,
}
PORTABLE_TPU_CLASS_COUNTS = {
    "v5e-2x2": 4,
}


def _capability_key(capability: object) -> str | None:
    if not isinstance(capability, Mapping):
        return None
    ns = capability.get("node_selector")
    if not isinstance(ns, Mapping):
        return None
    key = ns.get("key")
    return key if isinstance(key, str) else None


@cache
def cpu_capability_labels(
    catalog_path: Path | None = None,
) -> frozenset[str]:
    """Load the portable selector keys from the canonical CPU catalog."""

    if catalog_path is None:
        return PORTABLE_CPU_CAPABILITY_LABELS
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    capabilities = catalog["capabilities"]
    if not isinstance(capabilities, Mapping):
        raise TypeError("CPU capability catalog capabilities must be an object")
    labels = {
        key
        for capability in capabilities.values()
        if (key := _capability_key(capability)) is not None
    }
    return frozenset(labels)


def cpu_capabilities_path() -> Path:
    """Resolve the catalog from Bazel runfiles or a direct source checkout."""

    source_path = Path(__file__).resolve().parents[4] / "src/infra/compute/cpu-capabilities.json"
    if runfiles is None:
        return source_path
    runtime = runfiles.Create()
    if runtime is None:
        return source_path
    resolved = runtime.Rlocation(CPU_CAPABILITIES_RUNFILE)
    if not resolved:
        return source_path
    return Path(resolved)


@cache
def gpu_class_ids(catalog_path: Path | None = None) -> frozenset[str]:
    """Load the portable GPU class IDs from the canonical GPU catalog."""

    if catalog_path is None:
        return PORTABLE_GPU_CLASS_IDS
    models = gpu_catalog(catalog_path)["models"]
    if not isinstance(models, Mapping):
        raise TypeError("GPU catalog models must be an object")
    return frozenset(str(m) for m in models)


@cache
def gpu_class_max_counts(catalog_path: Path | None = None) -> dict[str, int]:
    """Return each GPU class's largest admitted provider per-node count."""

    if catalog_path is None:
        return dict(PORTABLE_GPU_MAX_COUNTS)
    providers = gpu_catalog(catalog_path)["providers"]
    if not isinstance(providers, Mapping):
        raise TypeError("GPU catalog providers must be an object")
    counts: dict[str, int] = {}
    for class_id, count in (
        (class_id, shape["gpu_count"])
        for provider in providers.values()
        if isinstance(provider, Mapping)
        for class_id, shape in capacity_shapes(provider).items()
        if isinstance(shape, Mapping)
        and isinstance(shape.get("gpu_count"), int)
        and not isinstance(shape.get("gpu_count"), bool)
    ):
        if not isinstance(class_id, str) or count < 1:
            raise ValueError("GPU catalog must declare positive per-node GPU counts")
        counts[class_id] = max(count, counts.get(class_id, 0))
    if not counts:
        raise ValueError("GPU catalog must declare positive per-node GPU counts")
    return counts


def gpu_catalog(catalog_path: Path | None = None) -> Mapping[str, object]:
    """Load the canonical GPU catalog."""

    path = catalog_path if catalog_path is not None else gpu_catalog_path()
    catalog = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(catalog, dict):
        raise TypeError("GPU catalog must be an object")
    return {str(k): v for k, v in catalog.items()}


def capacity_shapes(provider: Mapping[object, object]) -> Mapping[object, object]:
    """Return one provider's canonical GPU capacity shapes."""

    shapes = provider.get("capacity_shapes")
    if not isinstance(shapes, Mapping):
        raise TypeError("GPU provider capacity_shapes must be an object")
    return dict(shapes)


def gpu_catalog_path() -> Path:
    """Resolve the GPU catalog from Bazel runfiles or a source checkout."""

    source_path = Path(__file__).resolve().parents[4] / "src/infra/compute/gpu-catalog.json"
    if runfiles is None:
        return source_path
    runtime = runfiles.Create()
    if runtime is None:
        return source_path
    resolved = runtime.Rlocation(GPU_CATALOG_RUNFILE)
    if not resolved:
        return source_path
    return Path(resolved)


@cache
def tpu_class_counts(catalog_path: Path | None = None) -> dict[str, int]:
    """Load each portable TPU class and its exact per-node chip count."""

    if catalog_path is None:
        return dict(PORTABLE_TPU_CLASS_COUNTS)
    classes = tpu_catalog(catalog_path)["classes"]
    if not isinstance(classes, Mapping):
        raise TypeError("TPU catalog classes must be an object")
    counts: dict[str, int] = {}
    for class_id, definition in classes.items():
        if not isinstance(class_id, str) or not isinstance(definition, Mapping):
            raise TypeError("TPU catalog classes must be named objects")
        count = definition.get("chip_count")
        if not isinstance(count, int) or isinstance(count, bool) or count < 1:
            raise ValueError("TPU catalog classes must declare positive chip counts")
        counts[class_id] = count
    if not counts:
        raise ValueError("TPU catalog must declare at least one class")
    return counts


def tpu_catalog(catalog_path: Path | None = None) -> Mapping[str, object]:
    """Load the canonical TPU catalog."""

    path = catalog_path if catalog_path is not None else tpu_catalog_path()
    catalog = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(catalog, dict):
        raise TypeError("TPU catalog must be an object")
    return {str(k): v for k, v in catalog.items()}


def tpu_catalog_path() -> Path:
    """Resolve the TPU catalog from Bazel runfiles or a source checkout."""

    source_path = Path(__file__).resolve().parents[4] / "src/infra/compute/tpu-catalog.json"
    if runfiles is None:
        return source_path
    runtime = runfiles.Create()
    if runtime is None:
        return source_path
    resolved = runtime.Rlocation(TPU_CATALOG_RUNFILE)
    if not resolved:
        return source_path
    return Path(resolved)


def infrastructure_placement_label(label: str) -> bool:
    """Return whether a node label belongs to infrastructure placement."""

    return (
        label in DIRECT_NODE_PLACEMENT_LABELS
        or label in INFRASTRUCTURE_PLACEMENT_LABELS
        or label.startswith(INFRASTRUCTURE_PLACEMENT_PREFIXES)
    )


def accelerator_toleration_violations(
    value: object,
) -> list[AcceleratorTolerationViolation]:
    """Find wildcard, malformed, or unpaired accelerator tolerations."""

    return sorted(set(_accelerator_toleration_violations(value, ())))


def _pod_spec_toleration_violations(
    pod_spec: Mapping[object, object],
    path: tuple[PathPart, ...],
) -> Iterator[AcceleratorTolerationViolation]:
    tolerations = pod_spec.get("tolerations", [])
    if not isinstance(tolerations, Sequence) or isinstance(tolerations, (str, bytes)):
        return
    for index, toleration in enumerate(tolerations):
        if isinstance(toleration, Mapping):
            yield from _accelerator_toleration_violation(
                pod_spec,
                toleration,
                (*path, "tolerations", index),
            )


# LINT.IfChange(accelerator_toleration_contract)
def _accelerator_toleration_violations(
    value: object, path: tuple[PathPart, ...]
) -> Iterator[AcceleratorTolerationViolation]:
    if isinstance(value, Mapping):
        if _pod_spec(value):
            yield from _pod_spec_toleration_violations(value, path)
        for key, child in value.items():
            if isinstance(key, str):
                yield from _accelerator_toleration_violations(child, (*path, key))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for index, child in enumerate(value):
            yield from _accelerator_toleration_violations(child, (*path, index))


def _accelerator_toleration_violation(
    pod_spec: Mapping[object, object],
    toleration: Mapping[object, object],
    path: tuple[PathPart, ...],
) -> Iterator[AcceleratorTolerationViolation]:
    key = toleration.get("key", "")
    operator = toleration.get("operator")
    effect = toleration.get("effect", "")
    if not key and operator == "Exists" and effect in {"", "NoSchedule"}:
        yield AcceleratorTolerationViolation(
            path,
            "wildcard NoSchedule tolerations can bypass accelerator placement",
        )
        return
    if not isinstance(key, str):
        return

    accelerator_classes = {
        GOOGLE_TPU_RESOURCE: TPU_CLASS_LABEL,
        NVIDIA_GPU_RESOURCE: GPU_CLASS_LABEL,
    }
    if key not in accelerator_classes:
        return
    if not (
        operator == "Equal"
        and toleration.get("value") == "present"
        and effect == "NoSchedule"
        and "tolerationSeconds" not in toleration
    ):
        yield AcceleratorTolerationViolation(
            path,
            f"{key} toleration must use operator Equal, value 'present', effect "
            "NoSchedule, and no tolerationSeconds",
        )

    node_selector = pod_spec.get("nodeSelector")
    class_label = accelerator_classes[key]
    if not (
        isinstance(node_selector, Mapping)
        and class_label in node_selector
        and _has_positive_native_resource(pod_spec, key)
    ):
        yield AcceleratorTolerationViolation(
            path,
            f"{key} toleration requires nodeSelector {class_label!r} and a matching "
            "positive native resource",
        )


# LINT.ThenChange(//src/infra/argocd/components/kueue/kustomize/team-governance.yaml:accelerator_toleration_contract)


def infrastructure_selectors(value: object) -> list[InfrastructureSelector]:
    """Find infrastructure placement in selectors, affinity, and topology keys."""

    return sorted(set(_infrastructure_selectors(value, ())))


def direct_node_name_locations(value: object) -> list[tuple[PathPart, ...]]:
    """Find nonempty nodeName fields in nested PodSpecs."""

    return sorted(set(_direct_node_name_locations(value, ())))


def _direct_node_name_locations(
    value: object, path: tuple[PathPart, ...]
) -> Iterator[tuple[PathPart, ...]]:
    if isinstance(value, Mapping):
        node_name = value.get("nodeName")
        if "containers" in value and isinstance(node_name, str) and node_name:
            yield (*path, "nodeName")
        for key, child in value.items():
            if isinstance(key, str):
                yield from _direct_node_name_locations(child, (*path, key))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for index, child in enumerate(value):
            yield from _direct_node_name_locations(child, (*path, index))


# LINT.IfChange(team_host_port_contract)
def host_port_locations(value: object) -> list[tuple[PathPart, ...]]:
    """Find nonzero hostPort fields in nested PodSpecs."""

    return sorted(set(_host_port_locations(value, ())))


def _host_port_locations(
    value: object, path: tuple[PathPart, ...]
) -> Iterator[tuple[PathPart, ...]]:
    if isinstance(value, Mapping):
        if _pod_spec(value):
            yield from _pod_spec_host_port_locations(value, path)
        for key, child in value.items():
            if isinstance(key, str):
                yield from _host_port_locations(child, (*path, key))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for index, child in enumerate(value):
            yield from _host_port_locations(child, (*path, index))


def _pod_spec_host_port_locations(
    pod_spec: Mapping[object, object], path: tuple[PathPart, ...]
) -> Iterator[tuple[PathPart, ...]]:
    for field in ("containers", "initContainers"):
        containers = pod_spec.get(field, [])
        if not isinstance(containers, Sequence) or isinstance(containers, (str, bytes)):
            continue
        for index, container in enumerate(containers):
            if isinstance(container, Mapping):
                yield from _container_host_port_locations(container, (*path, field, index))


def _container_host_port_locations(
    container: Mapping[object, object], path: tuple[PathPart, ...]
) -> Iterator[tuple[PathPart, ...]]:
    ports = container.get("ports", [])
    if not isinstance(ports, Sequence) or isinstance(ports, (str, bytes)):
        return
    for index, port in enumerate(ports):
        if isinstance(port, Mapping) and "hostPort" in port and port["hostPort"] not in {None, 0}:
            yield (*path, "ports", index, "hostPort")


# LINT.ThenChange(//src/infra/argocd/components/kueue/kustomize/team-governance.yaml:team_host_port_contract)


def cpu_capability_violations(
    value: object,
    known_labels: frozenset[str] | None = None,
) -> list[CpuCapabilityViolation]:
    """Find unknown, malformed, or non-nodeSelector CPU capabilities."""

    labels = cpu_capability_labels() if known_labels is None else known_labels
    return sorted(set(_cpu_capability_violations(value, (), labels)))


def _cpu_capability_violations(
    value: object,
    path: tuple[PathPart, ...],
    known_labels: frozenset[str],
) -> Iterator[CpuCapabilityViolation]:
    if isinstance(value, Mapping):
        yield from _cpu_node_selector_violations(value, path, known_labels)
        yield from _cpu_node_affinity_violations(value, path, known_labels)
        yield from _cpu_topology_violations(value, path)
        if _pod_spec(value):
            yield from _cpu_accelerator_exclusivity_violations(value, path)
        for key, child in value.items():
            if isinstance(key, str):
                yield from _cpu_capability_violations(child, (*path, key), known_labels)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for index, child in enumerate(value):
            yield from _cpu_capability_violations(child, (*path, index), known_labels)


def _cpu_node_selector_violations(
    value: Mapping[object, object],
    path: tuple[PathPart, ...],
    known_labels: frozenset[str],
) -> Iterator[CpuCapabilityViolation]:
    node_selector = value.get("nodeSelector")
    if not isinstance(node_selector, Mapping):
        return
    for label, selected in node_selector.items():
        if not isinstance(label, str) or not label.startswith(CPU_CAPABILITY_PREFIX):
            continue
        location = (*path, "nodeSelector", label)
        if label not in known_labels:
            yield CpuCapabilityViolation(
                location, label, "is not declared by the CPU capability catalog"
            )
        elif selected != "true" or not isinstance(selected, str):
            yield CpuCapabilityViolation(location, label, "must use the string value 'true'")


def _cpu_node_affinity_violations(
    value: Mapping[object, object],
    path: tuple[PathPart, ...],
    known_labels: frozenset[str],
) -> Iterator[CpuCapabilityViolation]:
    node_affinity = value.get("nodeAffinity")
    if isinstance(node_affinity, (Mapping, Sequence)) and not isinstance(
        node_affinity, (str, bytes)
    ):
        yield from _cpu_affinity_violations(node_affinity, (*path, "nodeAffinity"), known_labels)


def _cpu_topology_violations(
    value: Mapping[object, object], path: tuple[PathPart, ...]
) -> Iterator[CpuCapabilityViolation]:
    constraints = value.get("topologySpreadConstraints")
    if not isinstance(constraints, Sequence) or isinstance(constraints, (str, bytes)):
        return
    for index, constraint in enumerate(constraints):
        if not isinstance(constraint, Mapping):
            continue
        label = constraint.get("topologyKey")
        if isinstance(label, str) and label.startswith(CPU_CAPABILITY_PREFIX):
            yield CpuCapabilityViolation(
                (*path, "topologySpreadConstraints", index, "topologyKey"),
                label,
                "is allowed only in nodeSelector",
            )


def _cpu_affinity_violations(
    value: Mapping[object, object] | Sequence[object],
    path: tuple[PathPart, ...],
    known_labels: frozenset[str],
) -> Iterator[CpuCapabilityViolation]:
    if isinstance(value, Mapping):
        label = value.get("key")
        if isinstance(label, str) and label.startswith(CPU_CAPABILITY_PREFIX):
            reason = "is allowed only in nodeSelector"
            if label not in known_labels:
                reason = "is not declared by the CPU capability catalog"
            yield CpuCapabilityViolation((*path, "key"), label, reason)
        items = [(k, v) for k, v in value.items() if isinstance(k, str)]
    else:
        items = list(enumerate(value))
    for segment, child in items:
        if isinstance(child, Mapping) or (
            isinstance(child, Sequence) and not isinstance(child, (str, bytes))
        ):
            yield from _cpu_affinity_violations(child, (*path, segment), known_labels)


def _cpu_accelerator_exclusivity_violations(
    pod_spec: Mapping[object, object], path: tuple[PathPart, ...]
) -> Iterator[CpuCapabilityViolation]:
    node_selector = pod_spec.get("nodeSelector")
    if not isinstance(node_selector, Mapping):
        return
    capability_labels = [
        label
        for label in node_selector
        if isinstance(label, str) and label.startswith(CPU_CAPABILITY_PREFIX)
    ]
    if not capability_labels or not _has_positive_accelerator(pod_spec):
        return
    for label in capability_labels:
        yield CpuCapabilityViolation(
            (*path, "nodeSelector", label),
            label,
            "cannot be combined with a positive GPU or TPU resource",
        )


def _container_has_accelerator(container: Mapping[object, object]) -> bool:
    resources = container.get("resources")
    if not isinstance(resources, Mapping):
        return False
    for boundary in ("requests", "limits"):
        quantities = resources.get(boundary)
        if isinstance(quantities, Mapping) and any(
            resource in quantities and _positive_whole_number(quantities[resource]) is not None
            for resource in (GOOGLE_TPU_RESOURCE, NVIDIA_GPU_RESOURCE)
        ):
            return True
    return False


def _has_positive_accelerator(pod_spec: Mapping[object, object]) -> bool:
    for field in ("containers", "initContainers"):
        containers = pod_spec.get(field, [])
        if not isinstance(containers, Sequence) or isinstance(containers, (str, bytes)):
            continue
        for container in containers:
            if isinstance(container, Mapping) and _container_has_accelerator(container):
                return True
    return False


def gpu_class_violations(
    value: object,
    known_classes: frozenset[str] | None = None,
    max_counts: Mapping[str, int] | None = None,
) -> list[GpuClassViolation]:
    """Find invalid GPU classes and native GPU resource declarations."""

    classes = gpu_class_ids() if known_classes is None else known_classes
    class_max_counts = gpu_class_max_counts() if max_counts is None else max_counts
    return sorted(set(_gpu_class_violations(value, (), classes, class_max_counts)))


def _gpu_class_violations(
    value: object,
    path: tuple[PathPart, ...],
    known_classes: frozenset[str],
    max_counts: Mapping[str, int],
) -> Iterator[GpuClassViolation]:
    if isinstance(value, Mapping):
        yield from _gpu_node_selector_violations(value, path, known_classes, max_counts)
        yield from _gpu_node_affinity_violations(value, path)
        yield from _gpu_topology_violations(value, path)
        if _pod_spec(value):
            yield from _gpu_pod_spec_violations(value, path, max_counts)
        for key, child in value.items():
            if isinstance(key, str):
                yield from _gpu_class_violations(child, (*path, key), known_classes, max_counts)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for index, child in enumerate(value):
            yield from _gpu_class_violations(child, (*path, index), known_classes, max_counts)


def _gpu_node_selector_violations(
    value: Mapping[object, object],
    path: tuple[PathPart, ...],
    known_classes: frozenset[str],
    max_counts: Mapping[str, int],
) -> Iterator[GpuClassViolation]:
    node_selector = value.get("nodeSelector")
    if not isinstance(node_selector, Mapping) or GPU_CLASS_LABEL not in node_selector:
        return
    selected = node_selector[GPU_CLASS_LABEL]
    location = (*path, "nodeSelector", GPU_CLASS_LABEL)
    if not isinstance(selected, str) or selected not in known_classes:
        yield GpuClassViolation(
            location,
            f"must name a model declared by the GPU catalog, got {selected!r}",
        )
    elif selected not in max_counts:
        yield GpuClassViolation(
            location,
            f"GPU class {selected!r} has no admitted provider capacity shape",
        )


@dataclass(frozen=True)
class _PlacementTarget[T]:
    label: str
    resource_name: str
    violation_factory: Callable[[tuple[PathPart, ...], str], T]


def _accelerator_topology_spread_violations[T](
    value: Mapping[object, object],
    path: tuple[PathPart, ...],
    target: _PlacementTarget[T],
) -> Iterator[T]:
    constraints = value.get("topologySpreadConstraints")
    if not isinstance(constraints, Sequence) or isinstance(constraints, (str, bytes)):
        return
    for index, constraint in enumerate(constraints):
        if isinstance(constraint, Mapping) and constraint.get("topologyKey") == target.label:
            yield target.violation_factory(
                (*path, "topologySpreadConstraints", index, "topologyKey"),
                f"{target.resource_name} is allowed only in nodeSelector",
            )


def _accelerator_node_affinity_key_violations[T](
    value: Mapping[object, object] | Sequence[object],
    path: tuple[PathPart, ...],
    target: _PlacementTarget[T],
) -> Iterator[T]:
    if isinstance(value, Mapping):
        if value.get("key") == target.label:
            yield target.violation_factory(
                (*path, "key"), f"{target.resource_name} is allowed only in nodeSelector"
            )
        for key, child in value.items():
            if not isinstance(key, str):
                continue
            if isinstance(child, (Mapping, Sequence)) and not isinstance(child, (str, bytes)):
                yield from _accelerator_node_affinity_key_violations(child, (*path, key), target)
    else:
        for index, child in enumerate(value):
            if isinstance(child, (Mapping, Sequence)) and not isinstance(child, (str, bytes)):
                yield from _accelerator_node_affinity_key_violations(child, (*path, index), target)


def _gpu_node_affinity_violations(
    value: Mapping[object, object], path: tuple[PathPart, ...]
) -> Iterator[GpuClassViolation]:
    node_affinity = value.get("nodeAffinity")
    if isinstance(node_affinity, (Mapping, Sequence)) and not isinstance(
        node_affinity, (str, bytes)
    ):
        target = _PlacementTarget(GPU_CLASS_LABEL, "GPU class", GpuClassViolation)
        yield from _accelerator_node_affinity_key_violations(
            node_affinity, (*path, "nodeAffinity"), target
        )


def _gpu_topology_violations(
    value: Mapping[object, object], path: tuple[PathPart, ...]
) -> Iterator[GpuClassViolation]:
    target = _PlacementTarget(GPU_CLASS_LABEL, "GPU class", GpuClassViolation)
    yield from _accelerator_topology_spread_violations(value, path, target)


@dataclass(frozen=True)
class ResourceKindSpec[V, D]:
    """Parameterizes validation for a native accelerator resource."""

    resource_name: str
    human_name: str
    violation_factory: Callable[[tuple[PathPart, ...], str], V]
    declaration_factory: Callable[[tuple[PathPart, ...], int | None, bool], D]


def _common_resource_declarations[V, D](
    pod_spec: Mapping[object, object],
    path: tuple[PathPart, ...],
    spec: ResourceKindSpec[V, D],
) -> tuple[list[V], list[D]]:
    violations: list[V] = []
    declarations: list[D] = []

    for field in ("containers", "initContainers"):
        containers = pod_spec.get(field, [])
        if not isinstance(containers, Sequence) or isinstance(containers, (str, bytes)):
            continue
        for index, container in enumerate(containers):
            if not isinstance(container, Mapping):
                continue
            declaration, decl_violations = _common_resource_declaration(
                container,
                (*path, field, index),
                spec,
                init_container=field == "initContainers",
            )
            violations.extend(decl_violations)
            if declaration is not None:
                declarations.append(declaration)
    return violations, declarations


def _validate_request_match[V, D](
    spec: ResourceKindSpec[V, D],
    request_mapping: Mapping[object, object],
    limit_count: int,
    path: tuple[PathPart, ...],
) -> tuple[tuple[PathPart, ...], str] | None:
    if spec.resource_name not in request_mapping:
        return None
    request_location = (*path, "resources", "requests", spec.resource_name)
    request_count = _positive_whole_number(request_mapping[spec.resource_name])
    if request_count is None:
        return (
            request_location,
            f"native {spec.human_name} request must be a positive whole number",
        )
    if request_count != limit_count:
        limit_location = (*path, "resources", "limits", spec.resource_name)
        return (
            limit_location,
            f"native {spec.human_name} request and limit must be equal",
        )
    return None


def _validate_resource_quantities[V, D](
    spec: ResourceKindSpec[V, D],
    request_mapping: Mapping[object, object],
    limit_mapping: Mapping[object, object],
    path: tuple[PathPart, ...],
) -> tuple[int | None, list[V]]:
    if spec.resource_name not in limit_mapping:
        request_location = (*path, "resources", "requests", spec.resource_name)
        msg = f"native {spec.human_name} requests require a positive limit"
        return None, [spec.violation_factory(request_location, msg)]

    limit_count = _positive_whole_number(limit_mapping[spec.resource_name])
    if limit_count is None:
        limit_location = (*path, "resources", "limits", spec.resource_name)
        msg = f"native {spec.human_name} limit must be a positive whole number"
        return None, [spec.violation_factory(limit_location, msg)]

    req_err = _validate_request_match(spec, request_mapping, limit_count, path)
    if req_err is not None:
        return None, [spec.violation_factory(req_err[0], req_err[1])]

    return limit_count, []


def _common_resource_declaration[V, D](
    container: Mapping[object, object],
    path: tuple[PathPart, ...],
    spec: ResourceKindSpec[V, D],
    *,
    init_container: bool,
) -> tuple[D | None, list[V]]:
    resources = container.get("resources")
    if not isinstance(resources, Mapping):
        return None, []
    requests = resources.get("requests")
    limits = resources.get("limits")
    request_mapping = requests if isinstance(requests, Mapping) else {}
    limit_mapping = limits if isinstance(limits, Mapping) else {}
    request_present = spec.resource_name in request_mapping
    limit_present = spec.resource_name in limit_mapping
    if not request_present and not limit_present:
        return None, []

    location = (
        (*path, "resources", "requests", spec.resource_name)
        if request_present
        else (*path, "resources", "limits", spec.resource_name)
    )
    count, violations = _validate_resource_quantities(spec, request_mapping, limit_mapping, path)
    declaration = spec.declaration_factory(location, count, init_container)
    plural_name = "NVIDIA GPUs" if "NVIDIA" in spec.human_name else "Google TPUs"
    if count is not None and init_container and container.get("restartPolicy") == "Always":
        violations.append(
            spec.violation_factory(
                (*path, "restartPolicy"),
                f"restartable init containers cannot request {plural_name}",
            )
        )
    return declaration, violations


def _pod_spec(value: Mapping[object, object]) -> bool:
    containers = value.get("containers")
    return isinstance(containers, Sequence) and not isinstance(containers, (str, bytes))


def _gpu_pod_spec_violations(
    pod_spec: Mapping[object, object],
    path: tuple[PathPart, ...],
    max_counts: Mapping[str, int],
) -> Iterator[GpuClassViolation]:
    node_selector = pod_spec.get("nodeSelector")
    has_class = isinstance(node_selector, Mapping) and GPU_CLASS_LABEL in node_selector
    selected_class = node_selector.get(GPU_CLASS_LABEL) if has_class else None
    class_location = (*path, "nodeSelector", GPU_CLASS_LABEL)
    resource_violations, declarations = _gpu_resource_declarations(pod_spec, path)
    yield from resource_violations

    if has_class and not declarations:
        yield GpuClassViolation(
            class_location,
            "GPU class requires a positive native NVIDIA GPU limit",
        )
    elif declarations and not has_class:
        yield GpuClassViolation(
            declarations[0].location,
            f"native NVIDIA GPU resources require nodeSelector {GPU_CLASS_LABEL!r}",
        )

    regular_count = sum(
        declaration.count
        for declaration in declarations
        if not declaration.init_container and declaration.count is not None
    )
    init_count = max(
        (
            declaration.count
            for declaration in declarations
            if declaration.init_container and declaration.count is not None
        ),
        default=0,
    )
    effective_count = max(regular_count, init_count)
    class_max = max_counts.get(selected_class) if isinstance(selected_class, str) else None
    if class_max is not None and effective_count > class_max:
        yield GpuClassViolation(
            class_location,
            f"Pod requests {effective_count} NVIDIA GPUs but class {selected_class!r} "
            f"has a catalog maximum of {class_max}",
        )


GPU_RESOURCE_SPEC = ResourceKindSpec(
    resource_name=NVIDIA_GPU_RESOURCE,
    human_name="NVIDIA GPU",
    violation_factory=GpuClassViolation,
    declaration_factory=GpuResourceDeclaration,
)


def _gpu_resource_declarations(
    pod_spec: Mapping[object, object], path: tuple[PathPart, ...]
) -> tuple[list[GpuClassViolation], list[GpuResourceDeclaration]]:
    return _common_resource_declarations(pod_spec, path, GPU_RESOURCE_SPEC)


def _gpu_resource_declaration(
    container: Mapping[object, object],
    path: tuple[PathPart, ...],
    *,
    init_container: bool,
) -> tuple[GpuResourceDeclaration | None, list[GpuClassViolation]]:
    return _common_resource_declaration(
        container,
        path,
        GPU_RESOURCE_SPEC,
        init_container=init_container,
    )


def tpu_class_violations(
    value: object,
    class_counts: Mapping[str, int] | None = None,
) -> list[TpuClassViolation]:
    """Find invalid TPU classes and native TPU resource declarations."""

    counts = tpu_class_counts() if class_counts is None else class_counts
    return sorted(set(_tpu_class_violations(value, (), counts)))


def _tpu_class_violations(
    value: object,
    path: tuple[PathPart, ...],
    class_counts: Mapping[str, int],
) -> Iterator[TpuClassViolation]:
    if isinstance(value, Mapping):
        yield from _tpu_node_selector_violations(value, path, class_counts)
        yield from _tpu_node_affinity_violations(value, path)
        yield from _tpu_topology_violations(value, path)
        if _pod_spec(value):
            yield from _tpu_pod_spec_violations(value, path, class_counts)
        for key, child in value.items():
            if isinstance(key, str):
                yield from _tpu_class_violations(child, (*path, key), class_counts)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for index, child in enumerate(value):
            yield from _tpu_class_violations(child, (*path, index), class_counts)


def _tpu_node_selector_violations(
    value: Mapping[object, object],
    path: tuple[PathPart, ...],
    class_counts: Mapping[str, int],
) -> Iterator[TpuClassViolation]:
    node_selector = value.get("nodeSelector")
    if not isinstance(node_selector, Mapping) or TPU_CLASS_LABEL not in node_selector:
        return
    selected = node_selector[TPU_CLASS_LABEL]
    if not isinstance(selected, str) or selected not in class_counts:
        yield TpuClassViolation(
            (*path, "nodeSelector", TPU_CLASS_LABEL),
            f"must name a class declared by the TPU catalog, got {selected!r}",
        )


def _tpu_node_affinity_violations(
    value: Mapping[object, object], path: tuple[PathPart, ...]
) -> Iterator[TpuClassViolation]:
    node_affinity = value.get("nodeAffinity")
    if isinstance(node_affinity, (Mapping, Sequence)) and not isinstance(
        node_affinity, (str, bytes)
    ):
        target = _PlacementTarget(TPU_CLASS_LABEL, "TPU class", TpuClassViolation)
        yield from _accelerator_node_affinity_key_violations(
            node_affinity, (*path, "nodeAffinity"), target
        )


def _tpu_topology_violations(
    value: Mapping[object, object], path: tuple[PathPart, ...]
) -> Iterator[TpuClassViolation]:
    target = _PlacementTarget(TPU_CLASS_LABEL, "TPU class", TpuClassViolation)
    yield from _accelerator_topology_spread_violations(value, path, target)


def _tpu_pod_spec_violations(
    pod_spec: Mapping[object, object],
    path: tuple[PathPart, ...],
    class_counts: Mapping[str, int],
) -> Iterator[TpuClassViolation]:
    node_selector = pod_spec.get("nodeSelector")
    has_class = isinstance(node_selector, Mapping) and TPU_CLASS_LABEL in node_selector
    selected_class = node_selector.get(TPU_CLASS_LABEL) if has_class else None
    class_location = (*path, "nodeSelector", TPU_CLASS_LABEL)
    resource_violations, declarations = _tpu_resource_declarations(pod_spec, path)
    yield from resource_violations

    if has_class and not declarations:
        yield TpuClassViolation(
            class_location,
            "TPU class requires one positive native Google TPU limit",
        )
    elif declarations and not has_class:
        yield TpuClassViolation(
            declarations[0].location,
            f"native Google TPU resources require nodeSelector {TPU_CLASS_LABEL!r}",
        )

    valid_declarations = [
        declaration for declaration in declarations if declaration.count is not None
    ]
    class_count = class_counts.get(selected_class) if isinstance(selected_class, str) else None
    if has_class and len(valid_declarations) > 1:
        yield TpuClassViolation(
            class_location,
            "Standard GKE TPU Pods must use exactly one TPU-consuming container",
        )
    elif class_count is not None and len(valid_declarations) == 1:
        requested = valid_declarations[0].count
        if requested != class_count:
            yield TpuClassViolation(
                valid_declarations[0].location,
                f"TPU class {selected_class!r} requires exactly {class_count} chips, got {requested}",
            )

    if has_class and _has_positive_native_resource(pod_spec, NVIDIA_GPU_RESOURCE):
        yield TpuClassViolation(
            class_location,
            "TPU classes cannot be combined with positive NVIDIA GPU resources",
        )


TPU_RESOURCE_SPEC = ResourceKindSpec(
    resource_name=GOOGLE_TPU_RESOURCE,
    human_name="Google TPU",
    violation_factory=TpuClassViolation,
    declaration_factory=TpuResourceDeclaration,
)


def _tpu_resource_declarations(
    pod_spec: Mapping[object, object], path: tuple[PathPart, ...]
) -> tuple[list[TpuClassViolation], list[TpuResourceDeclaration]]:
    return _common_resource_declarations(pod_spec, path, TPU_RESOURCE_SPEC)


def _tpu_resource_declaration(
    container: Mapping[object, object],
    path: tuple[PathPart, ...],
    *,
    init_container: bool,
) -> tuple[TpuResourceDeclaration | None, list[TpuClassViolation]]:
    return _common_resource_declaration(
        container,
        path,
        TPU_RESOURCE_SPEC,
        init_container=init_container,
    )


def _container_has_native_resource(container: Mapping[object, object], resource: str) -> bool:
    resources = container.get("resources")
    if not isinstance(resources, Mapping):
        return False
    for boundary in ("requests", "limits"):
        quantities = resources.get(boundary)
        if (
            isinstance(quantities, Mapping)
            and resource in quantities
            and _positive_whole_number(quantities[resource]) is not None
        ):
            return True
    return False


def _has_positive_native_resource(pod_spec: Mapping[object, object], resource: str) -> bool:
    for field in ("containers", "initContainers"):
        containers = pod_spec.get(field, [])
        if not isinstance(containers, Sequence) or isinstance(containers, (str, bytes)):
            continue
        for container in containers:
            if isinstance(container, Mapping) and _container_has_native_resource(
                container, resource
            ):
                return True
    return False


def _positive_whole_number(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str) and re.fullmatch(r"[1-9][0-9]*", value):
        return int(value)
    return None


def _infrastructure_selectors(
    value: object, path: tuple[PathPart, ...]
) -> Iterator[InfrastructureSelector]:
    if isinstance(value, Mapping):
        yield from _mapping_selectors(value, path)
        for key, child in value.items():
            if isinstance(key, str):
                yield from _infrastructure_selectors(child, (*path, key))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for index, child in enumerate(value):
            yield from _infrastructure_selectors(child, (*path, index))


def _mapping_selectors(
    value: Mapping[object, object], path: tuple[PathPart, ...]
) -> Iterator[InfrastructureSelector]:
    node_selector = value.get("nodeSelector")
    if isinstance(node_selector, Mapping):
        for label in node_selector:
            if isinstance(label, str) and infrastructure_placement_label(label):
                yield InfrastructureSelector((*path, "nodeSelector", label), label)

    node_affinity = value.get("nodeAffinity")
    if isinstance(node_affinity, (Mapping, Sequence)) and not isinstance(
        node_affinity, (str, bytes)
    ):
        yield from _affinity_selectors(node_affinity, (*path, "nodeAffinity"))

    topology_constraints = value.get("topologySpreadConstraints")
    if isinstance(topology_constraints, Sequence) and not isinstance(
        topology_constraints, (str, bytes)
    ):
        yield from _topology_selectors(topology_constraints, (*path, "topologySpreadConstraints"))


def _topology_selectors(
    constraints: Sequence[object], path: tuple[PathPart, ...]
) -> Iterator[InfrastructureSelector]:
    for index, constraint in enumerate(constraints):
        if not isinstance(constraint, Mapping):
            continue
        label = constraint.get("topologyKey")
        if (
            isinstance(label, str)
            and label not in DIRECT_NODE_PLACEMENT_LABELS
            and infrastructure_placement_label(label)
        ):
            yield InfrastructureSelector((*path, index, "topologyKey"), label)


def _affinity_selectors(
    value: Mapping[object, object] | Sequence[object], path: tuple[PathPart, ...]
) -> Iterator[InfrastructureSelector]:
    if isinstance(value, Mapping):
        yield from _match_field_selectors(value, path)
        label = value.get("key")
        if isinstance(label, str) and infrastructure_placement_label(label):
            yield InfrastructureSelector((*path, "key"), label)
        for key, child in value.items():
            if not isinstance(key, str):
                continue
            if isinstance(child, Mapping) or (
                isinstance(child, Sequence) and not isinstance(child, (str, bytes))
            ):
                yield from _affinity_selectors(child, (*path, key))
    else:
        for index, child in enumerate(value):
            if isinstance(child, Mapping) or (
                isinstance(child, Sequence) and not isinstance(child, (str, bytes))
            ):
                yield from _affinity_selectors(child, (*path, index))


def _match_field_selectors(
    value: Mapping[object, object], path: tuple[PathPart, ...]
) -> Iterator[InfrastructureSelector]:
    match_fields = value.get("matchFields")
    if not isinstance(match_fields, Sequence) or isinstance(match_fields, (str, bytes)):
        return
    for index, field in enumerate(match_fields):
        if isinstance(field, Mapping) and field.get("key") == "metadata.name":
            yield InfrastructureSelector((*path, "matchFields", index, "key"), "metadata.name")


def format_location(parts: tuple[PathPart, ...]) -> str:
    """Format a logical YAML location without depending on source layout."""

    location = "$"
    for part in parts:
        if isinstance(part, int):
            location += f"[{part}]"
        elif FIELD_NAME.fullmatch(part):
            location += f".{part}"
        else:
            location += f"[{part!r}]"
    return location


def manifest_errors(path: Path, documents: Iterable[object]) -> list[str]:
    """Return every portability error from parsed documents in one manifest."""

    errors: list[str] = []
    for document_index, document in enumerate(documents, start=1):
        for violation in accelerator_toleration_violations(document):
            errors.append(
                f"{path}: document {document_index} "
                f"{format_location(violation.location)}: {violation.reason}"
            )
        for location in direct_node_name_locations(document):
            errors.append(
                f"{path}: document {document_index} {format_location(location)}: "
                "direct node pinning through nonempty podSpec.nodeName is forbidden; "
                "select portable compute capabilities instead"
            )
        for location in host_port_locations(document):
            errors.append(
                f"{path}: document {document_index} {format_location(location)}: "
                "nonzero hostPort is forbidden in workload manifests; use a "
                "cluster Service and an approved gateway route instead"
            )
        for selector in infrastructure_selectors(document):
            errors.append(
                f"{path}: document {document_index} {format_location(selector.location)}: "
                f"infrastructure-owned placement label {selector.label!r} is forbidden in "
                "workload manifests; select portable compute capabilities and availability "
                "classes instead"
            )
        for violation in cpu_capability_violations(document):
            errors.append(
                f"{path}: document {document_index} "
                f"{format_location(violation.location)}: portable CPU capability "
                f"{violation.label!r} {violation.reason}"
            )
        for violation in gpu_class_violations(document):
            errors.append(
                f"{path}: document {document_index} "
                f"{format_location(violation.location)}: {violation.reason}"
            )
        for violation in tpu_class_violations(document):
            errors.append(
                f"{path}: document {document_index} "
                f"{format_location(violation.location)}: {violation.reason}"
            )
    return errors


def discover_projects(source_root: Path) -> list[Path]:
    """Find workload project markers beneath the source tree."""

    return sorted(
        path
        for path in source_root.rglob("project.yaml")
        if not IGNORED_PATH_SEGMENTS.intersection(path.parts)
    )


def discover_manifests(project: Path) -> list[Path]:
    """Find authored Kubernetes runtime resources inside one workload project."""

    search_root = project.parent.parent if project.parent.name == "deployment" else project.parent
    return sorted(
        path
        for path in search_root.rglob("*")
        if path.is_file()
        and (path.name.endswith(".k8s.yaml") or path.name.endswith(".k8s.yml"))
        and not IGNORED_PATH_SEGMENTS.intersection(path.parts)
    )


def validate_repository(root: Path, projects: Iterable[Path] | None = None) -> None:
    """Validate every Kubernetes manifest owned by a workload project."""

    project_paths = list(projects) if projects is not None else discover_projects(root / "src")
    manifest_paths = sorted({
        path for project in project_paths for path in discover_manifests(project)
    })
    errors: list[str] = []
    for path in manifest_paths:
        documents = yaml.safe_load_all(path.read_text(encoding="utf-8"))
        errors.extend(manifest_errors(path, documents))
    if errors:
        raise WorkloadPortabilityError("\n".join(errors))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(os.environ.get("BUILD_WORKSPACE_DIRECTORY", Path.cwd())),
        help="repository root",
    )
    args = parser.parse_args()
    try:
        validate_repository(args.root.resolve())
    except (
        json.JSONDecodeError,
        KeyError,
        OSError,
        TypeError,
        WorkloadPortabilityError,
        yaml.YAMLError,
    ):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
