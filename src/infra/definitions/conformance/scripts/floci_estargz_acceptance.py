#!/usr/bin/env python3
"""Verifies the local Floci eStargz runtime and lazy image pulls to defend against broken container startup, layer caching failures, and registry routing regressions."""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import datetime
import json
import pathlib
import re
import secrets
import signal
import subprocess
import sys
import tempfile
import tomllib
from typing import TYPE_CHECKING, Protocol, cast

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence

CANONICAL_AUTHORITY = "localhost:15100"
ACCEPTANCE_AUTHORITY = "127.0.0.1:15100"
REGISTRY_HOSTS_ROOT = "/var/lib/rancher/k3s/agent/etc/containerd/certs.d"
CANONICAL_HOSTS_PATH = f"{REGISTRY_HOSTS_ROOT}/{CANONICAL_AUTHORITY}/hosts.toml"
ACCEPTANCE_HOSTS_DIRECTORY = f"{REGISTRY_HOSTS_ROOT}/{ACCEPTANCE_AUTHORITY}"
ACCEPTANCE_HOSTS_PATH = f"{ACCEPTANCE_HOSTS_DIRECTORY}/hosts.toml"
BAKED_HOSTS_PATH = "/usr/local/share/k3s-runtime/registry/localhost-15100/hosts.toml"
RENDERED_HOSTS_ROOT = ".tmp/state/opentofu/local"
SOURCE_HOSTS_PATH = "src/third_party/k3s-io/k3s/hosts.toml"
CONTAINERD_CONFIG_PATH = "/var/lib/rancher/k3s/agent/etc/containerd/config.toml"
FLOCI_REGISTRIES_PATH = "/etc/rancher/k3s/registries.yaml"
STARGZ_SOCKET = "/run/containerd-stargz-grpc/containerd-stargz-grpc.sock"
DFDAEMON_CONFIG_PATH = "/etc/dragonfly/dfdaemon.yaml"
REMOTE_SNAPSHOT_LABEL = "containerd.io/snapshot/remote"
REMOTE_SNAPSHOT_VALUE = "remote snapshot"
REMOTE_DIGEST_LABEL = "containerd.io/snapshot/remote/stargz.digest"
DOWNLOAD_METRIC = "dragonfly_client_download_task_total"
CLIENT_SELECTOR = "app=dragonfly,component=client"
CLIENT_DAEMONSET = "dragonfly-client"
CLIENT_CONTAINER = "client"
PUBLICATION_CONTRACT_PATH = ".tmp/state/estargz-acceptance.json"
COMMAND_TIMEOUT_SECONDS = 60
PULL_TIMEOUT_SECONDS = 600
OCI_IMAGE_INDEX_MEDIA_TYPE = "application/vnd.oci.image.index.v1+json"
OCI_IMAGE_MANIFEST_MEDIA_TYPE = "application/vnd.oci.image.manifest.v1+json"
REFERENCE_PATTERN = re.compile(
    r"^(?P<authority>localhost:15100|127[.]0[.]0[.]1:15100)/"
    r"(?P<repository>[a-z0-9]+(?:[._-][a-z0-9]+)*"
    r"(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*)"
    r"@(?P<digest>sha256:[0-9a-f]{64})$"
)
DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
SOURCE_REVISION_PATTERN = re.compile(r"^[0-9a-f]{40}$")
STREAM_TAG_PATTERN = re.compile(r"^[0-9]{8}T[0-9]{6}Z_(?P<revision>[0-9a-f]{12})$")
METRIC_PATTERN = re.compile(rf"^{DOWNLOAD_METRIC}(?:\{{[^}}]*\}})?\s+([0-9]+(?:\.[0-9]+)?)$")
METRIC_SERIES_PATTERN = re.compile(rf"^{DOWNLOAD_METRIC}(?:\{{|\s)")


class AcceptanceError(RuntimeError):
    """Report a failed acceptance precondition or promised observable."""


class AcceptanceInterrupted(AcceptanceError):
    """Report a signal received while the fallback resolver was installed."""


class Runner(Protocol):
    def run(
        self,
        arguments: Sequence[str],
        *,
        input_text: str | None = None,
        check: bool = True,
        timeout_seconds: int,
    ) -> subprocess.CompletedProcess[str]: ...


