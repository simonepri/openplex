#!/usr/bin/env python3
"""Validate rendered Kubernetes PodDisruptionBudget manifests to reject zero-availability disruptions."""

from __future__ import annotations

import math
import sys
from pathlib import Path
from typing import Any

import yaml


def main() -> int:
    documents = list(yaml.safe_load_all(Path(sys.argv[1]).read_text(encoding="utf-8")))
    workloads = [document for document in documents if workload(document)]
    autoscaling_floors = {
        autoscaling_target(document): autoscaling_floor(document)
        for document in documents
        if autoscaling_target(document) is not None
    }
    errors = []
    for pdb in (document for document in documents if kind(document) == "PodDisruptionBudget"):
        selector = pdb.get("spec", {}).get("selector", {}).get("matchLabels", {})
        namespace = pdb.get("metadata", {}).get("namespace", "default")
        matches = [
            item
            for item in workloads
            if item.get("metadata", {}).get("namespace", "default") == namespace
            and selector.items()
            <= item
            .get("spec", {})
            .get("template", {})
            .get("metadata", {})
            .get("labels", {})
            .items()
        ]
        for item in matches:
            identity = (
                item.get("metadata", {}).get("namespace", "default"),
                kind(item),
                item.get("metadata", {}).get("name"),
            )
            replicas = max(
                int(item.get("spec", {}).get("replicas", 1)),
                autoscaling_floors.get(identity, 0),
            )
            minimum = min_available(pdb.get("spec", {}).get("minAvailable"), replicas)
            if minimum is not None and minimum >= replicas:
                errors.append(
                    f"{namespace}/{pdb['metadata']['name']} minAvailable {minimum} "
                    f"blocks every eviction for {kind(item)} "
                    f"{item['metadata']['name']} with {replicas} replicas"
                )
    if errors:
        for err in errors:
            sys.stderr.write(f"ERROR: {err}\n")
        return 1
    return 0


def workload(document: object) -> bool:
    return isinstance(document, dict) and kind(document) in {"Deployment", "StatefulSet"}


def kind(document: object) -> str:
    return str(document.get("kind", "")) if isinstance(document, dict) else ""


def autoscaling_target(document: object) -> tuple[str, str, str] | None:
    if not isinstance(document, dict):
        return None
    spec = document.get("spec", {})
    target = spec.get("scaleTargetRef", {})
    if kind(document) == "HorizontalPodAutoscaler":
        floor_key = "minReplicas"
    elif kind(document) == "ScaledObject":
        floor_key = "minReplicaCount"
    else:
        return None
    if floor_key not in spec or not target.get("name"):
        return None
    metadata = document.get("metadata")
    namespace = (
        str(metadata.get("namespace", "default")) if isinstance(metadata, dict) else "default"
    )
    target_kind = str(target.get("kind", "Deployment"))
    target_name = str(target["name"])
    return (
        namespace,
        target_kind,
        target_name,
    )


def autoscaling_floor(document: dict[str, Any]) -> int:
    spec = document["spec"]
    key = "minReplicas" if kind(document) == "HorizontalPodAutoscaler" else "minReplicaCount"
    return int(spec[key])


def min_available(value: object, replicas: int) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.endswith("%"):
        return math.ceil(replicas * int(value[:-1]) / 100)
    return None


if __name__ == "__main__":
    raise SystemExit(main())
