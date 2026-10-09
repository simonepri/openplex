"""Verify that no two Argo CD Applications able to land on the same cluster declare the same Kubernetes object."""

from __future__ import annotations

import itertools
import sys
import unittest
from pathlib import Path
from typing import Any

import yaml

COMPONENTS = "src/infra/argocd/components/"

Requirement = tuple[str, str, frozenset[str]]


def cluster_requirements(generator: dict[str, Any]) -> list[Requirement]:
    """Return every (key, operator, values) label requirement of the cluster selectors in a generator tree."""
    requirements: list[Requirement] = []
    selector = generator.get("clusters", {}).get("selector", {})
    for key, value in selector.get("matchLabels", {}).items():
        requirements.append((str(key), "In", frozenset([str(value)])))
    for expression in selector.get("matchExpressions", []):
        requirements.append((
            str(expression["key"]),
            str(expression["operator"]),
            frozenset(map(str, expression.get("values", []))),
        ))
    for child in generator.get("matrix", {}).get("generators", []):
        requirements.extend(cluster_requirements(child))
    return requirements


def can_share_cluster(*selections: list[Requirement]) -> bool:
    """Return whether some cluster's labels can satisfy all the given label requirements at once."""
    by_key: dict[str, list[tuple[str, frozenset[str]]]] = {}
    for key, operator, values in itertools.chain(*selections):
        by_key.setdefault(key, []).append((operator, values))
    for requirements in by_key.values():
        operators = {operator for operator, _ in requirements}
        if "DoesNotExist" in operators and operators & {"In", "Exists"}:
            return False
        allowed_sets = [values for operator, values in requirements if operator == "In"]
        if allowed_sets:
            excluded = set().union(
                *(values for operator, values in requirements if operator == "NotIn")
            )
            if not frozenset.intersection(*allowed_sets) - excluded:
                return False
    return True


def object_key(resource: dict[str, Any]) -> tuple[str, str, str, str]:
    metadata = resource["metadata"]
    return (
        str(resource["apiVersion"]).rpartition("/")[0],
        str(resource["kind"]),
        str(metadata.get("namespace", "")),
        str(metadata["name"]),
    )


class OwnershipTest(unittest.TestCase):
    def test_can_share_cluster_rejects_disjoint_label_requirements(self) -> None:
        cloud = [("buildbuddy.io/mode", "In", frozenset(["cloud"]))]
        for other, expected in (
            ([("buildbuddy.io/mode", "NotIn", frozenset(["cloud"]))], False),
            ([("buildbuddy.io/mode", "In", frozenset(["cloud", "community"]))], True),
            ([("buildbuddy.io/mode", "DoesNotExist", frozenset())], False),
            ([("buildbuddy.io/executors", "In", frozenset(["all"]))], True),
            ([], True),
        ):
            with self.subTest(other=other):
                self.assertIs(can_share_cluster(cloud, other), expected)

    def test_each_object_has_one_owning_application_per_cluster(self) -> None:
        renders = {
            Path(path).parent.name: list(
                filter(None, yaml.safe_load_all(Path(path).read_text(encoding="utf-8")))
            )
            for path in sys.argv[3:]
        }
        for application_set in sys.argv[1:3]:
            spec = yaml.safe_load(Path(application_set).read_text(encoding="utf-8"))["spec"]
            owners: dict[tuple[str, str, str, str], list[tuple[str, list[Requirement]]]] = {}
            missing = []
            for generator in spec["generators"]:
                selector_tree, components = generator["matrix"]["generators"]
                requirements = cluster_requirements(selector_tree)
                for element in components["list"]["elements"]:
                    path = element["path"]
                    if not path.startswith(COMPONENTS) or not path.endswith("/kustomize"):
                        continue
                    directory = path.removeprefix(COMPONENTS).removesuffix("/kustomize")
                    if directory not in renders:
                        missing.append(directory)
                        continue
                    for resource in renders[directory]:
                        owners.setdefault(object_key(resource), []).append((
                            element["component"],
                            requirements,
                        ))
            with self.subTest(application_set=Path(application_set).name):
                self.assertEqual(
                    missing, [], "add these components' :base_render to KUSTOMIZE_RENDERS"
                )
                shared = sorted(
                    f"{'/'.join(filter(None, key))}: {first} and {second}"
                    for key, declarations in owners.items()
                    for (first, first_requirements), (
                        second,
                        second_requirements,
                    ) in itertools.combinations(declarations, 2)
                    if first != second
                    and can_share_cluster(first_requirements, second_requirements)
                )
                self.assertEqual(shared, [])


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]])
