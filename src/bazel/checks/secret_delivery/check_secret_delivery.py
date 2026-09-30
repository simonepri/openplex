#!/usr/bin/env python3
"""Audit Kubernetes manifests to ensure secrets are mounted via volumes rather than inline environment variables."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import yaml

IGNORE_SECRET_ENV_ANNOTATION = "ignore-check.kube-linter.io/read-secret-from-env-var"
IGNORE_SECRET_ENV_LABEL = "ignore-check.kube-linter.io/read-secret-from-env-var"


@dataclass(frozen=True, order=True)
class SecretEnvFinding:
    """Diagnostic detail for one environment secret violation."""

    kind: str
    namespace: str
    name: str
    container: str
    variable: str
    secret_name: str
    key_or_all: str

    def format(self) -> str:
        loc = f"{self.namespace}/{self.name}" if self.namespace else self.name
        return (
            f"{self.kind} {loc}: container '{self.container}' exposes secret "
            f"'{self.secret_name}' ({self.key_or_all}) in environment variable '{self.variable}'. "
            f"Mount secrets as files or annotate with '{IGNORE_SECRET_ENV_ANNOTATION}'."
        )


@dataclass(frozen=True)
class TargetResource:
    kind: str
    namespace: str
    name: str


def mapping(value: object) -> Mapping[object, object]:
    """Return a mapping or an empty mapping for malformed optional fields."""
    if not isinstance(value, Mapping):
        return {}
    res: Mapping[object, object] = dict(value)
    return res


def _inspect_env_vars(
    target: TargetResource,
    c_name: str,
    c: Mapping[object, object],
) -> list[SecretEnvFinding]:
    findings: list[SecretEnvFinding] = []
    env_list = c.get("env")
    if not isinstance(env_list, Sequence):
        return findings
    for env in env_list:
        if not isinstance(env, Mapping):
            continue
        val_from = mapping(env.get("valueFrom"))
        secret_ref = mapping(val_from.get("secretKeyRef"))
        if secret_ref:
            findings.append(
                SecretEnvFinding(
                    kind=target.kind,
                    namespace=target.namespace,
                    name=target.name,
                    container=c_name,
                    variable=str(env.get("name", "<unnamed>")),
                    secret_name=str(secret_ref.get("name", "<unknown>")),
                    key_or_all=f"key={secret_ref.get('key', '<unknown>')}",
                )
            )
    return findings


def _inspect_env_from(
    target: TargetResource,
    c_name: str,
    c: Mapping[object, object],
) -> list[SecretEnvFinding]:
    findings: list[SecretEnvFinding] = []
    env_from_list = c.get("envFrom")
    if not isinstance(env_from_list, Sequence):
        return findings
    for env_from in env_from_list:
        if not isinstance(env_from, Mapping):
            continue
        secret_ref = mapping(env_from.get("secretRef"))
        if secret_ref:
            prefix = env_from.get("prefix", "")
            findings.append(
                SecretEnvFinding(
                    kind=target.kind,
                    namespace=target.namespace,
                    name=target.name,
                    container=c_name,
                    variable=f"<envFrom prefix='{prefix}'>",
                    secret_name=str(secret_ref.get("name", "<unknown>")),
                    key_or_all="all keys",
                )
            )
    return findings


def check_pod_spec(
    target: TargetResource,
    annotations: Mapping[object, object],
    pod_spec: Mapping[object, object],
) -> list[SecretEnvFinding]:
    """Inspect a PodSpec for Secret environment variable delivery."""
    if IGNORE_SECRET_ENV_ANNOTATION in annotations:
        return []

    pod_metadata = mapping(pod_spec.get("metadata"))
    pod_annotations = mapping(pod_metadata.get("annotations"))
    if IGNORE_SECRET_ENV_ANNOTATION in pod_annotations:
        return []

    findings: list[SecretEnvFinding] = []
    containers = []
    for container_field in ("initContainers", "containers", "ephemeralContainers"):
        c_list = pod_spec.get(container_field)
        if isinstance(c_list, Sequence):
            containers.extend(c_list)

    for c in containers:
        if not isinstance(c, Mapping):
            continue
        c_name = str(c.get("name", "<unnamed>"))
        findings.extend(_inspect_env_vars(target, c_name, c))
        findings.extend(_inspect_env_from(target, c_name, c))

    return findings


def _merge_workload_attrs(
    metadata: Mapping[object, object],
    tpl_meta: Mapping[object, object],
) -> dict[object, object]:
    attrs = dict(mapping(metadata.get("annotations")))
    attrs.update(mapping(metadata.get("labels")))
    attrs.update(mapping(tpl_meta.get("annotations")))
    attrs.update(mapping(tpl_meta.get("labels")))
    return attrs


def manifest_errors(documents: Iterable[object]) -> list[str]:
    """Validate all YAML documents in a rendered manifest."""
    errors: list[str] = []

    for doc in documents:
        if not isinstance(doc, Mapping):
            continue

        kind = str(doc.get("kind", ""))
        metadata = mapping(doc.get("metadata"))
        name = str(metadata.get("name", ""))
        namespace = str(metadata.get("namespace", ""))
        annotations = mapping(metadata.get("annotations"))
        target = TargetResource(kind=kind, namespace=namespace, name=name)

        spec = mapping(doc.get("spec"))

        if kind == "Pod":
            findings = check_pod_spec(target, annotations, spec)
            errors.extend(f.format() for f in findings)
        elif kind in {
            "Deployment",
            "DaemonSet",
            "StatefulSet",
            "Job",
            "ReplicaSet",
            "ReplicationController",
        }:
            template = mapping(spec.get("template"))
            pod_spec = mapping(template.get("spec"))
            attrs = _merge_workload_attrs(metadata, mapping(template.get("metadata")))
            findings = check_pod_spec(target, attrs, pod_spec)
            errors.extend(f.format() for f in findings)
        elif kind == "CronJob":
            job_template = mapping(spec.get("jobTemplate"))
            job_spec = mapping(job_template.get("spec"))
            pod_template = mapping(job_spec.get("template"))
            pod_spec = mapping(pod_template.get("spec"))
            attrs = _merge_workload_attrs(metadata, mapping(pod_template.get("metadata")))
            findings = check_pod_spec(target, attrs, pod_spec)
            errors.extend(f.format() for f in findings)

    return errors


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate secret delivery in Kubernetes manifests."
    )
    parser.add_argument(
        "--rendered", required=True, type=Path, help="Path to rendered manifest YAML."
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    content = args.rendered.read_text(encoding="utf-8")
    documents = yaml.safe_load_all(content)
    errors = manifest_errors(documents)
    if errors:
        for _err in errors:
            pass
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
