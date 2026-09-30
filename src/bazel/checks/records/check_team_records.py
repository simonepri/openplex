#!/usr/bin/env python3
"""Validate team definition files, check project record consistency, and emit generated index files."""

from __future__ import annotations

import argparse
import json
import operator
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

import jsonschema
import yaml

if TYPE_CHECKING:
    from collections.abc import Iterable

MIN_CODEOWNERS_PARTS = 2
MAX_ENCODED_IDENTIFIER_LEN = 127
MAX_DNS_LABEL_LEN = 63

DNS_LABEL = re.compile(r"^[a-z0-9](?:[-a-z0-9]*[a-z0-9])?$")
SOURCE_NAME = re.compile(r"^[a-z0-9]+(?:_[a-z0-9]+)*$")
TEAM_SLUG = re.compile(r"^[a-z][a-z0-9]{1,30}$")
DEV_SECRET_NAME_PATTERN = r"^[a-z][a-z0-9]*(_[a-z0-9]+)*$"
WORKLOAD_DELIVERY_MODES = frozenset({"promoted", "submitted"})
AVAILABILITY_CLASSES = frozenset({"be", "ha", "ma", "wa"})
# keep-sorted start
GPU_MODELS = frozenset({
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
# keep-sorted end
TPU_MODELS = frozenset({"v5e-2x2"})
ACCELERATOR_MODELS = GPU_MODELS | TPU_MODELS
TPU_CHIP_COUNTS = {"v5e-2x2": 4}
# LINT.IfChange(workspace-sidecar-capacity-reserve)
WORKSPACE_SIDECAR_CPU_MILLICORES = 35
WORKSPACE_SIDECAR_MEMORY_MIB = 96
# LINT.ThenChange(//src/infra/definitions/workspaces/templates/dev/locals.tf:workspace-sidecar-capacity-reserve)
WORKSPACE_MIN_STORAGE_GIB = 8
TEAM_SCHEMA = Path(__file__).resolve().parents[4] / "src/infra/definitions/teams/team.schema.json"
INFRASTRUCTURE_CONFIG_PATHS = (
    Path("src/infra/terraform/deployments/local/deployment.yaml"),
    Path("src/infra/terraform/deployments/research/deployment.yaml"),
)


def load_reserved_team_slugs() -> frozenset[str]:
    schema = json.loads(TEAM_SCHEMA.read_text(encoding="utf-8"))
    values = schema["$defs"]["reservedTeamSlug"]["enum"]
    if (
        not isinstance(values, list)
        or not values
        or not all(isinstance(value, str) for value in values)
    ):
        raise ValueError(
            f"{TEAM_SCHEMA}: $defs.reservedTeamSlug.enum must be a non-empty string list"
        )
    return frozenset(v for v in values if isinstance(v, str))


RESERVED_TEAM_SLUGS = load_reserved_team_slugs()
RESERVED_NAMESPACE_NAMES = RESERVED_TEAM_SLUGS | {
    # keep-sorted start
    "cert-manager",
    "cert-manager-system",
    "data-system",
    "dragonfly-system",
    "envoy-gateway-system",
    "external-dns",
    "external-dns-system",
    "gateway-system",
    "headlamp",
    "headlamp-system",
    "kube-system",
    "kueue-system",
    "local-path-storage",
    "observability-ingest",
    "operator-ui",
    "s3-gateway",
    "s3-system",
    # keep-sorted end
}


class TeamRecordError(ValueError):
    """A team or project record violates the team inventory contract."""


def _normalize_args(args: argparse.Namespace, root: Path) -> None:
    for attribute in (
        "write_index",
        "check_index",
        "write_scheduling_index",
        "check_scheduling_index",
        "write_workload_images",
        "check_workload_images",
        "write_codeowners",
        "check_codeowners",
        "codeowners",
    ):
        value = getattr(args, attribute)
        if value is not None and not value.is_absolute():
            setattr(args, attribute, root / value)


def _handle_index_outputs(
    args: argparse.Namespace, root: Path, team_paths: list[Path], index: dict[str, Any]
) -> None:
    if args.write_index is not None:
        write_index(args.write_index, index)
    if args.check_index is not None:
        check_index(args.check_index, index)
    if args.write_scheduling_index is not None or args.check_scheduling_index is not None:
        scheduling = scheduling_index(root, team_paths)
        if args.write_scheduling_index is not None:
            write_index(args.write_scheduling_index, scheduling)
        if args.check_scheduling_index is not None:
            check_index(args.check_scheduling_index, scheduling, "scheduling")
    if args.write_workload_images is not None or args.check_workload_images is not None:
        workload_images = workload_images_index(index.get("projects", []))
        if args.write_workload_images is not None:
            write_index(args.write_workload_images, workload_images)
        if args.check_workload_images is not None:
            check_index(args.check_workload_images, workload_images, "workload images")
    if args.write_codeowners is not None or args.check_codeowners is not None:
        target = args.write_codeowners or args.check_codeowners
        content = generate_codeowners(root, target)
        if args.write_codeowners is not None:
            write_codeowners(args.write_codeowners, content)
        if args.check_codeowners is not None:
            check_codeowners(args.check_codeowners, content)


def _execute(args: argparse.Namespace) -> None:
    root = args.root.resolve()
    _normalize_args(args, root)
    validate_dev_secret_schema_contract(root)
    validate_installation_schema(root)
    team_paths = args.team or sorted((root / "src/infra/definitions/teams").glob("*.yaml"))
    project_paths = args.project or discover_projects(root / "src")
    validate_project_schema(root, project_paths)
    index = validate_repository(root, team_paths, project_paths)
    if args.codeowners is not None:
        validate_codeowners(
            root,
            args.codeowners,
            codeowned_record_paths(root, team_paths, project_paths),
        )
    _handle_index_outputs(args, root, team_paths, index)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(os.environ.get("BUILD_WORKSPACE_DIRECTORY", Path.cwd())),
    )
    parser.add_argument("--team", action="append", type=Path, default=[])
    parser.add_argument("--project", action="append", type=Path, default=[])
    parser.add_argument(
        "--codeowners",
        type=Path,
        help="also require every record to match an owner-bearing entry here",
    )
    project_index_mode = parser.add_mutually_exclusive_group()
    project_index_mode.add_argument("--write-index", type=Path)
    project_index_mode.add_argument("--check-index", type=Path)
    scheduling_index_mode = parser.add_mutually_exclusive_group()
    scheduling_index_mode.add_argument("--write-scheduling-index", type=Path)
    scheduling_index_mode.add_argument("--check-scheduling-index", type=Path)
    workload_images_mode = parser.add_mutually_exclusive_group()
    workload_images_mode.add_argument("--write-workload-images", type=Path)
    workload_images_mode.add_argument("--check-workload-images", type=Path)
    codeowners_generate_mode = parser.add_mutually_exclusive_group()
    codeowners_generate_mode.add_argument("--write-codeowners", type=Path)
    codeowners_generate_mode.add_argument("--check-codeowners", type=Path)
    return parser


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()
    try:
        _execute(args)
    except (OSError, TeamRecordError, yaml.YAMLError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    else:
        return 0


def validate_installation_schema(root: Path) -> None:
    schema_path = root / "src/bazel/checks/records/installation.schema.json"
    if not schema_path.is_file():
        return
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    for rel_path in INFRASTRUCTURE_CONFIG_PATHS:
        path = root / rel_path
        if not path.is_file():
            continue
        record = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(record, dict) or "installation" not in record:
            continue
        try:
            jsonschema.validate(record["installation"], schema)
        except jsonschema.ValidationError as error:
            raise TeamRecordError(
                f"{path}: installation schema validation failed: {error.message}"
            ) from error


def validate_project_schema(root: Path, project_paths: list[Path]) -> None:
    schema = json.loads((root / "src/bazel/checks/records/project.schema.json").read_text())
    for path in project_paths:
        record = yaml.safe_load(path.read_text(encoding="utf-8"))
        try:
            jsonschema.validate(record, schema)
        except jsonschema.ValidationError as error:
            raise TeamRecordError(f"{path}: {error.message}") from error


def validate_codeowners(root: Path, codeowners: Path, owned_paths: list[Path]) -> None:
    """Every record must match an owner-bearing CODEOWNERS entry.

    Pattern semantics stay delegated to git check-ignore; reimplementing
    CODEOWNERS globbing here would drift from what GitHub resolves.
    """
    patterns = [
        line.split()[0]
        for line in codeowners.read_text(encoding="utf-8").splitlines()
        if len(line.split()) >= MIN_CODEOWNERS_PARTS and not line.lstrip().startswith("#")
    ]
    for nested in sorted(root.rglob("CODEOWNERS")):
        if nested.resolve() == codeowners.resolve():
            continue
        if any(part.startswith(".") for part in nested.parts):
            continue
        rel_dir = "/" + nested.parent.relative_to(root).as_posix()
        for line in nested.read_text(encoding="utf-8").splitlines():
            if len(line.split()) >= MIN_CODEOWNERS_PARTS and not line.lstrip().startswith("#"):
                pat = line.split()[0].lstrip("/")
                patterns.append(f"{rel_dir}/{pat}")

    if not patterns:
        return

    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".codeowners") as exclude_file:
        exclude_file.write("\n".join(patterns) + "\n")
        exclude_file.flush()
        relative = [str(path.relative_to(root)) for path in owned_paths]
        result = subprocess.run(
            [
                "git",
                "-c",
                f"core.excludesFile={exclude_file.name}",
                "check-ignore",
                "--no-index",
                "--verbose",
                "--stdin",
                "-z",
            ],
            cwd=root,
            input="\0".join(relative),
            capture_output=True,
            text=True,
            check=False,
        )
    # check-ignore exits 1 for "no path matched", which is a finding, not a crash.
    if result.returncode not in {0, 1}:
        raise TeamRecordError(f"git check-ignore failed: {result.stderr.strip()}")
    matched = set()
    fields = result.stdout.split("\0")
    # -z --verbose emits <source> <NUL> <linenum> <NUL> <pattern> <NUL> <path> <NUL>.
    for i in range(0, len(fields) - 3, 4):
        source = fields[i]
        pattern = fields[i + 2]
        path_match = fields[i + 3]
        if source == exclude_file.name and not pattern.startswith("!"):
            matched.add(path_match)
    for path in relative:
        if path not in matched:
            raise TeamRecordError(f"{path} is not covered by an owner-bearing CODEOWNERS entry")


def codeowned_record_paths(
    root: Path,
    team_paths: list[Path],
    project_paths: list[Path],
) -> list[Path]:
    """Return every authored authority whose ownership the gate protects."""
    return [
        *(root / path for path in INFRASTRUCTURE_CONFIG_PATHS),
        *team_paths,
        *project_paths,
    ]


def discover_projects(source_root: Path) -> list[Path]:
    return sorted(
        path
        for path in source_root.rglob("project.yaml")
        if ".tmp" not in path.parts and ".terraform" not in path.parts
    )


# LINT.IfChange(team-dev-secret-identifier-bounds)
def validate_dev_secret_schema_contract(root: Path) -> None:
    schema_paths = {
        "team definition": root / "src/infra/definitions/teams/team.schema.json",
        "team chart": root / "src/infra/argocd/components/team_lane/helm/values.schema.json",
        "team lane": root / "src/infra/argocd/components/team_namespace/helm/values.schema.json",
    }
    try:
        schemas = {
            name: json.loads(path.read_text(encoding="utf-8"))
            for name, path in schema_paths.items()
        }
        slug_fields = {
            "team definition": schemas["team definition"]["properties"]["slug"],
            "team chart": schemas["team chart"]["definitions"]["team"]["properties"]["slug"],
            "team lane": schemas["team lane"]["properties"]["team"]["properties"]["slug"],
        }
        secret_fields = {
            "team definition": schemas["team definition"]["properties"]["dev_secrets"]["items"],
            "team chart": schemas["team chart"]["definitions"]["team"]["properties"]["dev_secrets"][
                "items"
            ],
            "team lane": schemas["team lane"]["definitions"]["devSecretName"],
        }
    except (json.JSONDecodeError, KeyError, TypeError) as error:
        raise TeamRecordError(f"team schema identifier contract is unreadable: {error}") from error
    slug_contracts = {
        (field.get("pattern"), field.get("maxLength")) for field in slug_fields.values()
    }
    secret_contracts = {
        (field.get("pattern"), field.get("maxLength")) for field in secret_fields.values()
    }
    if len(slug_contracts) != 1:
        raise TeamRecordError("team schemas must use one slug pattern and maxLength")
    if len(secret_contracts) != 1:
        raise TeamRecordError("team schemas must use one dev-secret pattern and maxLength")

    slug_pattern, slug_max = slug_contracts.pop()
    secret_pattern, secret_max = secret_contracts.pop()
    if slug_pattern != TEAM_SLUG.pattern or not isinstance(slug_max, int):
        raise TeamRecordError("team schemas must enforce the canonical slug pattern and maxLength")
    if secret_pattern != DEV_SECRET_NAME_PATTERN or not isinstance(secret_max, int):
        raise TeamRecordError(
            "team schemas must enforce the canonical dev-secret pattern and maxLength"
        )
    encoded_max = len("cluster-teams-") + slug_max + 1 + secret_max
    if encoded_max != MAX_ENCODED_IDENTIFIER_LEN:
        raise TeamRecordError(
            "team schema identifier bounds must encode to the 127-character limit"
        )


# LINT.ThenChange(//src/infra/argocd/components/team_namespace/helm/templates/dev_secrets.yaml:team-dev-secret-identifier-bounds)


def validate_repository(
    root: Path, team_paths: Iterable[Path], project_paths: Iterable[Path]
) -> dict[str, Any]:
    root = root.resolve()
    teams = [load_record(path) for path in team_paths]
    projects = [load_record(path) for path in project_paths]
    if not teams:
        raise TeamRecordError("at least one team record is required")
    if not projects:
        raise TeamRecordError("at least one project record is required")

    validate_project_nesting(projects)
    projects_by_name = index_projects(projects)
    teams_by_slug = index_by_unique_field(teams, "slug", "team")
    validate_projects(projects_by_name)
    validate_teams(teams_by_slug)
    validate_claims(root, teams_by_slug, projects_by_name)
    return project_index(root, projects_by_name, teams_by_slug)


def load_record(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as stream:
        record = yaml.safe_load(stream)
    if not isinstance(record, dict):
        raise TeamRecordError(f"{path}: expected one YAML object")
    record["_path"] = path.resolve()
    return record


def index_by_unique_field(
    records: Iterable[dict[str, Any]], field: str, kind: str
) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    for record in records:
        path = record["_path"]
        value = record.get(field)
        if not isinstance(value, str) or not value:
            raise TeamRecordError(f"{path}: {kind} {field} must be a non-empty string")
        if value in indexed:
            raise TeamRecordError(
                f"{path}: duplicate {kind} {field} {value!r}; first declared by "
                f"{indexed[value]['_path']}"
            )
        indexed[value] = record
    return indexed


def project_root(path: Path) -> Path:
    if path.parent.name == "deployment":
        return path.parent.parent
    return path.parent


def index_projects(projects: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Index workload records by their package directory identity."""
    indexed: dict[str, dict[str, Any]] = {}
    for project in projects:
        path = project["_path"]
        if path.parent.name != "deployment":
            raise TeamRecordError(f"{path}: project.yaml must live in a deployment/ directory")
        name = project_root(path).name
        if len(name) > MAX_DNS_LABEL_LEN or SOURCE_NAME.fullmatch(name) is None:
            raise TeamRecordError(
                f"{path}: project directory must be a 1-63 character lowercase "
                "snake-case source name"
            )
        if name in indexed:
            raise TeamRecordError(
                f"{path}: duplicate project directory name {name!r}; first declared by "
                f"{indexed[name]['_path']}"
            )
        indexed[name] = project
    return indexed


def validate_project_nesting(projects: Iterable[dict[str, Any]]) -> None:
    roots = {project_root(record["_path"]) for record in projects}
    for root in roots:
        ancestor = root.parent
        while ancestor != ancestor.parent:
            if ancestor in roots:
                raise TeamRecordError(
                    f"{root / 'deployment/project.yaml'}: project is nested under "
                    f"{ancestor / 'deployment/project.yaml'}"
                )
            ancestor = ancestor.parent


def validate_projects(projects: dict[str, dict[str, Any]]) -> None:
    for name, record in projects.items():
        path = record["_path"]
        if "team" in record:
            raise TeamRecordError(
                f"{path}: projects cannot declare team; ownership comes from the team definitions"
            )
        derived_name = dns_name(name, path)
        if derived_name in RESERVED_NAMESPACE_NAMES:
            raise TeamRecordError(
                f"{path}: project name {name!r} derives reserved name {derived_name!r}"
            )

    for record in projects.values():
        delivery = record.get("delivery")
        if not isinstance(delivery, str) or delivery not in WORKLOAD_DELIVERY_MODES:
            raise TeamRecordError(
                f"{record['_path']}: delivery must be one of "
                f"{', '.join(sorted(WORKLOAD_DELIVERY_MODES))}"
            )
        validate_workload_delivery(record)


def validate_workload_delivery(project: dict[str, Any]) -> None:
    path = project["_path"]
    delivery = project["delivery"]
    stages = project.get("stages", [])
    if delivery == "submitted":
        if stages:
            raise TeamRecordError(f"{path}: submitted project cannot declare stages")
        return
    if not isinstance(stages, list) or not stages:
        raise TeamRecordError(f"{path}: promoted project requires stages")

    previous_stages: set[str] = set()
    for stage in stages:
        if not isinstance(stage, dict) or set(stage) != {"name", "promotion", "source"}:
            raise TeamRecordError(
                f"{path}: each stage must contain exactly name, promotion, and source"
            )
        name = stage["name"]
        if not isinstance(name, str) or not name:
            raise TeamRecordError(f"{path}: stage name must be a non-empty string")
        dns_name(name, path)
        if name in previous_stages:
            raise TeamRecordError(f"{path}: duplicate stage {name!r}")
        promotion = stage["promotion"]
        if not isinstance(promotion, str) or promotion not in {"automatic", "manual"}:
            raise TeamRecordError(f"{path}: stage {name!r} promotion must be automatic or manual")
        validate_stage_source(path, name, stage["source"], previous_stages)
        previous_stages.add(name)


def validate_stage_source(path: Path, name: str, source: object, previous_stages: set[str]) -> None:
    if not isinstance(source, dict):
        raise TeamRecordError(f"{path}: stage {name!r} source kind must be stage or warehouse")
    source_kind = source.get("kind")
    if not isinstance(source_kind, str) or source_kind not in {"stage", "warehouse"}:
        raise TeamRecordError(f"{path}: stage {name!r} source kind must be stage or warehouse")
    if source_kind == "warehouse":
        if set(source) != {"kind"}:
            raise TeamRecordError(f"{path}: stage {name!r} warehouse source cannot declare a name")
        return

    source_name = source.get("name")
    if (
        set(source) != {"kind", "name"}
        or not isinstance(source_name, str)
        or source_name not in previous_stages
    ):
        raise TeamRecordError(f"{path}: stage {name!r} source must name an earlier stage")


def project_stage_names(project: dict[str, Any]) -> list[str]:
    return [stage["name"] for stage in project.get("stages", [])]


def _validate_team_members(
    path: Path, members: list[Any] | None, seen_member_ids: dict[str, Path]
) -> None:
    for member in members or []:
        if not isinstance(member, dict) or "id" not in member:
            continue
        member_id = member["id"]
        if member_id in seen_member_ids:
            raise TeamRecordError(
                f"{path}: duplicate member id {member_id!r}; first declared by "
                f"{seen_member_ids[member_id]}"
            )
        seen_member_ids[member_id] = path


def validate_teams(teams: dict[str, dict[str, Any]]) -> None:
    seen_member_ids: dict[str, Path] = {}
    for slug, record in teams.items():
        path = record["_path"]
        if path.stem != slug:
            raise TeamRecordError(f"{path}: slug must match its filename")
        if TEAM_SLUG.fullmatch(slug) is None:
            raise TeamRecordError(
                f"{path}: team slug must be a lowercase alphanumeric token of 2-31 characters"
            )
        if slug in RESERVED_TEAM_SLUGS:
            raise TeamRecordError(f"{path}: team slug {slug!r} is reserved")
        _validate_team_members(path, record.get("members"), seen_member_ids)


def validate_claims(
    root: Path,
    teams: dict[str, dict[str, Any]],
    projects: dict[str, dict[str, Any]],
) -> None:
    claims: dict[str, str] = {}
    for slug, team in teams.items():
        path = team["_path"]
        project_names = team.get("projects")
        if not isinstance(project_names, list) or not project_names:
            raise TeamRecordError(f"{path}: projects must be a non-empty list")
        for name in project_names:
            if not isinstance(name, str):
                raise TeamRecordError(f"{path}: project claims must be strings")
            project = projects.get(name)
            if project is None:
                raise TeamRecordError(f"{path}: claimed project {name!r} does not exist")
            if name in claims:
                raise TeamRecordError(
                    f"{path}: project {name!r} is already claimed by team {claims[name]!r}"
                )
            claims[name] = slug
            validate_project_manifests(root, project)

    unclaimed = sorted(set(projects) - set(claims))
    if unclaimed:
        raise TeamRecordError(
            "workload projects must be claimed by exactly one team: " + ", ".join(unclaimed)
        )
    validate_derived_namespaces(teams, projects)


def validate_derived_namespaces(
    teams: dict[str, dict[str, Any]], _projects: dict[str, dict[str, Any]]
) -> list[str]:
    derived: dict[str, str] = {}
    for slug, team in teams.items():
        workloads_ns = derived_namespace(slug, "workloads", team["_path"])
        previous = derived.get(workloads_ns)
        if previous is not None and previous != slug:
            raise TeamRecordError(
                f"{team['_path']}: derived namespace {workloads_ns!r} collides with team {previous!r}"
            )
        derived[workloads_ns] = slug

        if team.get("workspaces", True):
            workspaces_ns = derived_namespace(slug, "workspaces", team["_path"])
            previous = derived.get(workspaces_ns)
            if previous is not None and previous != slug:
                raise TeamRecordError(
                    f"{team['_path']}: derived namespace {workspaces_ns!r} collides with team {previous!r}"
                )
            derived[workspaces_ns] = slug
    return sorted(derived)


def derived_namespace(slug: str, suffix: str, path: Path) -> str:
    return dns_name(f"team-{slug}-{suffix}", path)


def validate_project_manifests(root: Path, project: dict[str, Any]) -> None:
    path = project["_path"]
    if path.parent.name != "deployment":
        raise TeamRecordError(f"{path}: project.yaml must live in a deployment/ directory")
    root_dir = project_root(path)
    deployment = root_dir / "deployment" / "kustomization.yaml"
    if not deployment.is_file():
        raise TeamRecordError(f"{path}: claimed project requires {relative(root, deployment)}")
    root_project_yaml = root_dir / "project.yaml"
    if root_project_yaml.exists():
        raise TeamRecordError(
            f"{path}: project root must not contain {relative(root, root_project_yaml)}"
        )
    root_kustomization = root_dir / "kustomization.yaml"
    if root_kustomization.exists():
        raise TeamRecordError(
            f"{path}: project root must not contain {relative(root, root_kustomization)}"
        )

    expected_stages = set(project_stage_names(project))
    deployment_dir = root_dir / "deployment"
    for stage in expected_stages:
        overlay = deployment_dir / stage / "kustomization.yaml"
        if overlay.parent.is_dir() and not overlay.is_file():
            raise TeamRecordError(f"{path}: stage {stage!r} requires {relative(root, overlay)}")


def project_index(
    root: Path,
    projects: dict[str, dict[str, Any]],
    teams: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    claims = {project: slug for slug, team in teams.items() for project in team["projects"]}
    return {
        "version": 3,
        "projects": [
            {
                "delivery": record["delivery"],
                "deployments": project_deployments(root, record),
                "name": name,
                "path": relative(root, project_root(record["_path"])),
                "team": claims[name],
            }
            for name, record in sorted(projects.items())
        ],
    }


def _validate_cluster_bb(labels: dict[str, Any], prefix: str) -> None:
    mode = labels.get("buildbuddy.io/mode", "community")
    proxy = labels.get("buildbuddy.io/enterprise-proxy", "disabled")
    executors = labels.get("buildbuddy.io/executors", "none")

    if mode not in {"community", "cloud"}:
        raise TeamRecordError(
            f"{prefix}invalid mode {mode!r} for 'buildbuddy.io/mode'; must be 'community' or 'cloud'"
        )
    if proxy not in {"disabled", "enabled"}:
        raise TeamRecordError(
            f"{prefix}invalid proxy value {proxy!r} for 'buildbuddy.io/enterprise-proxy'; must be 'disabled' or 'enabled'"
        )
    if mode == "community":
        if proxy == "enabled":
            raise TeamRecordError(
                f"{prefix}'buildbuddy.io/enterprise-proxy: enabled' is invalid when 'buildbuddy.io/mode' is 'community'"
            )
        if executors != "none":
            raise TeamRecordError(f"{prefix}self-hosted executors require Cloud mode")
    elif mode == "cloud" and executors not in {"none", "gpu", "cpu", "all"}:
        raise TeamRecordError(
            f"{prefix}invalid executors value {executors!r} for 'buildbuddy.io/executors'; must be 'none', 'gpu', 'cpu', or 'all'"
        )


def validate_cluster_buildbuddy_capabilities(
    clusters: dict[str, Any], path: Path | None = None
) -> None:
    """Validate BuildBuddy capability labels across cluster records."""
    for _, cluster in sorted(clusters.items()):
        if not isinstance(cluster, dict):
            continue
        cluster_path = cluster.get("_path", path)
        prefix = f"{cluster_path}: " if cluster_path else ""
        labels = cluster.get("labels")
        if not isinstance(labels, dict):
            labels = {}
        _validate_cluster_bb(labels, prefix)


def scheduling_index(
    root: Path,
    team_paths: Iterable[Path],
    cell_paths: Iterable[Path] | None = None,
) -> dict[str, Any]:
    """Project team quota into the exact cells selected by each team."""
    teams = index_by_unique_field(
        (load_record(path) for path in team_paths),
        "slug",
        "team",
    )
    if cell_paths is None:
        local_dep = root / "src/infra/terraform/deployments/local/deployment.yaml"
        prod_dep = root / "src/infra/terraform/deployments/research/deployment.yaml"
        if local_dep.is_file() and prod_dep.is_file():
            clusters = {}
            for path in (local_dep, prod_dep):
                doc = load_record(path)
                for name, data in doc.get("clusters", {}).items():
                    clusters[name] = {**data, "name": name, "_path": path}
            validate_cluster_buildbuddy_capabilities(clusters, local_dep)
            cells = clusters
        else:
            clusters_path = root / "src/infra/terraform/deployments/local/deployment.yaml"
            clusters_doc = load_record(clusters_path)
            clusters = clusters_doc.get("clusters", {})
            if not isinstance(clusters, dict):
                raise TeamRecordError(f"{clusters_path}: clusters must be an object")
            validate_cluster_buildbuddy_capabilities(clusters, clusters_path)
            cells = {
                name: {**data, "name": name, "_path": clusters_path}
                for name, data in clusters.items()
            }
    else:
        cells = index_by_unique_field(
            (load_record(path) for path in cell_paths),
            "name",
            "cell",
        )
        validate_cluster_buildbuddy_capabilities(cells)
    schedulable_cells = {
        name: cell for name, cell in sorted(cells.items()) if cell.get("role") == "cell"
    }
    cell_labels: dict[str, dict[str, Any]] = {}
    for name, cell in schedulable_cells.items():
        labels = cell.get("labels")
        if not isinstance(labels, dict):
            raise TeamRecordError(f"{cell['_path']}: cell labels must be an object")
        cell_labels[name] = labels
    selected_teams = {
        name: [
            (slug, team)
            for slug, team in sorted(teams.items())
            if selector_matches(team.get("cells"), cell_labels[name], team["_path"])
        ]
        for name in schedulable_cells
    }
    selected_cells_by_team = {
        slug: {
            name
            for name, selected in selected_teams.items()
            if any(selected_slug == slug for selected_slug, _ in selected)
        }
        for slug in teams
    }
    for slug, team in teams.items():
        validate_team_quota(
            team,
            set(schedulable_cells),
            selected_cells_by_team[slug],
        )

    selected: dict[str, list[dict[str, Any]]] = {}
    for name, cell in schedulable_cells.items():
        selected[name] = [
            {
                "quota": team_quota(team, name),
                "slug": slug,
            }
            for slug, team in selected_teams[name]
        ]
        validate_cell_team_quota(cell, selected[name])
    if not selected:
        raise TeamRecordError("at least one role=cell record is required for scheduling")
    return {
        "version": 1,
        "cells": [{"name": name, "teams": selected[name]} for name in sorted(selected)],
    }


def _match_single_expression(
    expression: dict[str, Any], labels: dict[str, Any], path: Path
) -> bool:
    key = expression.get("key")
    operator = expression.get("operator")
    values = expression.get("values", [])
    if not isinstance(key, str) or not isinstance(values, list):
        raise TeamRecordError(f"{path}: cells.matchExpressions entry is malformed")
    present = key in labels
    if operator == "In":
        return present and labels[key] in values
    if operator == "NotIn":
        return not present or labels[key] not in values
    if operator == "Exists":
        return present
    if operator == "DoesNotExist":
        return not present
    raise TeamRecordError(f"{path}: unsupported cells selector operator {operator!r}")


def selector_matches(selector: object, labels: dict[str, Any], path: Path) -> bool:
    if not isinstance(selector, dict):
        raise TeamRecordError(f"{path}: cells must be a label selector object")
    match_labels = selector.get("matchLabels", {})
    expressions = selector.get("matchExpressions", [])
    if not isinstance(match_labels, dict) or not isinstance(expressions, list):
        raise TeamRecordError(f"{path}: cells must be a label selector object")
    if any(labels.get(key) != value for key, value in match_labels.items()):
        return False
    for expression in expressions:
        if not isinstance(expression, dict):
            raise TeamRecordError(f"{path}: cells.matchExpressions entries must be objects")
        if not _match_single_expression(expression, labels, path):
            return False
    return True


def validate_team_quota(
    team: dict[str, Any],
    existing_cells: set[str],
    selected_cells: set[str],
) -> None:
    path = team["_path"]
    quota = team.get("quota")
    if (
        not isinstance(quota, dict)
        or "classes" not in quota
        or not set(quota).issubset({"cells", "classes"})
    ):
        raise TeamRecordError(f"{path}: quota must contain classes and optional cells")
    validate_quota_classes(path, quota["classes"], "quota.classes")
    cell_overrides = quota.get("cells", {})
    if not isinstance(cell_overrides, dict):
        raise TeamRecordError(f"{path}: quota.cells must be an object")
    unknown_cells = sorted(set(cell_overrides) - existing_cells)
    if unknown_cells:
        raise TeamRecordError(
            f"{path}: quota.cells references unknown cells: {', '.join(unknown_cells)}"
        )
    unselected_cells = sorted(set(cell_overrides) - selected_cells)
    if unselected_cells:
        raise TeamRecordError(
            f"{path}: quota.cells overrides cells not selected by the team: "
            f"{', '.join(unselected_cells)}"
        )
    expected_classes = set(quota["classes"])
    for cell_name, override in cell_overrides.items():
        if not isinstance(override, dict) or set(override) != {"classes"}:
            raise TeamRecordError(f"{path}: quota.cells.{cell_name} must contain exactly classes")
        classes = override["classes"]
        validate_quota_classes(path, classes, f"quota.cells.{cell_name}.classes")
        if set(classes) != expected_classes:
            raise TeamRecordError(
                f"{path}: quota.cells.{cell_name}.classes must have the same "
                "availability classes as quota.classes"
            )


def extract_accelerators(path: Path, resources: dict[str, Any], location: str) -> dict[str, int]:
    accelerators: dict[str, int] = {}
    if "accelerators" in resources:
        acc_dict = resources["accelerators"]
        if not isinstance(acc_dict, dict):
            raise TeamRecordError(f"{path}: {location}.accelerators must be an object")
        for model, qty in acc_dict.items():
            if model not in ACCELERATOR_MODELS:
                raise TeamRecordError(
                    f"{path}: {location}.accelerators has unsupported accelerator {model!r}"
                )
            accelerators[model] = quota_quantity(path, model, qty)
    for key, val in resources.items():
        if key in ACCELERATOR_MODELS:
            if key in accelerators:
                raise TeamRecordError(
                    f"{path}: {location} duplicate accelerator specification for {key!r}"
                )
            accelerators[key] = quota_quantity(path, key, val)
    return accelerators


def _validate_accelerator_quantities(
    path: Path, location: str, name: str, quantity: object
) -> None:
    if not isinstance(quantity, dict):
        raise TeamRecordError(f"{path}: {location}.{name}.accelerators must be an object")
    for model, model_qty in quantity.items():
        if model not in ACCELERATOR_MODELS:
            raise TeamRecordError(
                f"{path}: {location}.{name}.accelerators has unsupported accelerator {model!r}"
            )
        quota_quantity(path, model, model_qty)


def _validate_single_quota_class(
    path: Path, location: str, name: str, resources: dict[str, Any]
) -> None:
    if name not in AVAILABILITY_CLASSES or not isinstance(resources, dict):
        raise TeamRecordError(f"{path}: {location} has an invalid availability class")
    if not {"cpu", "memory"}.issubset(resources):
        raise TeamRecordError(f"{path}: {location}.{name} requires cpu and memory")
    for resource, quantity in resources.items():
        if resource == "accelerators":
            _validate_accelerator_quantities(path, location, name, quantity)
            continue
        parsed = quota_quantity(path, resource, quantity)
        if resource == "google.com/tpu" and parsed % 4 != 0:
            raise TeamRecordError(
                f"{path}: {location}.{name}.google.com/tpu must be a multiple of four"
            )
    extract_accelerators(path, resources, f"{location}.{name}")


def validate_quota_classes(path: Path, classes: object, location: str) -> None:
    if not isinstance(classes, dict) or not classes:
        raise TeamRecordError(f"{path}: {location} must be a non-empty object")
    for name, resources in classes.items():
        _validate_single_quota_class(path, location, name, resources)


def team_quota(team: dict[str, Any], cell_name: str) -> dict[str, Any]:
    quota = team["quota"]
    cell_quota = quota.get("cells", {}).get(cell_name)
    return {"classes": quota["classes"] if cell_quota is None else cell_quota["classes"]}


def _check_accelerators_for_class(
    path: Path,
    team: dict[str, Any],
    cell: dict[str, Any],
    availability_class: str,
) -> None:
    cell_compute = cell.get("compute", {})
    gpu_classes = cell_compute.get("gpu_classes") or {}
    tpu_classes = cell_compute.get("tpu_classes") or {}
    team_resources = team["quota"]["classes"].get(availability_class, {})
    accelerators = extract_accelerators(path, team_resources, f"quota.classes.{availability_class}")
    for model, qty in accelerators.items():
        if qty <= 0:
            continue
        if model in GPU_MODELS and model not in gpu_classes:
            raise TeamRecordError(
                f"{path}: selected team {team['slug']} {availability_class} accelerator {model!r} "
                f"is not offered by {cell['name']}"
            )
        if model in TPU_MODELS and model not in tpu_classes:
            raise TeamRecordError(
                f"{path}: selected team {team['slug']} {availability_class} accelerator {model!r} "
                f"is not offered by {cell['name']}"
            )


def validate_team_accelerator_offerings(cell: dict[str, Any], teams: list[dict[str, Any]]) -> None:
    path = cell["_path"]
    for team in teams:
        for availability_class in ("be", "ha", "ma", "wa"):
            _check_accelerators_for_class(path, team, cell, availability_class)


def allocated_class_resource(
    path: Path, teams: list[dict[str, Any]], availability_class: str, resource: str
) -> int:
    if resource == "nvidia.com/gpu":
        return sum(
            quota_quantity(
                path,
                "nvidia.com/gpu",
                team["quota"]["classes"].get(availability_class, {}).get("nvidia.com/gpu", "0"),
            )
            + sum(
                qty
                for model, qty in extract_accelerators(
                    path,
                    team["quota"]["classes"].get(availability_class, {}),
                    f"quota.classes.{availability_class}",
                ).items()
                if model in GPU_MODELS
            )
            for team in teams
        )
    if resource == "google.com/tpu":
        return sum(
            quota_quantity(
                path,
                "google.com/tpu",
                team["quota"]["classes"].get(availability_class, {}).get("google.com/tpu", "0"),
            )
            + sum(
                qty * TPU_CHIP_COUNTS.get(model, 4)
                for model, qty in extract_accelerators(
                    path,
                    team["quota"]["classes"].get(availability_class, {}),
                    f"quota.classes.{availability_class}",
                ).items()
                if model in TPU_MODELS
            )
            for team in teams
        )
    return sum(
        quota_quantity(
            path,
            resource,
            team["quota"]["classes"].get(availability_class, {}).get(resource, "0"),
        )
        for team in teams
    )


def validate_cell_team_quota(cell: dict[str, Any], teams: list[dict[str, Any]]) -> None:
    path = cell["_path"]
    floors = cell.get("compute", {}).get("floors")
    if not isinstance(floors, dict):
        raise TeamRecordError(f"{path}: role=cell record requires compute.floors")
    validate_team_accelerator_offerings(cell, teams)
    for availability_class in ("be", "ha", "ma", "wa"):
        floor = floors.get(availability_class)
        if not isinstance(floor, dict):
            raise TeamRecordError(f"{path}: compute.floors.{availability_class} must be an object")
        for resource in ("cpu", "memory", "nvidia.com/gpu", "google.com/tpu"):
            allocated = allocated_class_resource(path, teams, availability_class, resource)
            available = quota_quantity(path, resource, floor.get(resource, "0"))
            if allocated > available:
                raise TeamRecordError(
                    f"{path}: selected team {availability_class} {resource} quota "
                    f"{allocated} exceeds class floor {available}"
                )
    workspace_default = default_workspace_quota(cell)
    if workspace_default is None:
        return
    for team in teams:
        classes = team["quota"]["classes"]
        ha = classes.get("ha", {})
        cpu = quota_quantity(path, "cpu", ha.get("cpu", "0"))
        memory = quota_quantity(path, "memory", ha.get("memory", "0"))
        if cpu < workspace_default["cpu"] or memory < workspace_default["memory"]:
            raise TeamRecordError(
                f"{path}: selected team {team['slug']} HA quota must provide at least "
                f"{workspace_default['cpu']} CPU and {workspace_default['memory']}Gi memory "
                "for one default workspace including its sidecars"
            )


def _is_positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def default_workspace_quota(cell: dict[str, Any]) -> dict[str, int] | None:
    workspaces = cell.get("workspaces")
    if not isinstance(workspaces, dict) or not workspaces.get("enabled", False):
        return None
    envelope = workspaces.get("resource_envelope")
    if not isinstance(envelope, dict):
        raise TeamRecordError(
            f"{cell['_path']}: workspace-enabled cell requires workspaces.resource_envelope"
        )
    try:
        cpu = envelope["cpu"]["default"]
        memory = envelope["memory_gib"]["default"]
        storage_min = envelope["storage_gib"]["min"]
    except (KeyError, TypeError) as error:
        raise TeamRecordError(
            f"{cell['_path']}: workspace envelope requires CPU and memory defaults "
            "and a storage minimum"
        ) from error
    if not _is_positive_int(cpu) or not _is_positive_int(memory):
        raise TeamRecordError(
            f"{cell['_path']}: workspace CPU and memory defaults must be positive whole numbers"
        )
    if (
        not isinstance(storage_min, int)
        or isinstance(storage_min, bool)
        or storage_min < WORKSPACE_MIN_STORAGE_GIB
    ):
        raise TeamRecordError(
            f"{cell['_path']}: workspace storage minimum must be at least "
            f"{WORKSPACE_MIN_STORAGE_GIB}Gi"
        )
    return {
        "cpu": (cpu * 1000 + WORKSPACE_SIDECAR_CPU_MILLICORES + 999) // 1000,
        "memory": (memory * 1024 + WORKSPACE_SIDECAR_MEMORY_MIB + 1023) // 1024,
    }


def quota_quantity(path: Path, resource: str, value: object) -> int:
    if (
        resource not in {"cpu", "memory", "nvidia.com/gpu", "google.com/tpu"}
        and resource not in ACCELERATOR_MODELS
    ):
        raise TeamRecordError(f"{path}: unsupported quota resource {resource!r}")
    if not isinstance(value, str):
        raise TeamRecordError(f"{path}: {resource} quota must be a string")
    if resource == "memory":
        if value == "0":
            return 0
        if re.fullmatch(r"[1-9][0-9]*Gi", value) is None:
            raise TeamRecordError(f"{path}: memory quota must be whole Gi")
        return int(value.removesuffix("Gi"))
    if re.fullmatch(r"[0-9]+", value) is None:
        raise TeamRecordError(f"{path}: {resource} quota must be a whole quantity")
    return int(value)


def workload_images_index(projects: list[dict[str, Any]]) -> dict[str, Any]:
    images = []
    for p in projects:
        name = p["name"]
        path = p["path"]
        source_name = name.replace("_", "-")
        images.append({
            "manifestRepository": f"registry.invalid/workloads/{source_name}",
            "package": path,
            "repository": path,
            "sourceName": source_name,
            "target": f"//{path}:publish",
        })
    return {"images": sorted(images, key=operator.itemgetter("sourceName")), "version": 1}


def project_deployments(root: Path, project: dict[str, Any]) -> list[dict[str, Any]]:
    root_dir = project_root(project["_path"])
    project_path = relative(root, root_dir)
    stages = project.get("stages", [])
    if not stages:
        return [{"path": f"{project_path}/deployment"}]
    deployments = []
    for stage in stages:
        stage_name = stage["name"]
        stage_dir = root_dir / "deployment" / stage_name / "kustomization.yaml"
        if stage_dir.is_file():
            path = f"{project_path}/deployment/{stage_name}"
        else:
            path = f"{project_path}/deployment"
        deployments.append({
            "path": path,
            "promotion": stage["promotion"],
            "source": stage["source"],
            "stage": stage_name,
        })
    return deployments


def dns_name(value: str, path: Path) -> str:
    derived = value.replace("_", "-")
    if len(derived) > MAX_DNS_LABEL_LEN or DNS_LABEL.fullmatch(derived) is None:
        raise TeamRecordError(f"{path}: {value!r} does not derive a valid DNS label")
    return derived


def relative(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root).as_posix()
    except ValueError as error:
        raise TeamRecordError(f"{path}: record must live under {root}") from error


def serialized_index(index: dict[str, Any]) -> str:
    return json.dumps(index, indent=2, sort_keys=True) + "\n"


def write_index(path: Path, index: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(serialized_index(index), encoding="utf-8")


def check_index(path: Path, index: dict[str, Any], kind: str = "project") -> None:
    expected = serialized_index(index)
    try:
        actual = path.read_text(encoding="utf-8")
    except FileNotFoundError as error:
        raise TeamRecordError(f"{path}: generated {kind} index is missing") from error
    if actual != expected:
        raise TeamRecordError(
            f"{path}: generated {kind} index is stale; regenerate it from source records"
        )


def generate_codeowners(root: Path, target: Path | None = None) -> str:
    """Compile nested CODEOWNERS files into GitHub's root CODEOWNERS syntax."""
    lines = [
        "# Generated file; do not edit.",
        "# Source: nested CODEOWNERS files throughout the repository.",
    ]
    target_resolved = target.resolve() if target and target.exists() else None
    nested_files: list[Path] = []
    for nested in sorted(root.rglob("CODEOWNERS")):
        if target_resolved and nested.resolve() == target_resolved:
            continue
        try:
            rel = nested.relative_to(root).as_posix()
        except ValueError:
            continue
        if rel in {"CODEOWNERS", ".github/CODEOWNERS", "docs/CODEOWNERS"}:
            continue
        if any(part.startswith(".") for part in nested.parts):
            continue
        nested_files.append(nested)

    for nested in nested_files:
        rel_dir = "/" + nested.parent.relative_to(root).as_posix()
        file_lines: list[str] = []
        for raw_line in nested.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) < MIN_CODEOWNERS_PARTS:
                continue
            pat = parts[0].lstrip("/")
            owners = " ".join(parts[1:])
            rule_path = f"{rel_dir}/{pat}" if pat else f"{rel_dir}/"
            file_lines.append(f"{rule_path} {owners}")
        if file_lines:
            lines.extend(("", f"# {nested.relative_to(root).as_posix()}", *file_lines))

    lines.extend(("", ""))
    return "\n".join(lines)


def write_codeowners(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def check_codeowners(path: Path, content: str) -> None:
    try:
        actual = path.read_text(encoding="utf-8")
    except FileNotFoundError as error:
        raise TeamRecordError(f"{path}: generated CODEOWNERS is missing") from error
    if actual != content:
        raise TeamRecordError(f"{path}: generated CODEOWNERS is stale; regenerate it via //:fix")


if __name__ == "__main__":
    raise SystemExit(main())
