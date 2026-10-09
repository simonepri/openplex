#!/usr/bin/env python3
"""Provide unified CLI tooling for building, publishing, and running containerized workload applications."""

from __future__ import annotations

import getpass
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

REPOSITORY_COMPONENT_PATTERN = r"[a-z0-9]+(([._]|__|-+)[a-z0-9]+)*"
REPOSITORY_PATH_PATTERN = re.compile(
    rf"^{REPOSITORY_COMPONENT_PATTERN}(/{REPOSITORY_COMPONENT_PATTERN})*$"
)
REGISTRY_HOST_PATTERN = r"[a-z0-9]+([.-][a-z0-9]+)*(:[0-9]+)?"
REPOSITORY_PATTERN = re.compile(
    rf"^({REGISTRY_HOST_PATTERN}/)?{REPOSITORY_COMPONENT_PATTERN}(/{REPOSITORY_COMPONENT_PATTERN})*$"
)
WORKLOAD_PATTERN = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")
FLOCI_REGISTRY_PATTERN = re.compile(
    r"^((origin-registry:5000|(localhost|127\.0\.0\.1):(4566|15100))/"
    r"[0-9]{12}/[a-z]{2}(-gov)?-[a-z]+-[0-9]+"
    r"|[0-9]{12}\.dkr\.ecr\.[a-z0-9-]+\.localhost\.floci\.io(:[0-9]+)?)$"
)
ECR_REGISTRY_PATTERN = re.compile(r"^[0-9]{12}\.dkr\.ecr\.[a-z0-9-]+\.amazonaws\.com(\.cn)?$")
STREAM_TAG_PATTERN = re.compile(r"^((ci|dev)-)?[0-9]{8}T[0-9]{6}Z_[0-9a-f]{12}$")
STREAM_TAG_PARTS = re.compile(
    r"^(?P<prefix>(?:ci|dev)-)?(?P<stamp>[0-9]{8}T[0-9]{6}Z)_[0-9a-f]{12}(?P<role>-[a-z0-9-]+)?$"
)
EXTRA_TAG_PATTERN = re.compile(r"^[a-zA-Z0-9_][a-zA-Z0-9_.-]{0,127}$")
SHA256_DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
TEAM_NAMESPACE_PATTERN = re.compile(r"^team-[a-z0-9]([a-z0-9-]*[a-z0-9])?$")
CELL_NAME_PATTERN = re.compile(r"^cell-[a-z0-9]([a-z0-9-]*[a-z0-9])?$")
TEAM_LABEL_PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
MANIFEST_ACCEPT_HEADER = (
    "application/vnd.oci.image.index.v1+json, "
    "application/vnd.oci.image.manifest.v1+json, "
    "application/vnd.docker.distribution.manifest.list.v2+json, "
    "application/vnd.docker.distribution.manifest.v2+json"
)

UNSET_AWS_ENV_VARS = [
    "AWS_ACCESS_KEY_ID",
    "AWS_CONTAINER_AUTHORIZATION_TOKEN",
    "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
    "AWS_DEFAULT_PROFILE",
    "AWS_CA_BUNDLE",
    "AWS_EC2_METADATA_SERVICE_ENDPOINT",
    "AWS_EC2_METADATA_SERVICE_ENDPOINT_MODE",
    "AWS_ENDPOINT_URL",
    "AWS_ENDPOINT_URL_ECR",
    "AWS_ENDPOINT_URL_STS",
    "AWS_PROFILE",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SECURITY_TOKEN",
    "AWS_SESSION_TOKEN",
]


MANIFEST_PARTS_COUNT = 2
MAX_WORKLOAD_LEN = 15
MAX_RUN_NAME_LEN = 47
MAX_TEAM_LEN = 31
SUBCOMMAND_ARG_COUNT = 6


def _search_runfiles_manifest(manifest_file: str, targets: set[str]) -> str | None:
    with Path(manifest_file).open("r", encoding="utf-8") as f:
        for line in f:
            parts = line.strip().split(" ", 1)
            if len(parts) == MANIFEST_PARTS_COUNT and parts[0] in targets:
                return parts[1]
    return None


def _resolve_runfiles_candidate(path: str) -> str | None:
    """Find path in Bazel runfiles directory or manifest."""
    runfiles_dir = os.environ.get("RUNFILES_DIR")
    if runfiles_dir:
        base = Path(runfiles_dir)
        for sub in (path, f"_main/{path}"):
            candidate = base / sub
            if candidate.exists():
                return str(candidate)

    manifest_file = os.environ.get("RUNFILES_MANIFEST_FILE")
    if manifest_file and Path(manifest_file).is_file():
        return _search_runfiles_manifest(manifest_file, {path, f"_main/{path}"})
    return None


def _resolve_candidate(p: Path, path: str, workspace: str | None) -> str | None:
    if p.is_absolute() and p.exists():
        return str(p)
    runfile = _resolve_runfiles_candidate(path)
    if runfile:
        return runfile
    if workspace and (Path(workspace) / path).exists():
        return str(Path(workspace) / path)
    return None


def resolve_path(path: str, workspace: str | None = None) -> str:
    """Resolve runfiles or workspace path for a tool or data dependency."""
    if not path:
        return path
    p = Path(path)
    candidate = _resolve_candidate(p, path, workspace)
    if candidate:
        return candidate
    return str(p.resolve()) if p.exists() else path


@dataclass
class PublishArgs:
    """Arguments for workload image publication."""

    workload: str
    repository_path: str
    pusher: str
    index: str
    crane: str
    publication_mode: str


@dataclass
class ImageDelivery:
    """Contract for delivering an image to a registry target."""

    pusher: str
    repository: str
    digest: str
    tag: str
    publication_mode: str
    crane: str = ""
    workspace: str = ""
    extra_tags: list[str] | None = None