class SubprocessRunner:
    def run(
        self,
        arguments: Sequence[str],
        *,
        input_text: str | None = None,
        check: bool = True,
        timeout_seconds: int,
    ) -> subprocess.CompletedProcess[str]:
        if timeout_seconds <= 0:
            raise ValueError("command timeout must be positive")
        try:
            result = subprocess.run(
                list(arguments),
                check=False,
                capture_output=True,
                input=input_text,
                text=True,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired as error:
            raise AcceptanceError(
                f"{' '.join(arguments)} exceeded its {timeout_seconds}s deadline"
            ) from error
        except OSError as error:
            raise AcceptanceError(f"cannot execute {arguments[0]}: {error}") from error
        if check and result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "command failed"
            raise AcceptanceError(f"{' '.join(arguments)}: {detail}")
        return result


@dataclasses.dataclass(frozen=True)
class LocalTopology:
    installation: str
    control: str
    cells: tuple[str, ...]
    storage_writer_cell: str

    @property
    def cluster_children(self) -> dict[str, str]:
        namespace = f"{self.installation}-local"
        return {
            self.control: f"floci-{namespace}-eks-{self.control}",
            **{cell: f"floci-{namespace}-{cell}-eks-{cell}" for cell in self.cells},
        }

    @property
    def acceptance_child(self) -> str:
        return self.cluster_children[self.storage_writer_cell]

    @property
    def registry_child(self) -> str:
        return f"floci-{self.installation}-local-ecr-registry"


@dataclasses.dataclass(frozen=True)
class ImageContract:
    reference: str
    authority: str
    target_layer: str
    layer_digests: frozenset[str]


@dataclasses.dataclass(frozen=True)
class PublishedImage:
    reference: str
    authority: str
    layer_digests: tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class PublicationContract:
    source_revision: str
    stream_tag: str
    topology: LocalTopology
    acceptance_child: str
    image_references: tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class ClientObservation:
    node: str
    pod: str
    pod_uid: str
    restart_count: int
    download_tasks: int
    task_ids: frozenset[str]


def repository_root() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parents[4]


def parse_local_topology(payload: str) -> LocalTopology:
    document = decode_object(payload, "installation local topology")
    if set(document) != {"installation", "control", "cells", "storageWriterCell"}:
        raise AcceptanceError("installation local topology has unexpected fields")
    installation = document["installation"]
    control = document["control"]
    cells_value = document["cells"]
    storage_writer_cell = document["storageWriterCell"]
    dns_label = re.compile(r"^[a-z0-9](?:[-a-z0-9]*[a-z0-9])?$")
    if not isinstance(installation, str) or not 1 <= len(installation) <= 63:
        raise AcceptanceError("installation local topology is not canonical")
    if dns_label.fullmatch(installation) is None:
        raise AcceptanceError("installation local topology is not canonical")
    if not isinstance(control, str) or not control.startswith("ctrl-"):
        raise AcceptanceError("installation local topology is not canonical")
    if not 1 <= len(control) <= 63 or dns_label.fullmatch(control) is None:
        raise AcceptanceError("installation local topology is not canonical")
    if not isinstance(cells_value, list) or not cells_value:
        raise AcceptanceError("installation local topology is not canonical")
    cells = tuple(cell for cell in cells_value if isinstance(cell, str))
    if len(cells) != len(cells_value) or any(
        not cell.startswith("cell-")
        or not 1 <= len(cell) <= 63
        or dns_label.fullmatch(cell) is None
        for cell in cells
    ):
        raise AcceptanceError("installation local topology is not canonical")
    if len(set(cells)) != len(cells) or control in cells:
        raise AcceptanceError("installation local topology is not canonical")
    if not isinstance(storage_writer_cell, str) or storage_writer_cell not in cells:
        raise AcceptanceError("installation local topology is not canonical")
    return LocalTopology(
        installation=installation,
        control=control,
        cells=cells,
        storage_writer_cell=storage_writer_cell,
    )


def load_local_topology(_root: pathlib.Path, _runner: Runner | None = None) -> LocalTopology:
    return LocalTopology(
        installation="openplex",
        control="ctrl-eaws-lh1",
        cells=("cell-eaws-lh1",),
        storage_writer_cell="cell-eaws-lh1",
    )


def decode_object(payload: str, subject: str) -> dict[str, object]:
    try:
        value = json.loads(payload)
    except json.JSONDecodeError as error:
        raise AcceptanceError(f"{subject} returned invalid JSON") from error
    if not isinstance(value, dict):
        raise AcceptanceError(f"{subject} did not return a JSON object")
    return cast("dict[str, object]", value)


def require_registry_hosts_source(rendered: str, source: str, cluster: str) -> None:
    try:
        rendered_contract = tomllib.loads(rendered)
        source_contract = tomllib.loads(source)
    except tomllib.TOMLDecodeError as error:
        raise AcceptanceError(f"{cluster} registry hosts contain invalid TOML") from error
    if rendered_contract != source_contract:
        raise AcceptanceError(f"{cluster} rendered registry hosts differ from source")


def parse_publication_contract(payload: str) -> PublicationContract:
    document = decode_object(payload, "local-up eStargz publication contract")
    expected_fields = {
        "version",
        "sourceRevision",
        "streamTag",
        "topology",
        "acceptanceChild",
        "images",
    }
    if set(document) != expected_fields or document.get("version") != 1:
        raise AcceptanceError("local-up eStargz publication contract has unexpected fields")

    source_revision = document["sourceRevision"]
    stream_tag = document["streamTag"]
    topology_value = document["topology"]
    acceptance_child = document["acceptanceChild"]
    images_value = document["images"]
    if (
        not isinstance(source_revision, str)
        or SOURCE_REVISION_PATTERN.fullmatch(source_revision) is None
        or not isinstance(stream_tag, str)
        or (stream_tag_match := STREAM_TAG_PATTERN.fullmatch(stream_tag)) is None
        or stream_tag_match.group("revision") != source_revision[:12]
        or not isinstance(topology_value, dict)
        or not isinstance(acceptance_child, str)
        or not isinstance(images_value, list)
        or len(images_value) < 3
        or not all(isinstance(reference, str) for reference in images_value)
    ):
        raise AcceptanceError("local-up eStargz publication contract is not canonical")

    topology = parse_local_topology(json.dumps(topology_value))
    image_references = tuple(images_value)
    parsed_references = [parse_image_reference(reference) for reference in image_references]
    repositories = [repository for authority, repository, _ in parsed_references]
    if (
        acceptance_child != topology.acceptance_child
        or any(authority != CANONICAL_AUTHORITY for authority, _, _ in parsed_references)
        or len(set(image_references)) != len(image_references)
        or len(set(repositories)) != len(repositories)
    ):
        raise AcceptanceError("local-up eStargz publication contract is not canonical")
    return PublicationContract(
        source_revision=source_revision,
        stream_tag=stream_tag,
        topology=topology,
        acceptance_child=acceptance_child,
        image_references=image_references,
    )


def load_publication_contract(root: pathlib.Path) -> PublicationContract:
    state_root = root / ".tmp/state"
    contract_path = root / PUBLICATION_CONTRACT_PATH
    if (
        (root / ".tmp").is_symlink()
        or state_root.is_symlink()
        or contract_path.is_symlink()
        or not contract_path.is_file()
    ):
        raise AcceptanceError(
            f"completed local-up eStargz publication contract is absent: {contract_path}"
        )
    try:
        return parse_publication_contract(contract_path.read_text())
    except OSError as error:
        raise AcceptanceError(
            f"cannot read local-up eStargz publication contract: {error}"
        ) from error


def require_publication_context(
    publication: PublicationContract,
    topology: LocalTopology,
    source_revision: str,
) -> None:
    if (
        publication.topology != topology
        or publication.acceptance_child != topology.acceptance_child
    ):
        raise AcceptanceError(
            "local-up publication topology differs from the configured local fleet"
        )
    if publication.source_revision != source_revision:
        raise AcceptanceError("local-up publication revision differs from the current checkout")


def parse_image_reference(reference: str) -> tuple[str, str, str]:
    match = REFERENCE_PATTERN.fullmatch(reference)
    if match is None:
        raise AcceptanceError(
            "image references must be digest references under localhost:15100 "
            "or the owned 127.0.0.1:15100 acceptance authority"
        )
    authority = match.group("authority")
    repository = match.group("repository")
    digest = match.group("digest")
    assert authority is not None and repository is not None and digest is not None
    return str(authority), str(repository), str(digest)


def reference_with_authority(reference: str, authority: str) -> str:
    _, repository, digest = parse_image_reference(reference)
    if authority not in {CANONICAL_AUTHORITY, ACCEPTANCE_AUTHORITY}:
        raise ValueError(f"unsupported local registry authority: {authority}")
    return f"{authority}/{repository}@{digest}"


def platform_manifest_reference(
    reference: str, manifest_payload: str, node_architecture: str
) -> str:
    authority, repository, _ = parse_image_reference(reference)
    manifest = decode_object(manifest_payload, f"manifest {reference}")
    media_type = manifest.get("mediaType")
    if media_type == OCI_IMAGE_MANIFEST_MEDIA_TYPE:
        return reference
    if media_type != OCI_IMAGE_INDEX_MEDIA_TYPE:
        raise AcceptanceError(f"{reference} is not an OCI image index or manifest")
    descriptors = manifest.get("manifests")
    if not isinstance(descriptors, list):
        raise AcceptanceError(f"{reference} has no platform manifests")
    matches: list[str] = []
    for descriptor in descriptors:
        if not isinstance(descriptor, dict):
            raise AcceptanceError(f"{reference} contains an invalid platform descriptor")
        platform = descriptor.get("platform")
        digest = descriptor.get("digest")
        if (
            descriptor.get("mediaType") == OCI_IMAGE_MANIFEST_MEDIA_TYPE
            and isinstance(platform, dict)
            and platform.get("os") == "linux"
            and platform.get("architecture") == node_architecture
            and isinstance(digest, str)
            and DIGEST_PATTERN.fullmatch(digest) is not None
        ):
            matches.append(digest)
    if len(matches) != 1:
        raise AcceptanceError(
            f"{reference} must expose exactly one linux/{node_architecture} manifest"
        )
    return f"{authority}/{repository}@{matches[0]}"


def parse_published_image(
    reference: str,
    manifest_payload: str,
    config_payload: str,
    node_architecture: str,
) -> PublishedImage:
    authority, _, _ = parse_image_reference(reference)
    manifest = decode_object(manifest_payload, f"manifest {reference}")
    if manifest.get("mediaType") != OCI_IMAGE_MANIFEST_MEDIA_TYPE or "manifests" in manifest:
        raise AcceptanceError(f"{reference} is not a single-platform OCI image manifest")
    layers = manifest.get("layers")
    if not isinstance(layers, list) or not layers:
        raise AcceptanceError(f"{reference} has no image layers")
    ranked_layers: list[tuple[int, str]] = []
    for layer in layers:
        if not isinstance(layer, dict):
            raise AcceptanceError(f"{reference} has an invalid layer descriptor")
        digest = layer.get("digest")
        size = layer.get("size")
        annotations = layer.get("annotations")
        toc_digest = (
            annotations.get("containerd.io/snapshot/stargz/toc.digest")
            if isinstance(annotations, dict)
            else None
        )
        if (
            layer.get("mediaType") != "application/vnd.oci.image.layer.v1.tar+gzip"
            or not isinstance(digest, str)
            or DIGEST_PATTERN.fullmatch(digest) is None
            or not isinstance(size, int)
            or size < 0
            or not isinstance(toc_digest, str)
            or DIGEST_PATTERN.fullmatch(toc_digest) is None
        ):
            raise AcceptanceError(f"{reference} contains a non-eStargz layer")
        ranked_layers.append((size, digest))
    if len({digest for _, digest in ranked_layers}) != len(ranked_layers):
        raise AcceptanceError(f"{reference} repeats an image layer")

    config = decode_object(config_payload, f"config {reference}")
    if config.get("os") != "linux" or config.get("architecture") != node_architecture:
        raise AcceptanceError(f"{reference} does not target linux/{node_architecture}")
    ranked_layers.sort(key=lambda layer: (-layer[0], layer[1]))
    return PublishedImage(
        reference=reference,
        authority=authority,
        layer_digests=tuple(digest for _, digest in ranked_layers),
    )


def parse_image_contract(
    reference: str,
    target_layer: str,
    manifest_payload: str,
    config_payload: str,
    node_architecture: str,
) -> ImageContract:
    if DIGEST_PATTERN.fullmatch(target_layer) is None:
        raise AcceptanceError(f"{target_layer} is not a sha256 layer digest")
    published = parse_published_image(
        reference, manifest_payload, config_payload, node_architecture
    )
    if target_layer not in published.layer_digests:
        raise AcceptanceError(f"{target_layer} is not a layer of {reference}")
    return ImageContract(
        reference=reference,
        authority=published.authority,
        target_layer=target_layer,
        layer_digests=frozenset(published.layer_digests),
    )


def require_distinct_target_layers(images: Sequence[ImageContract]) -> None:
    targets = [image.target_layer for image in images]
    if len(set(targets)) != len(targets):
        raise AcceptanceError("each routing path requires a distinct cold target layer")


def select_cold_images(
    images: Sequence[PublishedImage],
    cached_content: set[str],
    snapshot_digests: set[str],
    task_ids: frozenset[str],
) -> tuple[ImageContract, ImageContract, ImageContract]:
    selected: list[ImageContract] = []
    selected_layers: set[str] = set()
    layers_pulled_by_prior_images: set[str] = set()
    for image in images:
        target_layer = next(
            (
                digest
                for digest in image.layer_digests
                if digest not in cached_content
                and digest not in snapshot_digests
                and digest.removeprefix("sha256:") not in task_ids
                and digest not in selected_layers
                and digest not in layers_pulled_by_prior_images
            ),
            None,
        )
        if target_layer is None:
            continue
        selected.append(
            ImageContract(
                reference=image.reference,
                authority=image.authority,
                target_layer=target_layer,
                layer_digests=frozenset(image.layer_digests),
            )
        )
        selected_layers.add(target_layer)
        layers_pulled_by_prior_images.update(image.layer_digests)
        if len(selected) == 3:
            proxy, fallback, reentry = selected
            return (
                proxy,
                dataclasses.replace(
                    fallback,
                    reference=reference_with_authority(fallback.reference, ACCEPTANCE_AUTHORITY),
                    authority=ACCEPTANCE_AUTHORITY,
                ),
                dataclasses.replace(
                    reentry,
                    reference=reference_with_authority(reentry.reference, ACCEPTANCE_AUTHORITY),
                    authority=ACCEPTANCE_AUTHORITY,
                ),
            )
    raise AcceptanceError(
        "local-up published fewer than three images with distinct cold eStargz layers"
    )


def parse_download_tasks(metrics: str) -> int:
    total = 0.0
    for line in metrics.splitlines():
        match = METRIC_PATTERN.fullmatch(line)
        if match is None:
            if METRIC_SERIES_PATTERN.match(line) is not None:
                raise AcceptanceError(f"{DOWNLOAD_METRIC} is invalid")
            continue
        total += float(match.group(1))
    if not total.is_integer():
        raise AcceptanceError(f"{DOWNLOAD_METRIC} is invalid")
    return int(total)


def parse_snapshot_keys(output: str) -> frozenset[str]:
    lines = [line.split() for line in output.splitlines() if line.strip()]
    if not lines or lines[0][:3] != ["KEY", "PARENT", "KIND"]:
        raise AcceptanceError("stargz snapshot listing has an unexpected format")
    return frozenset(fields[0] for fields in lines[1:] if fields)


def parse_task_ids(output: str) -> frozenset[str]:
    return frozenset(
        str(match) for match in re.findall(r"(?<![0-9a-f])[0-9a-f]{64}(?![0-9a-f])", output)
    )


def yaml_scalar(payload: str, path: Sequence[str]) -> str | None:
    stack: list[tuple[int, str]] = []
    for line in payload.splitlines():
        if not line.strip() or line.lstrip().startswith(("#", "-")):
            continue
        match = re.fullmatch(r"( *)([A-Za-z][A-Za-z0-9]*):(?: +(.*))?", line)
        if match is None:
            continue
        indentation = len(match.group(1))
        while stack and stack[-1][0] >= indentation:
            stack.pop()
        key = match.group(2)
        value = match.group(3)
        current_path = (*tuple(item[1] for item in stack), key)
        if current_path == tuple(path) and value is not None:
            return str(value.strip().strip("\"'"))
        if value is None:
            stack.append((indentation, key))
    return None


def toml_section_scalars(payload: str, header: str | None) -> dict[str, str]:
    scalars: dict[str, str] = {}
    active = header is None
    seen = header is None
    for line in payload.splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            if active and seen:
                break
            active = stripped == header
            seen = seen or active
            continue
        if not active or not stripped or stripped.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        scalars[key.strip()] = value.strip().strip("\"'")
    if not seen:
        raise AcceptanceError(f"generated containerd config omits {header}")
    return scalars


def validate_containerd_config(payload: str) -> None:
    root = toml_section_scalars(payload, None)
    images = toml_section_scalars(payload, "[plugins.'io.containerd.cri.v1.images']")
    registry = toml_section_scalars(
        payload, "[plugins.'io.containerd.snapshotter.v1.stargz'.registry]"
    )
    if (
        root.get("version") != "3"
        or images.get("snapshotter") != "stargz"
        or images.get("disable_snapshot_annotations") != "false"
        or images.get("use_local_image_pull") != "true"
        or registry.get("config_path") != REGISTRY_HOSTS_ROOT
        or re.search(r"^\[proxy_plugins", payload, re.MULTILINE)
    ):
        raise AcceptanceError(
            "generated containerd config does not select K3s's embedded stargz resolver"
        )


def validate_k3s_process_model(pid_one_payload: str, processes_payload: str) -> None:
    arguments = [argument for argument in pid_one_payload.split("\0") if argument]
    if arguments != ["/bin/k3s init"]:
        raise AcceptanceError("PID 1 is not the K3s init supervisor")
    server_pattern = re.compile(r"^\s*[0-9]+\s+(?:\{exe\}\s+)?(?:/bin/)?k3s\s+server(?:\s|$)")
    servers = [line for line in processes_payload.splitlines() if server_pattern.match(line)]
    if len(servers) != 1:
        raise AcceptanceError("the runtime does not expose exactly one K3s server process")
    if re.search(
        r"(?:^|[ /])containerd-stargz-grpc(?:\s|$)",
        processes_payload,
        re.MULTILINE,
    ):
        raise AcceptanceError("an external stargz snapshotter process is running")


def listening_ports(payload: str) -> frozenset[int]:
    ports: set[int] = set()
    for line in payload.splitlines()[1:]:
        fields = line.split()
        if len(fields) < 4 or fields[3] != "0A" or ":" not in fields[1]:
            continue
        try:
            ports.add(int(fields[1].rsplit(":", 1)[1], 16))
        except ValueError as error:
            raise AcceptanceError("the node TCP socket table is invalid") from error
    return frozenset(ports)


def validate_node(payload: str, cluster: str) -> tuple[str, str]:
    document = decode_object(payload, f"{cluster} nodes")
    items = document.get("items")
    if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
        raise AcceptanceError(f"{cluster} must expose exactly one local node")
    node = items[0]
    metadata = node.get("metadata")
    spec = node.get("spec")
    status = node.get("status")
    if not isinstance(metadata, dict) or not isinstance(spec, dict) or not isinstance(status, dict):
        raise AcceptanceError(f"{cluster} returned an invalid Node")
    labels = metadata.get("labels")
    taints = spec.get("taints", [])
    conditions = status.get("conditions")
    if (
        not isinstance(labels, dict)
        or not isinstance(taints, list)
        or not isinstance(conditions, list)
    ):
        raise AcceptanceError(f"{cluster} returned an invalid Node contract")
    if any("stargz-runtime" in str(key) for key in labels) or any(
        isinstance(taint, dict) and "stargz-runtime" in str(taint.get("key", ""))
        for taint in taints
    ):
        raise AcceptanceError(f"{cluster} exposes redundant stargz runtime metadata")
    if not any(
        isinstance(condition, dict)
        and condition.get("type") == "Ready"
        and condition.get("status") == "True"
        for condition in conditions
    ):
        raise AcceptanceError(f"{cluster} node is not Ready")
    name = metadata.get("name")
    architecture = labels.get("kubernetes.io/arch")
    if (
        not isinstance(name, str)
        or not isinstance(architecture, str)
        or architecture not in {"amd64", "arm64"}
    ):
        raise AcceptanceError(f"{cluster} returned an invalid node name or architecture")
    return name, architecture


def validate_daemonset(payload: str, cluster: str) -> None:
    daemonset = decode_object(payload, f"{cluster} Dragonfly client")
    spec = daemonset.get("spec")
    status = daemonset.get("status")
    if not isinstance(spec, dict) or not isinstance(status, dict):
        raise AcceptanceError(f"{cluster} returned an invalid Dragonfly DaemonSet")
    template = spec.get("template")
    if not isinstance(template, dict) or not isinstance(template.get("spec"), dict):
        raise AcceptanceError(f"{cluster} returned an invalid Dragonfly pod template")
    node_selector = template["spec"].get("nodeSelector")
    desired = status.get("desiredNumberScheduled")
    ready = status.get("numberReady")
    if (
        not isinstance(node_selector, dict)
        or node_selector != {"kubernetes.io/os": "linux"}
        or not isinstance(desired, int)
        or desired < 1
        or ready != desired
    ):
        raise AcceptanceError(f"{cluster} Dragonfly client is not ready on embedded stargz")


def require_no_client_daemonset(payload: str, cluster: str) -> None:
    document = decode_object(payload, f"{cluster} Dragonfly DaemonSets")
    items = document.get("items")
    if not isinstance(items, list):
        raise AcceptanceError(f"{cluster} returned an invalid Dragonfly DaemonSet list")
    names: list[str] = []
    for item in items:
        metadata = item.get("metadata") if isinstance(item, dict) else None
        name = metadata.get("name") if isinstance(metadata, dict) else None
        if not isinstance(name, str):
            raise AcceptanceError(f"{cluster} returned an invalid Dragonfly DaemonSet list")
        names.append(name)
    if CLIENT_DAEMONSET in names:
        raise AcceptanceError(f"{cluster} must not run the cell-only Dragonfly client")


def parse_client(payload: str, node: str) -> tuple[str, str, int]:
    document = decode_object(payload, f"Dragonfly client on {node}")
    items = document.get("items")
    if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
        raise AcceptanceError(f"expected exactly one Dragonfly client on {node}")
    pod = items[0]
    metadata = pod.get("metadata")
    status = pod.get("status")
    if not isinstance(metadata, dict) or not isinstance(status, dict):
        raise AcceptanceError(f"Dragonfly returned an invalid client pod on {node}")
    conditions = status.get("conditions")
    containers = status.get("containerStatuses")
    if not isinstance(conditions, list) or not isinstance(containers, list):
        raise AcceptanceError(f"Dragonfly returned an incomplete client status on {node}")
    if not any(
        isinstance(condition, dict)
        and condition.get("type") == "Ready"
        and condition.get("status") == "True"
        for condition in conditions
    ):
        raise AcceptanceError(f"Dragonfly client is not Ready on {node}")
    clients = [
        container
        for container in containers
        if isinstance(container, dict) and container.get("name") == CLIENT_CONTAINER
    ]
    name = metadata.get("name")
    uid = metadata.get("uid")
    restart_count = clients[0].get("restartCount") if len(clients) == 1 else None
    if not isinstance(name, str) or not isinstance(uid, str) or not isinstance(restart_count, int):
        raise AcceptanceError(f"Dragonfly returned an invalid client identity on {node}")
    return name, uid, restart_count


def snapshot_target_digest(payload: str) -> str | None:
    snapshot = decode_object(payload, "stargz snapshot")
    labels = snapshot.get("Labels")
    if not isinstance(labels, dict):
        labels = snapshot.get("labels")
    if not isinstance(labels, dict) or labels.get(REMOTE_SNAPSHOT_LABEL) != REMOTE_SNAPSHOT_VALUE:
        return None
    digest = labels.get(REMOTE_DIGEST_LABEL)
    return digest if isinstance(digest, str) else None


@contextlib.contextmanager
def temporary_fallback_route(
    install: Callable[[str], None], normal: str, fallback: str
) -> Iterator[None]:
    install(normal)
    previous_handlers = {
        signum: signal.getsignal(signum) for signum in (signal.SIGINT, signal.SIGTERM)
    }

    def interrupt(signum: int, _frame: object) -> None:
        raise AcceptanceInterrupted(f"received signal {signum}")

    for signum in previous_handlers:
        signal.signal(signum, interrupt)
    try:
        install(fallback)
        yield
    finally:
        for signum in previous_handlers:
            signal.signal(signum, signal.SIG_IGN)
        try:
            install(normal)
        finally:
            for signum, handler in previous_handlers.items():
                signal.signal(signum, handler)


class Acceptance:
    def __init__(self, runner: Runner, root: pathlib.Path, topology: LocalTopology) -> None:
        self.runner = runner
        self.root = root
        self.topology = topology
        self.acceptance_cell = topology.storage_writer_cell
        self.acceptance_child = topology.acceptance_child
        self.node_names: dict[str, str] = {}
        self.node_architectures: dict[str, str] = {}
        self.clients: dict[str, ClientObservation] = {}

    def command(
        self,
        arguments: Sequence[str],
        *,
        input_text: str | None = None,
        check: bool = True,
        timeout_seconds: int = COMMAND_TIMEOUT_SECONDS,
    ) -> subprocess.CompletedProcess[str]:
        return self.runner.run(
            arguments,
            input_text=input_text,
            check=check,
            timeout_seconds=timeout_seconds,
        )

    def docker(
        self,
        arguments: Sequence[str],
        *,
        check: bool = True,
        timeout_seconds: int = COMMAND_TIMEOUT_SECONDS,
    ) -> subprocess.CompletedProcess[str]:
        return self.command(
            ["docker", "--context", "colima", *arguments],
            check=check,
            timeout_seconds=timeout_seconds,
        )

    def kubectl(
        self,
        cluster: str,
        arguments: Sequence[str],
        *,
        check: bool = True,
        timeout_seconds: int = COMMAND_TIMEOUT_SECONDS,
    ) -> subprocess.CompletedProcess[str]:
        kubeconfig = self.root / ".tmp/kubeconfigs" / f"{cluster}.yaml"
        if not kubeconfig.is_file():
            raise AcceptanceError(f"local kubeconfig is absent: {kubeconfig}")
        return self.command(
            ["kubectl", "--kubeconfig", str(kubeconfig), *arguments],
            check=check,
            timeout_seconds=timeout_seconds,
        )

    def inspect(self) -> None:
        for cluster, child in self.topology.cluster_children.items():
            self.inspect_child(child, self.rendered_hosts(cluster))
            node_payload = self.kubectl(cluster, ["get", "nodes", "-o", "json"]).stdout
            node, architecture = validate_node(node_payload, cluster)
            self.node_names[cluster] = node
            self.node_architectures[cluster] = architecture

        control_daemonsets = self.kubectl(
            self.topology.control,
            ["-n", "dragonfly-system", "get", "daemonsets", "-o", "json"],
        ).stdout
        require_no_client_daemonset(control_daemonsets, self.topology.control)

        for cluster in self.topology.cells:
            daemonset = self.kubectl(
                cluster,
                [
                    "-n",
                    "dragonfly-system",
                    "get",
                    "daemonset",
                    CLIENT_DAEMONSET,
                    "-o",
                    "json",
                ],
            ).stdout
            validate_daemonset(daemonset, cluster)
            self.clients[cluster] = self.observe_client(cluster)

    def inspect_child(self, child: str, canonical_hosts: str) -> None:
        payload = self.docker(["inspect", child]).stdout
        try:
            documents = json.loads(payload)
        except json.JSONDecodeError as error:
            raise AcceptanceError(f"docker inspect returned invalid JSON for {child}") from error
        if (
            not isinstance(documents, list)
            or len(documents) != 1
            or not isinstance(documents[0], dict)
        ):
            raise AcceptanceError(f"docker inspect did not resolve exactly one {child}")
        document = documents[0]
        state = document.get("State")
        host_config = document.get("HostConfig")
        mounts = document.get("Mounts")
        restart_policy = host_config.get("RestartPolicy") if isinstance(host_config, dict) else None
        if (
            document.get("Name") != f"/{child}"
            or not isinstance(document.get("Id"), str)
            or not isinstance(document.get("Image"), str)
            or not isinstance(state, dict)
            or state.get("Running") is not True
            or not isinstance(restart_policy, dict)
            or restart_policy.get("Name") != "unless-stopped"
            or not isinstance(mounts, list)
        ):
            raise AcceptanceError(f"{child} is not the retained running Floci child")
        retained_mounts = [
            mount
            for mount in mounts
            if isinstance(mount, dict)
            and mount.get("Type") == "volume"
            and mount.get("Name") == child
            and mount.get("Destination") == "/var/lib/rancher/k3s"
            and mount.get("RW") is True
        ]
        if len(retained_mounts) != 1:
            raise AcceptanceError(f"{child} does not use its exact retained k3s volume")

        validate_k3s_process_model(
            self.docker(["exec", child, "cat", "/proc/1/cmdline"]).stdout,
            self.docker(["top", child, "-eo", "pid,args"]).stdout,
        )
        validate_containerd_config(
            self.docker(["exec", child, "cat", CONTAINERD_CONFIG_PATH]).stdout
        )
        self.docker(["exec", child, "test", "-S", STARGZ_SOCKET])
        plugin_output = self.docker(["exec", child, "ctr", "plugins", "ls"]).stdout
        if not any(
            fields[:2] == ["io.containerd.snapshotter.v1", "stargz"] and "ok" in fields[2:]
            for fields in (line.split() for line in plugin_output.splitlines())
        ):
            raise AcceptanceError(f"{child} does not report a healthy embedded stargz plugin")
        self.docker(["exec", child, "test", "!", "-e", FLOCI_REGISTRIES_PATH])
        baked_hosts = self.docker(["exec", child, "cat", BAKED_HOSTS_PATH]).stdout
        if baked_hosts != canonical_hosts:
            raise AcceptanceError(f"{child} baked registry hosts differ from source")
        for path in (CANONICAL_HOSTS_PATH, ACCEPTANCE_HOSTS_PATH):
            self.require_owned_hosts_path(child, path)
            mode = self.docker(["exec", child, "stat", "-c", "%a", path]).stdout.strip()
            installed_hosts = self.docker(["exec", child, "cat", path]).stdout
            if mode != "444" or installed_hosts != canonical_hosts:
                raise AcceptanceError(
                    f"{child} registry hosts at {path} differ from the owned source"
                )

    def require_owned_hosts_path(self, child: str, path: str) -> None:
        directory = str(pathlib.PurePosixPath(path).parent)
        for arguments in (
            ["exec", child, "test", "-d", directory],
            ["exec", child, "test", "!", "-L", directory],
            ["exec", child, "test", "-f", path],
            ["exec", child, "test", "!", "-L", path],
        ):
            self.docker(arguments)

    def observe_client(self, cluster: str) -> ClientObservation:
        node = self.node_names.get(cluster)
        if node is None:
            node_payload = self.kubectl(cluster, ["get", "nodes", "-o", "json"]).stdout
            node, architecture = validate_node(node_payload, cluster)
            self.node_names[cluster] = node
            self.node_architectures[cluster] = architecture
        client_payload = self.kubectl(
            cluster,
            [
                "-n",
                "dragonfly-system",
                "get",
                "pods",
                "-l",
                CLIENT_SELECTOR,
                "--field-selector",
                f"spec.nodeName={node}",
                "-o",
                "json",
            ],
        ).stdout
        pod, pod_uid, restart_count = parse_client(client_payload, node)
        dfdaemon_config = self.kubectl(
            cluster,
            [
                "-n",
                "dragonfly-system",
                "exec",
                pod,
                "--container",
                CLIENT_CONTAINER,
                "--",
                "cat",
                DFDAEMON_CONFIG_PATH,
            ],
        ).stdout
        if (
            yaml_scalar(
                dfdaemon_config,
                ("proxy", "registryMirror", "enableTaskIDBasedBlobDigest"),
            )
            != "true"
        ):
            raise AcceptanceError(f"{cluster} dfdaemon does not use blob digests as OCI task IDs")
        metrics = self.kubectl(
            cluster,
            [
                "get",
                "--raw",
                f"/api/v1/namespaces/dragonfly-system/pods/{pod}:4002/proxy/metrics",
            ],
        ).stdout
        tasks = self.kubectl(
            cluster,
            [
                "-n",
                "dragonfly-system",
                "exec",
                pod,
                "--container",
                CLIENT_CONTAINER,
                "--",
                "dfctl",
                "task",
                "ls",
            ],
        ).stdout
        return ClientObservation(
            node=node,
            pod=pod,
            pod_uid=pod_uid,
            restart_count=restart_count,
            download_tasks=parse_download_tasks(metrics),
            task_ids=parse_task_ids(tasks),
        )

    def load_published_image(self, reference: str) -> PublishedImage:
        architecture = self.node_architectures[self.acceptance_cell]
        published_manifest = self.command([
            "oras",
            "manifest",
            "fetch",
            "--plain-http",
            reference,
        ]).stdout
        platform_reference = platform_manifest_reference(
            reference, published_manifest, architecture
        )
        manifest = (
            published_manifest
            if platform_reference == reference
            else self.command([
                "oras",
                "manifest",
                "fetch",
                "--plain-http",
                platform_reference,
            ]).stdout
        )
        config = self.command([
            "oras",
            "manifest",
            "fetch-config",
            "--plain-http",
            platform_reference,
        ]).stdout
        return parse_published_image(platform_reference, manifest, config, architecture)

    def snapshot_keys(self) -> frozenset[str]:
        output = self.docker([
            "exec",
            self.acceptance_child,
            "ctr",
            "--namespace",
            "k8s.io",
            "snapshots",
            "--snapshotter",
            "stargz",
            "ls",
        ]).stdout
        return parse_snapshot_keys(output)

    def snapshot_digest(self, key: str) -> str | None:
        payload = self.docker([
            "exec",
            self.acceptance_child,
            "ctr",
            "--namespace",
            "k8s.io",
            "snapshots",
            "--snapshotter",
            "stargz",
            "info",
            key,
        ]).stdout
        return snapshot_target_digest(payload)

    def require_cold_targets(
        self, images: Sequence[ImageContract], client: ClientObservation
    ) -> None:
        cached_content, snapshot_digests = self.cached_target_state()
        for image in images:
            task_id = image.target_layer.removeprefix("sha256:")
            if (
                image.target_layer in cached_content
                or image.target_layer in snapshot_digests
                or task_id in client.task_ids
            ):
                raise AcceptanceError(
                    f"target layer {image.target_layer} is not cold for {image.reference}"
                )

    def cached_target_state(self) -> tuple[set[str], set[str]]:
        content_output = self.docker([
            "exec",
            self.acceptance_child,
            "ctr",
            "--namespace",
            "k8s.io",
            "content",
            "ls",
            "--quiet",
        ]).stdout
        cached_content = set(content_output.splitlines())
        snapshot_digests = {
            digest
            for key in self.snapshot_keys()
            if (digest := self.snapshot_digest(key)) is not None
        }
        return cached_content, snapshot_digests

    def discover_cold_images(
        self,
        publication: PublicationContract,
        client: ClientObservation,
    ) -> tuple[ImageContract, ImageContract, ImageContract]:
        published_images = [
            self.load_published_image(reference) for reference in publication.image_references
        ]
        cached_content, snapshot_digests = self.cached_target_state()
        return select_cold_images(
            published_images,
            cached_content,
            snapshot_digests,
            client.task_ids,
        )

    def require_new_remote_snapshot(
        self,
        before: frozenset[str],
        image: ImageContract,
    ) -> None:
        for key in self.snapshot_keys() - before:
            if self.snapshot_digest(key) == image.target_layer:
                return
        raise AcceptanceError(
            f"{image.reference} did not create a remote snapshot for {image.target_layer}"
        )

    @staticmethod
    def require_same_client(before: ClientObservation, after: ClientObservation) -> None:
        if (
            before.node != after.node
            or before.pod_uid != after.pod_uid
            or before.restart_count != after.restart_count
        ):
            raise AcceptanceError("Dragonfly client changed during the pull observation")

    def pull(self, image: ImageContract, *, route: str, expected_client_uid: str) -> None:
        before_client = self.observe_client(self.acceptance_cell)
        if before_client.pod_uid != expected_client_uid:
            raise AcceptanceError("Dragonfly client UID differs from the operator's inspected UID")
        before_snapshots = self.snapshot_keys()
        started_at = (
            datetime.datetime
            .now(datetime.UTC)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )
        self.docker(
            ["exec", self.acceptance_child, "crictl", "pull", image.reference],
            timeout_seconds=PULL_TIMEOUT_SECONDS,
        )
        self.require_new_remote_snapshot(before_snapshots, image)
        after_client = self.observe_client(self.acceptance_cell)
        self.require_same_client(before_client, after_client)
        target_task = image.target_layer.removeprefix("sha256:")

        if route == "dragonfly":
            if (
                after_client.download_tasks <= before_client.download_tasks
                or target_task not in after_client.task_ids
            ):
                raise AcceptanceError(f"Dragonfly did not serve target layer {image.target_layer}")
            return
        if route != "origin":
            raise AssertionError(f"unsupported route {route}")
        if (
            after_client.download_tasks != before_client.download_tasks
            or target_task in after_client.task_ids
        ):
            raise AcceptanceError("Dragonfly handled a task during the direct-origin observation")
        registry_logs = self.docker(["logs", "--since", started_at, self.topology.registry_child])
        logs = f"{registry_logs.stdout}\n{registry_logs.stderr}"
        if f"/blobs/{image.target_layer}" not in logs and image.target_layer not in logs:
            raise AcceptanceError(f"origin logs did not identify target layer {image.target_layer}")

    def install_acceptance_hosts(self, contents: str) -> None:
        self.require_owned_hosts_path(self.acceptance_child, ACCEPTANCE_HOSTS_PATH)
        remote_temporary = f"{ACCEPTANCE_HOSTS_PATH}.acceptance.{secrets.token_hex(8)}"
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8") as local:
            local.write(contents)
            local.flush()
            self.docker(["cp", local.name, f"{self.acceptance_child}:{remote_temporary}"])
        try:
            self.docker(["exec", self.acceptance_child, "chmod", "0444", remote_temporary])
            self.require_owned_hosts_path(self.acceptance_child, ACCEPTANCE_HOSTS_PATH)
            self.docker([
                "exec",
                self.acceptance_child,
                "mv",
                "-f",
                "--",
                remote_temporary,
                ACCEPTANCE_HOSTS_PATH,
            ])
        finally:
            self.docker(
                ["exec", self.acceptance_child, "rm", "-f", "--", remote_temporary],
                check=False,
            )
        installed = self.docker([
            "exec",
            self.acceptance_child,
            "cat",
            ACCEPTANCE_HOSTS_PATH,
        ]).stdout
        if installed != contents:
            raise AcceptanceError("the acceptance registry hosts restore did not verify")

    def require_dead_proxy_port(self) -> None:
        sockets = ""
        for path in ("/proc/net/tcp", "/proc/net/tcp6"):
            sockets += self.docker(["exec", self.acceptance_child, "cat", path]).stdout
        if 1 in listening_ports(sockets):
            raise AcceptanceError("TCP port 1 is listening on the acceptance cell")

    def rendered_hosts_path(self, cluster: str) -> pathlib.Path:
        if cluster not in self.topology.cluster_children:
            raise ValueError(f"unknown local cluster: {cluster}")
        suffix = "" if cluster == self.topology.control else f"-{cluster}"
        return self.root / RENDERED_HOSTS_ROOT / f"k3s-build{suffix}/registry-hosts.toml"

    def rendered_hosts(self, cluster: str) -> str:
        path = self.rendered_hosts_path(cluster)
        if path.is_symlink() or not path.is_file():
            raise AcceptanceError(f"rendered Floci hosts contract is absent: {path}")
        source_path = self.root / SOURCE_HOSTS_PATH
        if source_path.is_symlink() or not source_path.is_file():
            raise AcceptanceError(f"source Floci hosts contract is absent: {source_path}")
        try:
            rendered = path.read_text()
            source = source_path.read_text()
        except OSError as error:
            raise AcceptanceError(
                f"cannot read rendered Floci hosts contract {path}: {error}"
            ) from error
        require_registry_hosts_source(rendered, source, cluster)
        return rendered

    def verified_normal_hosts(self) -> str:
        normal_hosts = self.rendered_hosts(self.acceptance_cell)
        baked_hosts = self.docker(["exec", self.acceptance_child, "cat", BAKED_HOSTS_PATH]).stdout
        if normal_hosts != baked_hosts:
            raise AcceptanceError(
                "the rendered registry hosts differ from the running immutable image"
            )
        if normal_hosts.count("http://127.0.0.1:4001") != 1:
            raise AcceptanceError("canonical hosts have an unexpected proxy contract")
        return normal_hosts

    def prepare_pulls(self) -> str:
        normal_hosts = self.verified_normal_hosts()
        self.install_acceptance_hosts(normal_hosts)
        self.inspect()
        return normal_hosts

    def current_source_revision(self) -> str:
        revision = self.command([
            "git",
            "-C",
            str(self.root),
            "rev-parse",
            "--verify",
            "HEAD^{commit}",
        ]).stdout.strip()
        if SOURCE_REVISION_PATTERN.fullmatch(revision) is None:
            raise AcceptanceError("current checkout did not resolve to a Git commit")
        return revision

    def pulls(self, publication: PublicationContract) -> None:
        require_publication_context(publication, self.topology, self.current_source_revision())
        normal_hosts = self.prepare_pulls()
        initial_client = self.clients[self.acceptance_cell]
        expected_client_uid = initial_client.pod_uid
        images = self.discover_cold_images(publication, initial_client)
        if [image.authority for image in images] != [
            CANONICAL_AUTHORITY,
            ACCEPTANCE_AUTHORITY,
            ACCEPTANCE_AUTHORITY,
        ]:
            raise AcceptanceError("proxy uses localhost; fallback and re-entry use 127.0.0.1")
        require_distinct_target_layers(images)
        self.require_cold_targets(images, initial_client)

        self.pull(images[0], route="dragonfly", expected_client_uid=expected_client_uid)
        self.require_dead_proxy_port()
        fallback_hosts = normal_hosts.replace("http://127.0.0.1:4001", "http://127.0.0.1:1")
        with temporary_fallback_route(self.install_acceptance_hosts, normal_hosts, fallback_hosts):
            self.pull(images[1], route="origin", expected_client_uid=expected_client_uid)
        self.pull(images[2], route="dragonfly", expected_client_uid=expected_client_uid)


def argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="mode", required=True)
    subcommands.add_parser("inspect", help="read the embedded runtime and Dragonfly state")
    subcommands.add_parser(
        "pulls",
        help="run stateful Dragonfly, direct-origin, and normal-path re-entry pulls",
    )
    return parser


def execute(arguments: Sequence[str], runner: Runner | None = None) -> None:
    options = argument_parser().parse_args(arguments)
    root = repository_root()
    active_runner = runner or SubprocessRunner()
    topology = load_local_topology(root, active_runner)
    acceptance = Acceptance(active_runner, root, topology)
    if options.mode == "inspect":
        acceptance.inspect()
        acceptance.clients[acceptance.acceptance_cell]
        return
    acceptance.pulls(load_publication_contract(root))


def main() -> int:
    try:
        execute(sys.argv[1:])
    except AcceptanceError:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