def parse_publish_images(
    pusher_arg: str, index_arg: str, workspace: str
) -> tuple[list[str], list[str], list[str]]:
    """Parse role names, pusher targets, and expected image digests."""
    roles: list[str] = []
    pushers: list[str] = []
    indices: list[str] = []

    if index_arg != "-":
        roles.append("")
        pushers.append(pusher_arg)
        indices.append(index_arg)
    else:
        for entry in pusher_arg.split(","):
            if not entry:
                continue
            role, rest = entry.split("=", 1)
            role_pusher, role_index = rest.split("=", 1)
            roles.append(role)
            pushers.append(role_pusher)
            indices.append(role_index)

    if not roles:
        sys.stderr.write("No workload images specified for publication.\n")
        sys.exit(1)

    expected_digests: list[str] = []
    for idx_file in indices:
        resolved_idx = resolve_path(idx_file, workspace)
        if not Path(resolved_idx).is_file():
            sys.stderr.write(f"Image index file does not exist: {resolved_idx}\n")
            sys.exit(1)
        with Path(resolved_idx).open("r", encoding="utf-8") as f:
            idx_data = json.load(f)
        digest = idx_data.get("manifests", [{}])[0].get("digest", "")
        if not SHA256_DIGEST_PATTERN.match(digest):
            sys.stderr.write(f"Image index {resolved_idx} contains invalid digest: {digest}\n")
            sys.exit(1)
        expected_digests.append(digest)

    return roles, pushers, expected_digests


def determine_stream_tag(workspace: str) -> str:
    """Read or generate the immutable release stream tag."""
    tag = os.environ.get("WORKLOAD_STREAM_TAG")
    if not tag:
        script = Path(workspace) / "src/bazel/rules/oci/stream_tag.sh"
        base_tag = subprocess.check_output(["bash", str(script)], cwd=workspace, text=True).strip()
        is_ci = os.environ.get("CI") == "true" or os.environ.get("GITHUB_ACTIONS") == "true"
        default_prefix = "ci" if is_ci else "dev"
        prefix = os.environ.get("WORKLOAD_TAG_PREFIX", default_prefix)
        tag = f"{prefix}-{base_tag}" if prefix else base_tag
    if not STREAM_TAG_PATTERN.match(tag):
        sys.stderr.write(f"Invalid workload stream tag: {tag}\n")
        sys.exit(1)
    return tag


def parse_extra_tags(extra_tags_raw: str | None = None) -> list[str]:
    """Parse and validate extra image tags from WORKLOAD_EXTRA_TAGS or argument string."""
    raw = (
        extra_tags_raw if extra_tags_raw is not None else os.environ.get("WORKLOAD_EXTRA_TAGS", "")
    ).strip()
    if not raw:
        return []
    tags: list[str] = []
    seen: set[str] = set()
    for tag in re.split(r"[\s,]+", raw):
        if tag and tag not in seen:
            seen.add(tag)
            tags.append(tag)
    for tag in tags:
        if not EXTRA_TAG_PATTERN.match(tag):
            sys.stderr.write(f"Invalid workload extra tag: {tag}\n")
            sys.exit(1)
    return tags


def _resolve_extra_tags(delivery: ImageDelivery) -> list[str]:
    if delivery.extra_tags is not None:
        for tag in delivery.extra_tags:
            if not EXTRA_TAG_PATTERN.match(tag):
                sys.stderr.write(f"Invalid workload extra tag: {tag}\n")
                sys.exit(1)
        return delivery.extra_tags
    return parse_extra_tags()


def _find_ca_in_roots(roots: list[Path]) -> Path | None:
    for r in roots:
        floci_ca = r / ".tmp/state/floci/tls/ca.crt"
        if floci_ca.is_file():
            return floci_ca
        headscale_ca = r / ".tmp/state/headscale/tls/ca.crt"
        if headscale_ca.is_file():
            return headscale_ca
    return None


def _find_ca_file(workspace: str) -> Path | None:
    if os.environ.get("SSL_CERT_FILE"):
        ca_candidate = Path(os.environ["SSL_CERT_FILE"])
        if ca_candidate.is_file():
            return ca_candidate

    roots: list[Path] = []
    if workspace:
        roots.append(Path(workspace))
    bwd = os.environ.get("BUILD_WORKSPACE_DIRECTORY")
    if bwd:
        roots.append(Path(bwd))
    return _find_ca_in_roots(roots)


@dataclass
class LocalRegistryContext:
    """Connection and authentication options for local registry transport."""

    scheme: str
    ssl_context: ssl.SSLContext | None
    tool_insecure_args: list[str]
    subproc_env: dict[str, str] | None


def _local_registry_tls_context(workspace: str = "", repository: str = "") -> LocalRegistryContext:
    """Return scheme, TLS context, tool args, and environment for local registry communication."""
    if os.environ.get("WORKLOAD_REGISTRY_INSECURE", "false") == "true" or repository.startswith(
        "origin-registry:5000/"
    ):
        return LocalRegistryContext("http", None, ["--insecure"], None)

    ca_file = _find_ca_file(workspace)
    ssl_context = ssl.create_default_context(cafile=str(ca_file) if ca_file else None)
    subproc_env = dict(os.environ)
    if ca_file:
        subproc_env["SSL_CERT_FILE"] = str(ca_file)

    return LocalRegistryContext("https", ssl_context, [], subproc_env)


def _validate_insecure_flag(workload_registry: str, insecure_raw: str) -> bool:
    if insecure_raw not in {"true", "false"}:
        sys.stderr.write(f"Invalid WORKLOAD_REGISTRY_INSECURE value: {insecure_raw}\n")
        sys.exit(1)
    insecure = insecure_raw == "true"
    if insecure and not FLOCI_REGISTRY_PATTERN.match(workload_registry):
        sys.stderr.write(
            "Insecure workload publication is restricted to the registered Floci workspace origin.\n"
        )
        sys.exit(1)
    return insecure


def _read_origin_registry_repo(workspace: str, repository_path: str) -> str:
    registry_contract = Path(workspace) / ".tmp/state/origin-registry.json"
    if not registry_contract.is_file():
        return ""
    with registry_contract.open("r", encoding="utf-8") as f:
        data = json.load(f)
        if data.get("version") != 1:
            sys.stderr.write("unsupported origin registry contract\n")
            sys.exit(1)
        return str(data.get("host", {}).get("repositories", {}).get(repository_path, ""))


def determine_publish_repository(workspace: str, repository_path: str) -> tuple[str, bool]:
    """Resolve the origin repository and whether it requires local/insecure transport."""
    workload_registry = os.environ.get("WORKLOAD_REGISTRY", "")
    insecure_raw = os.environ.get("WORKLOAD_REGISTRY_INSECURE", "false")
    workload_registry_insecure = _validate_insecure_flag(workload_registry, insecure_raw)
    local_repository = _read_origin_registry_repo(workspace, repository_path)

    if not workload_registry:
        if not local_repository:
            sys.stderr.write(
                "The local origin registry contract is missing this workload repository; run mise run //src/infra:up first.\n"
            )
            sys.exit(1)
        repository = local_repository
        local_registry = True
    else:
        repository = f"{workload_registry}/{repository_path}"
        local_registry = (
            bool(FLOCI_REGISTRY_PATTERN.match(workload_registry))
            or workload_registry_insecure
            or (bool(local_repository) and repository == local_repository)
        )

    if not REPOSITORY_PATTERN.match(repository):
        sys.stderr.write(f"Invalid workload image repository: {repository}\n")
        sys.exit(1)

    if (
        workload_registry
        and not local_registry
        and not ECR_REGISTRY_PATTERN.match(workload_registry)
    ):
        sys.stderr.write("Managed workload publication requires the bare AWS ECR origin host.\n")
        sys.exit(1)

    return repository, local_registry


def _check_local_existing_tag(delivery: ImageDelivery) -> bool:
    if delivery.publication_mode == "stream" and delivery.tag:
        existing_digest = local_registry_tag_digest(
            delivery.repository, delivery.tag, delivery.workspace
        )
        if existing_digest == delivery.digest:
            return True
        if existing_digest is not None:
            sys.stderr.write(
                f"Local registry tag {delivery.repository}:{delivery.tag} already names {existing_digest}.\n"
            )
            sys.exit(1)
    return False


def _check_local_manifest_exists(delivery: ImageDelivery, ctx: LocalRegistryContext) -> bool:
    return _local_manifest_digest(delivery.repository, delivery.digest, ctx) == delivery.digest


def _tag_local_stream(delivery: ImageDelivery, ctx: LocalRegistryContext) -> None:
    if delivery.publication_mode == "stream" and delivery.tag and delivery.crane:
        target_ref = f"{delivery.repository}@{delivery.digest}"
        try:
            subprocess.run(
                [delivery.crane, "tag", *ctx.tool_insecure_args, target_ref, delivery.tag],
                env=ctx.subproc_env,
                stdout=sys.stderr,
                check=True,
            )
        except subprocess.CalledProcessError as e:
            sys.exit(e.returncode)


def _push_local_stream_and_verify(delivery: ImageDelivery, ctx: LocalRegistryContext) -> None:
    pusher_args = [*ctx.tool_insecure_args, "--repository", delivery.repository]
    if delivery.publication_mode == "stream" and delivery.tag:
        pusher_args.extend(["--tag", delivery.tag])

    try:
        subprocess.run(
            [delivery.pusher, *pusher_args], env=ctx.subproc_env, stdout=sys.stderr, check=True
        )
    except subprocess.CalledProcessError as e:
        sys.exit(e.returncode)

    if delivery.publication_mode == "stream" and delivery.tag:
        registry_digest = local_registry_tag_digest(
            delivery.repository, delivery.tag, delivery.workspace
        )
        if registry_digest != delivery.digest:
            ref = f"{delivery.repository}:{delivery.tag}"
            sys.stderr.write(f"Local registry returned an unexpected digest for {ref}.\n")
            sys.exit(1)

    if not _check_local_manifest_exists(delivery, ctx):
        sys.stderr.write(f"Local registry is missing expected image {delivery.digest}.\n")
        sys.exit(1)


def _tag_local_extra_tags(
    delivery: ImageDelivery, ctx: LocalRegistryContext, extra_tags: list[str]
) -> None:
    if not extra_tags or not delivery.crane:
        return
    target_ref = f"{delivery.repository}@{delivery.digest}"
    for extra_tag in extra_tags:
        try:
            subprocess.run(
                [delivery.crane, "tag", *ctx.tool_insecure_args, target_ref, extra_tag],
                env=ctx.subproc_env,
                stdout=sys.stderr,
                check=True,
            )
        except subprocess.CalledProcessError as e:
            sys.exit(e.returncode)


def deliver_to_local_registry(delivery: ImageDelivery) -> None:
    """Push image to local Floci registry and verify digest via HTTP HEAD."""
    extra_tags = _resolve_extra_tags(delivery)
    ctx = _local_registry_tls_context(delivery.workspace, delivery.repository)
    if _check_local_existing_tag(delivery):
        return

    if _check_local_manifest_exists(delivery, ctx):
        _tag_local_stream(delivery, ctx)
    else:
        _push_local_stream_and_verify(delivery, ctx)

    _tag_local_extra_tags(delivery, ctx, extra_tags)


def local_registry_tag_digest(repository: str, tag: str, workspace: str = "") -> str | None:
    """Resolve a local tag, returning None only when the registry reports it absent."""
    return _local_manifest_digest(
        repository, tag, _local_registry_tls_context(workspace, repository)
    )


def _local_manifest_digest(
    repository: str, reference: str, ctx: LocalRegistryContext
) -> str | None:
    registry_address, repo_path = repository.split("/", 1)
    request = urllib.request.Request(
        f"{ctx.scheme}://{registry_address}/v2/{repo_path}/manifests/{reference}",
        headers={"Accept": MANIFEST_ACCEPT_HEADER},
        method="HEAD",
    )
    try:
        with urllib.request.urlopen(request, context=ctx.ssl_context, timeout=30) as response:
            digest = response.headers.get("Docker-Content-Digest", "")
    except urllib.error.HTTPError as error:
        error.close()
        if error.code == 404:
            return None
        sys.stderr.write(
            f"Cannot resolve local registry reference {repository}:{reference} (HTTP {error.code}).\n"
        )
        sys.exit(1)
    except (OSError, urllib.error.URLError) as error:
        sys.stderr.write(f"Cannot connect to local registry {registry_address}: {error}.\n")
        sys.exit(1)
    if not isinstance(digest, str) or not SHA256_DIGEST_PATTERN.fullmatch(digest):
        sys.stderr.write(
            f"Local registry returned an invalid manifest digest for {repository}:{reference}.\n"
        )
        sys.exit(1)
    return digest


def _validate_ecr_web_role(web_role: str, web_token: str) -> None:
    if (web_role and not web_token) or (not web_role and web_token):
        sys.stderr.write(
            "Managed web identity requires both AWS_ROLE_ARN and AWS_WEB_IDENTITY_TOKEN_FILE.\n"
        )
        sys.exit(1)


def _validate_ecr_container_uri(container_uri: str, container_token: str) -> None:
    if (container_uri and not container_token) or (not container_uri and container_token):
        sys.stderr.write(
            "EKS Pod Identity requires both its credential endpoint and authorization token file.\n"
        )
        sys.exit(1)


def _validate_ecr_tokens(
    web_role: str, web_token: str, container_uri: str, container_token: str
) -> None:
    if web_role:
        web_token_path = Path(web_token)
        if not web_token_path.is_file() or not os.access(web_token_path, os.R_OK):
            sys.stderr.write("Managed web identity requires a readable projected token.\n")
            sys.exit(1)
    elif container_uri:
        container_token_path = Path(container_token)
        if (
            container_uri != "http://169.254.170.23/v1/credentials"
            or not container_token_path.is_file()
            or not os.access(container_token_path, os.R_OK)
        ):
            sys.stderr.write(
                "EKS Pod Identity requires the injected agent endpoint and a readable authorization token.\n"
            )
            sys.exit(1)
    else:
        sys.stderr.write(
            "Managed ECR publication requires EKS Pod Identity or projected web identity.\n"
        )
        sys.exit(1)


def validate_ecr_identity() -> None:
    """Validate Amazon ECR authentication and identity preconditions."""
    if not shutil.which("docker-credential-ecr-login"):
        sys.stderr.write("Amazon ECR publication requires docker-credential-ecr-login.\n")
        sys.exit(1)

    web_role = os.environ.get("AWS_ROLE_ARN", "")
    web_token = os.environ.get("AWS_WEB_IDENTITY_TOKEN_FILE", "")
    _validate_ecr_web_role(web_role, web_token)

    container_uri = os.environ.get("AWS_CONTAINER_CREDENTIALS_FULL_URI", "")
    container_token = os.environ.get("AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE", "")
    _validate_ecr_container_uri(container_uri, container_token)

    if web_role and container_uri:
        sys.stderr.write("Managed ECR publication accepts exactly one workload identity mode.\n")
        sys.exit(1)

    _validate_ecr_tokens(web_role, web_token, container_uri, container_token)


def build_ecr_environment(docker_config_dir: str) -> dict[str, str]:
    """Build a sanitized environment preventing credential clashes during ECR push."""
    env = os.environ.copy()
    for var in UNSET_AWS_ENV_VARS:
        env.pop(var, None)
    cfg_dir = Path(docker_config_dir)
    env["AWS_CONFIG_FILE"] = str(cfg_dir / "aws-config")
    env["AWS_EC2_METADATA_DISABLED"] = "true"
    env["AWS_ECR_DISABLE_CACHE"] = "true"
    env["AWS_SDK_LOAD_CONFIG"] = "false"
    env["AWS_SHARED_CREDENTIALS_FILE"] = str(cfg_dir / "aws-credentials")
    env["DOCKER_CONFIG"] = docker_config_dir
    return env


def _setup_ecr_docker_config(docker_config: str, registry_host: str) -> None:
    cfg_dir = Path(docker_config)
    cfg_dir.chmod(0o700)
    aws_cfg = cfg_dir / "aws-config"
    aws_creds = cfg_dir / "aws-credentials"
    cfg_json = cfg_dir / "config.json"

    for p in (aws_cfg, aws_creds):
        p.touch(mode=0o600)
    with cfg_json.open("w", encoding="utf-8") as f:
        json.dump({"credHelpers": {registry_host: "ecr-login"}}, f)
    cfg_json.chmod(0o600)


def _latest_stream_tag(tags: list[str], current_tag: str) -> str | None:
    """Return the newest tag in the same prefix and role stream as current_tag."""
    current = STREAM_TAG_PARTS.match(current_tag)
    if not current:
        return None
    stream = [
        (match.group("stamp"), tag)
        for tag in tags
        if (match := STREAM_TAG_PARTS.match(tag))
        and match.group("prefix") == current.group("prefix")
        and match.group("role") == current.group("role")
    ]
    return max(stream)[1] if stream else None


def _stream_already_tags_digest(delivery: ImageDelivery, ecr_env: dict[str, str]) -> bool:
    """Report whether the newest stream tag already names this digest."""
    listing = subprocess.run(
        [delivery.crane, "ls", delivery.repository],
        env=ecr_env,
        capture_output=True,
        text=True,
        check=False,
    )
    if listing.returncode != 0:
        return False
    latest = _latest_stream_tag(listing.stdout.split(), delivery.tag)
    if not latest:
        return False
    resolved = subprocess.run(
        [delivery.crane, "digest", f"{delivery.repository}:{latest}"],
        env=ecr_env,
        capture_output=True,
        text=True,
        check=False,
    )
    return resolved.returncode == 0 and resolved.stdout.strip() == delivery.digest


def _ecr_tag_stream(delivery: ImageDelivery, target_ref: str, ecr_env: dict[str, str]) -> None:
    if delivery.publication_mode == "stream" and delivery.tag:
        # A new stream tag on an unchanged digest reads as a new release to tag-tracking consumers.
        if _stream_already_tags_digest(delivery, ecr_env):
            return
        try:
            subprocess.run(
                [delivery.crane, "tag", target_ref, delivery.tag],
                env=ecr_env,
                stdout=sys.stderr,
                check=True,
            )
        except subprocess.CalledProcessError as e:
            sys.exit(e.returncode)


def _ecr_push_and_verify(delivery: ImageDelivery, ecr_env: dict[str, str]) -> None:
    pusher_args = ["--repository", delivery.repository]
    if delivery.publication_mode == "stream" and delivery.tag:
        pusher_args.extend(["--tag", delivery.tag])

    try:
        subprocess.run([delivery.pusher, *pusher_args], env=ecr_env, stdout=sys.stderr, check=True)
    except subprocess.CalledProcessError as e:
        sys.exit(e.returncode)

    ref = (
        f"{delivery.repository}:{delivery.tag}"
        if delivery.publication_mode == "stream" and delivery.tag
        else f"{delivery.repository}@{delivery.digest}"
    )
    try:
        res = subprocess.run(
            [delivery.crane, "digest", ref],
            env=ecr_env,
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError as e:
        sys.exit(e.returncode)
    if res.stdout.strip() != delivery.digest:
        sys.stderr.write(f"AWS ECR returned an unexpected digest for {ref}.\n")
        sys.exit(1)


def _ecr_tag_extra_tags(
    delivery: ImageDelivery,
    target_ref: str,
    ecr_env: dict[str, str],
    extra_tags: list[str],
) -> None:
    if not extra_tags or not delivery.crane:
        return
    for extra_tag in extra_tags:
        try:
            subprocess.run(
                [delivery.crane, "tag", target_ref, extra_tag],
                env=ecr_env,
                stdout=sys.stderr,
                check=True,
            )
        except subprocess.CalledProcessError as e:
            sys.exit(e.returncode)


def deliver_to_ecr(delivery: ImageDelivery) -> None:
    """Deliver image to managed AWS ECR with isolated credentials and verify digest."""
    extra_tags = _resolve_extra_tags(delivery)
    validate_ecr_identity()
    registry_host = delivery.repository.split("/", 1)[0]

    with tempfile.TemporaryDirectory(prefix="ecr-auth.") as docker_config:
        _setup_ecr_docker_config(docker_config, registry_host)
        ecr_env = build_ecr_environment(docker_config)
        target_ref = f"{delivery.repository}@{delivery.digest}"
        check_res = subprocess.run(
            [delivery.crane, "digest", target_ref],
            env=ecr_env,
            capture_output=True,
            text=True,
            check=False,
        )
        cache_hit = check_res.returncode == 0 and check_res.stdout.strip() == delivery.digest

        if cache_hit:
            _ecr_tag_stream(delivery, target_ref, ecr_env)
        else:
            _ecr_push_and_verify(delivery, ecr_env)

        _ecr_tag_extra_tags(delivery, target_ref, ecr_env, extra_tags)


def _validate_publish_args(args: PublishArgs, workspace: str) -> str:
    resolved_crane = resolve_path(args.crane, workspace)
    if not Path(resolved_crane).is_file() or not os.access(resolved_crane, os.X_OK):
        sys.stderr.write(f"Bazel crane is not executable: {args.crane}\n")
        sys.exit(1)

    if args.publication_mode not in {"digest", "stream"}:
        sys.stderr.write(f"Invalid workload publication mode: {args.publication_mode}\n")
        sys.exit(1)

    if not WORKLOAD_PATTERN.match(args.workload) or len(args.workload) > MAX_WORKLOAD_LEN:
        sys.stderr.write(
            f"Invalid pipeline slug (lowercase DNS-safe, maximum {MAX_WORKLOAD_LEN} characters): {args.workload}\n"
        )
        sys.exit(1)

    if not REPOSITORY_PATH_PATTERN.match(
        args.repository_path
    ) or not args.repository_path.startswith("src/"):
        sys.stderr.write(f"Invalid workload repository path: {args.repository_path}\n")
        sys.exit(1)

    return resolved_crane


def _deliver_image(delivery: ImageDelivery, *, local_registry: bool) -> None:
    if local_registry:
        deliver_to_local_registry(delivery)
    else:
        if not ECR_REGISTRY_PATTERN.match(delivery.repository.split("/", 1)[0]):
            sys.stderr.write("Secure workload publication requires AWS ECR.\n")
            sys.exit(1)
        deliver_to_ecr(delivery)


def execute_publish(args: PublishArgs) -> None:
    """Execute workload image publication and write origin reference map to stdout."""
    workspace = os.environ.get("BUILD_WORKSPACE_DIRECTORY", "")
    if not workspace:
        sys.stderr.write("BUILD_WORKSPACE_DIRECTORY is required: run this target via bazel run\n")
        sys.exit(1)

    resolved_crane = _validate_publish_args(args, workspace)
    extra_tags = parse_extra_tags()
    stream_tag = determine_stream_tag(workspace) if args.publication_mode == "stream" else ""
    repository, local_registry = determine_publish_repository(workspace, args.repository_path)
    roles, pushers, digests = parse_publish_images(args.pusher, args.index, workspace)

    references: dict[str, str] = {}
    if not (len(roles) == len(pushers) == len(digests)):
        raise ValueError("roles, pushers, and digests must have identical length")
    for role, pusher_target, expected_digest in zip(roles, pushers, digests, strict=True):
        resolved_pusher = resolve_path(pusher_target, workspace)
        role_tag = (
            f"{stream_tag}-{role}" if (args.publication_mode == "stream" and role) else stream_tag
        )

        delivery = ImageDelivery(
            pusher=resolved_pusher,
            repository=repository,
            digest=expected_digest,
            tag=role_tag,
            publication_mode=args.publication_mode,
            crane=resolved_crane,
            workspace=workspace,
            extra_tags=extra_tags,
        )

        _deliver_image(delivery, local_registry=local_registry)
        references[role] = f"{repository}@{expected_digest}"

    print(json.dumps(references, sort_keys=True))


@dataclass
class RunArgs:
    """Arguments for on-demand workload run."""

    workload: str
    repository_path: str
    publisher: str
    manifest: str
    team_namespace: str
    kubectl: str


@dataclass
class MutationConfig:
    """Configuration for templating manifest placeholders and image references."""

    workload: str
    run_name: str
    run_id: str
    launcher: str
    team_namespace: str
    virtual_cell: str
    deployment_refs: dict[str, str]


@dataclass
class ClusterTarget:
    """Target cluster parameters for workload admission."""

    kubectl: str
    kubeconfig: str | None
    target_cell: str
    team_namespace: str


def _resolve_run_launcher() -> str:
    launcher_raw = (
        os.environ.get("WORKLOAD_LAUNCHER") or os.environ.get("USER") or os.environ.get("LOGNAME")
    )
    if not launcher_raw:
        try:
            launcher_raw = getpass.getuser()
        except Exception:
            launcher_raw = "default"
    launcher = re.sub(r"[^a-z0-9]", "", launcher_raw.lower())[:MAX_WORKLOAD_LEN]
    if not launcher:
        sys.stderr.write(f"Launcher identity normalizes to an empty username: {launcher_raw}\n")
        sys.exit(1)
    return launcher


def _resolve_run_id() -> str:
    run_id = os.environ.get("WORKLOAD_RUN_ID") or str(int(time.time()))
    if not WORKLOAD_PATTERN.match(run_id) or len(run_id) > MAX_WORKLOAD_LEN:
        sys.stderr.write(
            f"Invalid workload run ID (lowercase DNS-safe, maximum {MAX_WORKLOAD_LEN} characters): {run_id}\n"
        )
        sys.exit(1)
    return run_id


def _resolve_target_cell() -> str:
    target_cell = os.environ.get("WORKLOAD_TARGET_CELL") or os.environ.get("WORKSPACE_CELL") or ""
    if not target_cell:
        sys.stderr.write(
            "Set WORKLOAD_TARGET_CELL or run from a Coder workspace with WORKSPACE_CELL.\n"
        )
        sys.exit(1)
    if not CELL_NAME_PATTERN.match(target_cell):
        sys.stderr.write(f"Invalid workload target cell: {target_cell}\n")
        sys.exit(1)
    if not target_cell.startswith("cell-eaws-") and not os.environ.get("WORKLOAD_REGISTRY"):
        sys.stderr.write(
            f"WORKLOAD_REGISTRY is required for non-local fleet cluster {target_cell}.\n"
        )
        sys.exit(1)
    return target_cell


def validate_run_preflight(args: RunArgs) -> tuple[str, str, str, str]:
    """Validate command line flags and environment variables for on-demand execution."""
    if not WORKLOAD_PATTERN.match(args.workload) or len(args.workload) > MAX_WORKLOAD_LEN:
        sys.stderr.write(
            f"Invalid pipeline slug (lowercase DNS-safe, maximum {MAX_WORKLOAD_LEN} characters): {args.workload}\n"
        )
        sys.exit(1)

    if not REPOSITORY_PATH_PATTERN.match(args.repository_path):
        sys.stderr.write(f"Invalid workload repository path: {args.repository_path}\n")
        sys.exit(1)

    if not TEAM_NAMESPACE_PATTERN.match(args.team_namespace):
        sys.stderr.write(f"Invalid team namespace: {args.team_namespace}\n")
        sys.exit(1)

    run_id = _resolve_run_id()
    launcher = _resolve_run_launcher()
    run_name = f"{args.workload}-{launcher}-{run_id}"
    if len(run_name) > MAX_RUN_NAME_LEN:
        sys.stderr.write(
            f"Canonical workload name exceeds KubeRay's {MAX_RUN_NAME_LEN}-character limit: {run_name}\n"
        )
        sys.exit(1)

    target_cell = _resolve_target_cell()
    return run_id, launcher, run_name, target_cell


def run_kubectl_cmd(
    kubectl: str, kubeconfig: str | None, subcmd: list[str], input_data: str | None = None
) -> str:
    """Execute a kubectl command optionally targeting a cell-specific kubeconfig."""
    cmd = [kubectl]
    if kubeconfig:
        cmd.extend(["--kubeconfig", kubeconfig])
    cmd.extend(subcmd)
    res = subprocess.run(cmd, input=input_data, capture_output=True, text=True, check=True)
    return res.stdout


AUTH_RESOURCE_MAP = {
    "RayService": ("rayservices.ray.io", "RayServices"),
    "Job": ("jobs.batch", "Jobs"),
    "CronJob": ("cronjobs.batch", "CronJobs"),
    "RayJob": ("rayjobs.ray.io", "RayJobs"),
    "": ("rayjobs.ray.io", "RayJobs"),
}


def _extract_manifest_kind(content: str) -> str:
    try:
        data = json.loads(content)
        if isinstance(data, dict):
            return str(data.get("kind", ""))
    except json.JSONDecodeError:
        pass
    for line in content.splitlines():
        if line.startswith("kind:"):
            return line.split(":", 1)[1].strip()
    return ""


def resolve_auth_resource(manifest_path: str) -> tuple[str, str]:
    """Determine the Kubernetes RBAC resource kind from manifest file."""
    content = Path(manifest_path).read_text(encoding="utf-8")
    kind = _extract_manifest_kind(content)
    return AUTH_RESOURCE_MAP.get(kind, (f"{kind.lower()}s", f"{kind}s"))


def _parse_repo_contract(out: str) -> dict[str, str]:
    try:
        cm_data = json.loads(out)
        repo_json = cm_data.get("data", {}).get("repositories.json", "")
        repo_contract = json.loads(repo_json)
    except (json.JSONDecodeError, AttributeError):
        return {}
    if not isinstance(repo_contract, dict) or repo_contract.get("version") != 1:
        return {}
    repos = repo_contract.get("repositories", {})
    if isinstance(repos, dict):
        return {str(k): str(v) for k, v in repos.items()}
    return {}


def resolve_deployment_repository(target: ClusterTarget, repository_path: str) -> str:
    """Read the deployment repository contract from the team namespace."""
    try:
        out = run_kubectl_cmd(
            target.kubectl,
            target.kubeconfig,
            [
                "get",
                "configmap",
                "workload-repositories",
                "--namespace",
                target.team_namespace,
                "--output=json",
            ],
        )
    except subprocess.CalledProcessError:
        sys.stderr.write(
            f"Cannot read the workload repository contract in {target.team_namespace} on {target.target_cell}.\n"
        )
        sys.exit(1)

    repos = _parse_repo_contract(out)
    deployment_repo = repos.get(repository_path, "")
    if not deployment_repo:
        sys.stderr.write(
            f"The workload repository contract in {target.team_namespace} on {target.target_cell} has no repository for {repository_path}.\n"
        )
        sys.exit(1)

    if not REPOSITORY_PATTERN.match(deployment_repo):
        sys.stderr.write(f"Invalid deployment image repository: {deployment_repo}\n")
        sys.exit(1)

    return deployment_repo


def _resolve_role_image(val: str, workload: str, deployment_refs: dict[str, str]) -> str | None:
    p = (
        rf"^registry\.invalid/workloads/{re.escape(workload)}[/-]"
        rf"(?P<role>[a-z0-9_-]+)(@sha256:[0-9a-f]{{64}}|:[a-zA-Z0-9_.-]+)?$"
    )
    m = re.match(p, val)
    if not m:
        return None
    role = m.group("role")
    if role in deployment_refs:
        return deployment_refs[role]
    if "" in deployment_refs:
        return deployment_refs[""]
    raise KeyError(f"unknown image role: {role}")


def _resolve_manifest_image(val: str, workload: str, deployment_refs: dict[str, str]) -> str | None:
    """Match virtual placeholder workload image to published digest reference."""
    p1 = rf"^registry\.invalid/workloads/{re.escape(workload)}(@sha256:[0-9a-f]{{64}}|:[a-zA-Z0-9_.-]+)?$"
    if re.match(p1, val):
        return (
            deployment_refs.get("")
            or deployment_refs.get("default")
            or next(iter(deployment_refs.values()))
        )
    return _resolve_role_image(val, workload, deployment_refs)


def _substitute_string(obj: str, config: MutationConfig) -> str:
    resolved = _resolve_manifest_image(obj, config.workload, config.deployment_refs)
    if resolved is not None:
        return resolved
    return obj.replace("__WORKLOAD_RUN_ID__", config.run_name).replace(
        "__WORKLOAD_CELL_VIRTUAL__", config.virtual_cell
    )


def mutate_manifest(
    manifest_obj: Mapping[str, object],
    config: MutationConfig,
) -> dict[str, object]:
    """Substitute template placeholders, labels, and image references in manifest."""
    raw_data: object = json.loads(json.dumps(manifest_obj))
    assert isinstance(raw_data, dict)
    manifest: dict[str, object] = {str(k): v for k, v in raw_data.items()}
    metadata_val = manifest.setdefault("metadata", {})
    if isinstance(metadata_val, dict):
        metadata_val.pop("generateName", None)
        metadata_val["name"] = config.run_name
        metadata_val["namespace"] = config.team_namespace
        labels_val = metadata_val.setdefault("labels", {})
        if isinstance(labels_val, dict):
            labels_val["pipeline"] = config.workload
            labels_val["run-id"] = config.run_id
            labels_val["user"] = config.launcher

    def walk(obj: object) -> object:
        if isinstance(obj, str):
            return _substitute_string(obj, config)
        if isinstance(obj, dict):
            return {k: walk(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [walk(elem) for elem in obj]
        return obj

    res = walk(manifest)
    if isinstance(res, dict):
        return {str(k): v for k, v in res.items()}
    return manifest


def _is_applicable_rayjob(
    submitted_json: Mapping[str, object], run_name: str, team_namespace: str
) -> bool:
    if submitted_json.get("kind") != "RayJob" or submitted_json.get("apiVersion") != "ray.io/v1":
        return False
    metadata = submitted_json.get("metadata")
    if not isinstance(metadata, dict):
        return False
    return bool(metadata.get("name") == run_name and metadata.get("namespace") == team_namespace)


def _extract_dashboard_team(metadata: dict[str, object], run_name: str) -> str:
    labels = metadata.get("labels")
    team = labels.get("team") if isinstance(labels, dict) else None
    if not isinstance(team, str) or len(team) > MAX_TEAM_LEN or not TEAM_LABEL_PATTERN.match(team):
        sys.stderr.write(
            f"Created RayJob {run_name} returned an invalid dashboard-url annotation.\n"
        )
        sys.exit(1)

    workspace_team = os.environ.get("WORKSPACE_TEAM", "")
    if workspace_team and team != workspace_team:
        sys.stderr.write(
            f"Created RayJob {run_name} returned an invalid dashboard-url annotation.\n"
        )
        sys.exit(1)
    return team


@dataclass
class DashboardContext:
    """Validation target context for RayJob dashboard URL."""

    run_name: str
    target_cell: str
    team_namespace: str
    team: str


def _validate_dashboard_url_fields(url: str, ctx: DashboardContext) -> None:
    m = re.match(
        r"^https://(?P<host>[^/]+)/teams/(?P<team>[a-z][a-z0-9]*(?:-[a-z0-9]+)*)/namespaces/(?P<namespace>team-[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)/jobs/(?P<job>[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)/$",
        url,
    )
    if not m:
        sys.stderr.write(
            f"Created RayJob {ctx.run_name} returned an invalid dashboard-url annotation.\n"
        )
        sys.exit(1)

    host = m.group("host")
    host_re = rf"^ray\.{re.escape(ctx.target_cell)}\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*$"
    if (
        not re.match(host_re, host)
        or m.group("team") != ctx.team
        or m.group("namespace") != ctx.team_namespace
        or m.group("job") != ctx.run_name
    ):
        sys.stderr.write(
            f"Created RayJob {ctx.run_name} returned an invalid dashboard-url annotation.\n"
        )
        sys.exit(1)


def check_ray_dashboard(
    submitted_json: Mapping[str, object], run_name: str, target_cell: str, team_namespace: str
) -> None:
    """Validate RayJob dashboard URL contract and print to stdout if available."""
    if not _is_applicable_rayjob(submitted_json, run_name, team_namespace):
        return

    metadata = submitted_json.get("metadata")
    if not isinstance(metadata, dict):
        return

    team = _extract_dashboard_team(metadata, run_name)
    annotations = metadata.get("annotations")
    if annotations is None:
        return
    if not isinstance(annotations, dict):
        sys.stderr.write(
            f"Created RayJob {run_name} returned an invalid dashboard-url annotation.\n"
        )
        sys.exit(1)

    url = annotations.get("dashboard-url")
    if not url:
        return
    if not isinstance(url, str):
        sys.stderr.write(
            f"Created RayJob {run_name} returned an invalid dashboard-url annotation.\n"
        )
        sys.exit(1)

    ctx = DashboardContext(
        run_name=run_name,
        target_cell=target_cell,
        team_namespace=team_namespace,
        team=team,
    )
    _validate_dashboard_url_fields(url, ctx)


def _resolve_run_paths(args: RunArgs, workspace: str) -> tuple[str, str, str]:
    """Resolve and validate paths for kubectl, workload manifest, and image publisher."""
    resolved_kubectl = resolve_path(args.kubectl, workspace)
    if not Path(resolved_kubectl).is_file() or not os.access(resolved_kubectl, os.X_OK):
        sys.stderr.write(f"Bazel kubectl is not executable: {args.kubectl}\n")
        sys.exit(1)

    resolved_manifest = resolve_path(args.manifest, workspace)
    if not Path(resolved_manifest).is_file():
        sys.stderr.write(f"Workload manifest does not exist: {args.manifest}\n")
        sys.exit(1)

    resolved_publisher = resolve_path(args.publisher, workspace)
    if not Path(resolved_publisher).is_file() or not os.access(resolved_publisher, os.X_OK):
        sys.stderr.write(f"Publisher is not executable: {args.publisher}\n")
        sys.exit(1)

    return resolved_kubectl, resolved_manifest, resolved_publisher


def _resolve_cluster_kubeconfig(workspace: str, target_cell: str) -> str | None:
    """Resolve cell-specific kubeconfig path or verify default availability."""
    target_kubeconfig = Path(workspace) / f".tmp/kubeconfigs/{target_cell}.yaml"
    kubeconfig_path = str(target_kubeconfig) if target_kubeconfig.is_file() else None
    if (
        not kubeconfig_path
        and not os.environ.get("KUBECONFIG")
        and not (Path.home() / ".kube/config").is_file()
    ):
        sys.stderr.write(
            f"No Kubernetes configuration is available for workload target cell {target_cell}.\n"
        )
        sys.exit(1)
    return kubeconfig_path


def _verify_cluster_context(target: ClusterTarget) -> None:
    """Ensure current kubectl context strictly matches the targeted cell cluster."""
    try:
        current_ctx = run_kubectl_cmd(
            target.kubectl, target.kubeconfig, ["config", "current-context"]
        ).strip()
    except subprocess.CalledProcessError:
        sys.stderr.write(
            f"Cannot read the Kubernetes context for workload target cell {target.target_cell}.\n"
        )
        sys.exit(1)

    if current_ctx != target.target_cell:
        sys.stderr.write(
            f"Kubernetes context {current_ctx} does not match workload target cell {target.target_cell}.\n"
        )
        sys.exit(1)


def _verify_rbac_permission(target: ClusterTarget, manifest_path: str) -> None:
    """Check that user identity has admission privileges for the workload resource."""
    auth_resource, auth_error = resolve_auth_resource(manifest_path)
    # `kubectl auth can-i` answers "no" with exit status 1, so only other
    # failures carry an error worth showing.
    try:
        auth_out = run_kubectl_cmd(
            target.kubectl,
            target.kubeconfig,
            ["auth", "can-i", "create", auth_resource, "--namespace", target.team_namespace],
        ).strip()
    except subprocess.CalledProcessError as e:
        if e.stdout.strip() != "no":
            sys.stderr.write(e.stderr)
            sys.exit(e.returncode)
        auth_out = "no"
    if auth_out != "yes":
        sys.stderr.write(
            f"Kubernetes identity cannot create {auth_error} in {target.team_namespace} on {target.target_cell}.\n"
        )
        sys.exit(1)


def _publish_and_resolve_images(publisher: str, dep_repo: str) -> dict[str, str]:
    """Execute the workload publisher and map published digests to deployment repositories."""
    try:
        pub_res = subprocess.run([publisher], check=True, text=True, capture_output=True)
    except subprocess.CalledProcessError as e:
        if e.stderr:
            sys.stderr.write(e.stderr)
        sys.exit(e.returncode)

    raw_output = pub_res.stdout.strip()
    if not raw_output:
        sys.stderr.write(f"Publisher {publisher} returned invalid or empty image references: \n")
        sys.exit(1)

    try:
        origin_refs = json.loads(raw_output)
    except json.JSONDecodeError:
        sys.stderr.write(
            f"Publisher {publisher} returned invalid or empty image references: {raw_output}\n"
        )
        sys.exit(1)

    if not isinstance(origin_refs, dict):
        sys.stderr.write(
            f"Publisher {publisher} returned invalid or empty image references: {raw_output}\n"
        )
        sys.exit(1)

    return {str(k): f"{dep_repo}@{str(v).split('@')[-1]}" for k, v in origin_refs.items()}


def _template_and_apply_manifest(
    target: ClusterTarget,
    manifest_path: str,
    config: MutationConfig,
) -> dict[str, object]:
    """Dry-run parse manifest, substitute placeholders and image refs, and create resource."""
    raw_manifest_json = run_kubectl_cmd(
        target.kubectl,
        target.kubeconfig,
        ["create", "--dry-run=client", "--filename", manifest_path, "--output=json"],
    )
    manifest_obj = json.loads(raw_manifest_json)
    templated = mutate_manifest(manifest_obj, config)

    try:
        submitted_str = run_kubectl_cmd(
            target.kubectl,
            target.kubeconfig,
            ["create", "--filename=-", "--output=json"],
            input_data=json.dumps(templated),
        )
    except subprocess.CalledProcessError as e:
        if e.stderr:
            sys.stderr.write(e.stderr)
        sys.exit(e.returncode)

    submitted_obj: object = json.loads(submitted_str)
    assert isinstance(submitted_obj, dict)
    return {str(k): v for k, v in submitted_obj.items()}


def execute_run(args: RunArgs) -> None:
    """Execute preflight checks, manifest templating, image publishing, and job admission."""
    run_id, launcher, run_name, target_cell = validate_run_preflight(args)
    workspace = os.environ.get("BUILD_WORKSPACE_DIRECTORY", "")
    if not workspace:
        sys.stderr.write("BUILD_WORKSPACE_DIRECTORY is required: run this target via bazel run\n")
        sys.exit(1)

    resolved_kubectl, resolved_manifest, resolved_publisher = _resolve_run_paths(args, workspace)
    kubeconfig_path = _resolve_cluster_kubeconfig(workspace, target_cell)
    target = ClusterTarget(
        kubectl=resolved_kubectl,
        kubeconfig=kubeconfig_path,
        target_cell=target_cell,
        team_namespace=args.team_namespace,
    )
    _verify_cluster_context(target)
    _verify_rbac_permission(target, resolved_manifest)

    dep_repo = resolve_deployment_repository(target, args.repository_path)
    deployment_refs = _publish_and_resolve_images(resolved_publisher, dep_repo)

    config = MutationConfig(
        workload=args.workload,
        run_name=run_name,
        run_id=run_id,
        launcher=launcher,
        team_namespace=args.team_namespace,
        virtual_cell=target_cell.removeprefix("cell-"),
        deployment_refs=deployment_refs,
    )
    submitted_json = _template_and_apply_manifest(target, resolved_manifest, config)
    check_ray_dashboard(submitted_json, run_name, target_cell, args.team_namespace)


def _resolve_cli_mode(argv: list[str], prog: str) -> str:
    if argv and argv[0] in {"publish", "run"}:
        return argv.pop(0)
    for mode in ("publish", "run"):
        if mode in prog:
            return mode
    if len(argv) == SUBCOMMAND_ARG_COUNT:
        return "run" if argv[4].startswith("team-") else "publish"
    return ""


def main() -> None:
    """Entry point dispatching to publish or run subcommand."""
    argv = sys.argv[1:]
    prog = Path(sys.argv[0]).name
    mode = _resolve_cli_mode(argv, prog)

    if mode == "publish":
        if len(argv) != SUBCOMMAND_ARG_COUNT:
            sys.stderr.write(
                "Usage: workload_cli publish <workload> <repository-path> <pusher> <index> <crane> <publication_mode>\n"
            )
            sys.exit(1)
        execute_publish(PublishArgs(*argv))
    elif mode == "run":
        if len(argv) != SUBCOMMAND_ARG_COUNT:
            sys.stderr.write(
                "Usage: workload_cli run <workload> <repository-path> <publisher> <manifest> <namespace> <kubectl>\n"
            )
            sys.exit(1)
        execute_run(RunArgs(*argv))
    else:
        sys.stderr.write("Unknown or missing workload subcommand: expected 'publish' or 'run'\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
