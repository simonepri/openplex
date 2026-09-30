#!/usr/bin/env python3
"""Executes live Kopia backup and restore workflows on Coder workspace PVCs to defend against data loss and permission corruption across workspace stop-start lifecycles."""

from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, cast

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

DEFAULT_TIMEOUT_SECONDS = 4_200
CLEANUP_RESERVE_SECONDS = 300
MAX_CATALOG_SNAPSHOTS = 50
MAX_SNAPSHOT_SIZE_BYTES = (1 << 63) - 1
POLL_SECONDS = 5.0
PROTECTED_CATALOG_SCHEMA = 3
SUFFIX = re.compile(r"^[a-z0-9]{8}$")
SELECTOR = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class AcceptanceError(Exception):
    """Report a credential-free local Kopia acceptance failure."""


@dataclass(frozen=True)
class Workspace:
    """Identify the Coder and Kubernetes state needed by the recovery drill."""

    name: str
    namespace: str
    owner_id: str
    pvc_uid: str


@dataclass(frozen=True)
class Catalog:
    """Hold one validated workspace snapshot ConfigMap projection."""

    name: str
    snapshots: tuple[dict[str, object], ...]

    @property
    def selectors(self) -> set[str]:
        selectors: set[str] = set()
        for snapshot in self.snapshots:
            selector = snapshot.get("selector")
            if not isinstance(selector, str) or not SELECTOR.fullmatch(selector):
                raise AcceptanceError("workspace snapshot catalog has an invalid selector")
            if selector in selectors:
                raise AcceptanceError("workspace snapshot catalog repeats a selector")
            selectors.add(selector)
        return selectors


class AcceptanceAccess(Protocol):
    """Describe the real boundaries used by the recovery drill."""

    def create(
        self,
        name: str,
        restore_selector: str | None,
        deadline: float,
    ) -> Workspace: ...

    def exec(self, name: str, command: Sequence[str], deadline: float) -> None: ...

    def transition(self, name: str, action: str, deadline: float) -> None: ...

    def indexes(self, namespace: str, deadline: float) -> tuple[Catalog, ...]: ...

    def protected_catalog(
        self,
        team: str,
        name: str,
        deadline: float,
    ) -> Catalog: ...


def remaining_seconds(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise AcceptanceError("local Kopia acceptance exceeded its deadline")
    return remaining


def mapping(value: object, subject: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise AcceptanceError(f"{subject} is invalid")
    return cast("dict[str, object]", value)


def string_field(value: dict[str, object], field: str, subject: str) -> str:
    result = value.get(field)
    if not isinstance(result, str) or not result:
        raise AcceptanceError(f"{subject} {field} is invalid")
    return result


class LocalAccess:
    """Reach Coder through its OIDC helper and catalogs through kubectl."""

    def __init__(self, repository_root: Path, executable: str = "kubectl") -> None:
        self.repository_root = repository_root
        self.executable = executable

    def _run(
        self,
        arguments: Sequence[str],
        deadline: float,
        *,
        subject: str,
    ) -> str:
        try:
            completed = subprocess.run(
                arguments,
                cwd=self.repository_root,
                check=False,
                capture_output=True,
                text=True,
                timeout=max(1.0, remaining_seconds(deadline)),
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise AcceptanceError(f"{subject} could not run") from error
        if completed.returncode != 0:
            raise AcceptanceError(f"{subject} failed")
        return completed.stdout

    def _workspace(
        self,
        arguments: Sequence[str],
        deadline: float,
        *,
        json_output: bool,
    ) -> dict[str, object] | None:
        timeout_seconds = math.floor(remaining_seconds(deadline))
        if timeout_seconds < 30:
            raise AcceptanceError("local Kopia acceptance has no time for a Coder operation")
        output = self._run(
            [
                "mise",
                "run",
                "local-coder-workspace",
                "--",
                "--timeout-seconds",
                str(min(timeout_seconds, 7_200)),
                *arguments,
            ],
            deadline,
            subject="local Coder workspace operation",
        )
        if not json_output:
            return None
        try:
            return mapping(json.loads(output), "local Coder workspace response")
        except json.JSONDecodeError as error:
            raise AcceptanceError("local Coder workspace response is invalid") from error

    def create(
        self,
        name: str,
        restore_selector: str | None,
        deadline: float,
    ) -> Workspace:
        arguments = ["create", name]
        if restore_selector is not None:
            arguments.extend(["--restore-selector", restore_selector])
        value = self._workspace(arguments, deadline, json_output=True)
        assert value is not None
        return Workspace(
            name=string_field(value, "name", "created workspace"),
            namespace=string_field(value, "namespace", "created workspace"),
            owner_id=string_field(value, "ownerId", "created workspace"),
            pvc_uid=string_field(value, "pvcUid", "created workspace"),
        )

    def exec(self, name: str, command: Sequence[str], deadline: float) -> None:
        self._workspace(["exec", name, "--", *command], deadline, json_output=False)

    def transition(self, name: str, action: str, deadline: float) -> None:
        self._workspace([action, name], deadline, json_output=True)

    def _kubectl_json(self, arguments: Sequence[str], deadline: float) -> dict[str, object]:
        output = self._run(
            [self.executable, *arguments],
            deadline,
            subject="workspace snapshot catalog query",
        )
        try:
            return mapping(json.loads(output), "workspace snapshot catalog response")
        except json.JSONDecodeError as error:
            raise AcceptanceError("workspace snapshot catalog response is invalid") from error

    @staticmethod
    def _catalog(
        value: dict[str, object],
        data_key: str,
        app_name: str,
        expected_schema: int,
    ) -> Catalog:
        metadata = mapping(value.get("metadata"), "workspace snapshot ConfigMap metadata")
        labels = mapping(metadata.get("labels"), "workspace snapshot ConfigMap labels")
        data = mapping(value.get("data"), "workspace snapshot ConfigMap data")
        if labels.get("app.kubernetes.io/name") != app_name:
            raise AcceptanceError("workspace snapshot ConfigMap identity is invalid")
        raw_record = data.get(data_key)
        if not isinstance(raw_record, str):
            raise AcceptanceError(f"workspace snapshot ConfigMap lacks {data_key}")
        try:
            record = mapping(json.loads(raw_record), f"workspace snapshot {data_key}")
        except json.JSONDecodeError as error:
            raise AcceptanceError(f"workspace snapshot {data_key} is invalid") from error
        snapshots = record.get("snapshots")
        if (
            record.get("schema") != expected_schema
            or not isinstance(snapshots, list)
            or len(snapshots) > MAX_CATALOG_SNAPSHOTS
            or not all(isinstance(snapshot, dict) for snapshot in snapshots)
        ):
            raise AcceptanceError(f"workspace snapshot {data_key} has an invalid contract")
        if expected_schema == PROTECTED_CATALOG_SCHEMA:
            for snapshot in snapshots:
                size_bytes = snapshot.get("sizeBytes")
                if (
                    not isinstance(size_bytes, int)
                    or isinstance(size_bytes, bool)
                    or not 0 <= size_bytes <= MAX_SNAPSHOT_SIZE_BYTES
                ):
                    raise AcceptanceError(f"workspace snapshot {data_key} has an invalid contract")
                packed_size_bytes = snapshot.get("packedSizeBytes")
                if packed_size_bytes is not None and (
                    not isinstance(packed_size_bytes, int)
                    or isinstance(packed_size_bytes, bool)
                    or not 0 <= packed_size_bytes <= MAX_SNAPSHOT_SIZE_BYTES
                ):
                    raise AcceptanceError(f"workspace snapshot {data_key} has an invalid contract")
        return Catalog(
            name=string_field(metadata, "name", "workspace snapshot ConfigMap"),
            snapshots=tuple(snapshots),
        )

    def indexes(self, namespace: str, deadline: float) -> tuple[Catalog, ...]:
        response = self._kubectl_json(
            [
                "get",
                "configmaps",
                f"--namespace={namespace}",
                "--selector=app.kubernetes.io/name=workspace-snapshot-index",
                "--output=json",
            ],
            deadline,
        )
        items = response.get("items")
        if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
            raise AcceptanceError("workspace snapshot index list is invalid")
        return tuple(
            self._catalog(item, "snapshots.json", "workspace-snapshot-index", 2) for item in items
        )

    def protected_catalog(
        self,
        team: str,
        name: str,
        deadline: float,
    ) -> Catalog:
        value = self._kubectl_json(
            [
                "get",
                f"configmap/{name}",
                f"--namespace=workspace-snapshots-{team}",
                "--output=json",
            ],
            deadline,
        )
        return self._catalog(
            value,
            "catalog.json",
            "workspace-snapshot-catalog",
            PROTECTED_CATALOG_SCHEMA,
        )


def workspace_team(namespace: str) -> str:
    suffix = "-workspaces"
    if not namespace.startswith("team-") or not namespace.endswith(suffix):
        raise AcceptanceError("workspace namespace is not a team workspaces lane")
    team = namespace.removeprefix("team-").removesuffix(suffix)
    if not re.fullmatch(r"[a-z0-9](?:[-a-z0-9]*[a-z0-9])?", team):
        raise AcceptanceError("workspace namespace has an invalid team")
    return team


def matching_snapshot(
    catalogs: Sequence[Catalog], workspace: str
) -> tuple[Catalog, dict[str, object]] | None:
    matches: list[tuple[Catalog, dict[str, object]]] = []
    for catalog in catalogs:
        for snapshot in catalog.snapshots:
            if snapshot.get("workspace") == workspace:
                matches.append((catalog, snapshot))
                continue
            display = snapshot.get("display")
            if isinstance(display, str):
                fields = display.split(" | ")
                if len(fields) == 5 and fields[3] == workspace:
                    matches.append((catalog, snapshot))
    if not matches:
        return None
    if len(matches) != 1:
        raise AcceptanceError("workspace snapshot catalog contains an ambiguous recovery point")
    return matches[0]


class KopiaAcceptance:
    """Orchestrate one append-only backup and replacement-PVC restore."""

    def __init__(
        self,
        access: AcceptanceAccess,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.access = access
        self.sleep = sleep

    def _wait_for_snapshot(
        self,
        namespace: str,
        workspace: str,
        baseline: set[str],
        deadline: float,
    ) -> tuple[Catalog, dict[str, object]]:
        while True:
            match = matching_snapshot(self.access.indexes(namespace, deadline), workspace)
            if match is not None:
                selector = match[1].get("selector")
                if isinstance(selector, str) and selector not in baseline:
                    return match
            remaining_seconds(deadline)
            self.sleep(POLL_SECONDS)

    def _cleanup(self, names: Sequence[str], deadline: float) -> None:
        for name in reversed(names):
            try:
                self.access.transition(name, "delete", deadline)
            except AcceptanceError:
                try:
                    self.access.transition(name, "stop", deadline)
                    self.access.transition(name, "delete", deadline)
                except AcceptanceError:
                    continue

    def run(self, suffix: str, deadline: float) -> dict[str, object]:
        if not SUFFIX.fullmatch(suffix):
            raise AcceptanceError(
                "acceptance suffix must contain exactly eight lowercase characters"
            )
        source_name = f"backup-src-{suffix}"
        restored_name = f"backup-restore-{suffix}"
        sentinel_path = f"/home/coder/kopia-acceptance-{suffix}.txt"
        sentinel = f"kopia-acceptance:{suffix}"
        operation_deadline = deadline - min(
            CLEANUP_RESERVE_SECONDS,
            remaining_seconds(deadline) / 2,
        )
        created: list[str] = []
        try:
            source = self.access.create(source_name, None, operation_deadline)
            created.append(source_name)
            indexes = self.access.indexes(source.namespace, operation_deadline)
            if len(indexes) > 1:
                raise AcceptanceError("local owner has more than one workspace snapshot index")
            baseline = indexes[0].selectors if indexes else set()
            if len(baseline) >= MAX_CATALOG_SNAPSHOTS:
                raise AcceptanceError(
                    "workspace snapshot catalog is full; refusing to evict recovery data"
                )
            self.access.exec(
                source_name,
                [
                    "/bin/sh",
                    "-ceu",
                    'umask 077; printf "%s" "$2" >"$1"',
                    "workspace-kopia-write",
                    sentinel_path,
                    sentinel,
                ],
                operation_deadline,
            )
            self.access.transition(source_name, "stop", operation_deadline)
            opaque_catalog, opaque_snapshot = self._wait_for_snapshot(
                source.namespace,
                source_name,
                baseline,
                operation_deadline,
            )
            selector = string_field(opaque_snapshot, "selector", "opaque workspace snapshot")
            team = workspace_team(source.namespace)
            protected = self.access.protected_catalog(team, opaque_catalog.name, operation_deadline)
            protected_match = matching_snapshot([protected], source_name)
            if protected_match is None:
                raise AcceptanceError(
                    "signed workspace snapshot is absent from the protected catalog"
                )
            protected_snapshot = protected_match[1]
            if (
                protected_snapshot.get("selector") != selector
                or mapping(protected_snapshot.get("origin"), "protected snapshot origin").get(
                    "path"
                )
                != "/var/lib/workspace"
            ):
                raise AcceptanceError("protected and opaque snapshot catalogs disagree")
            if not baseline.issubset(opaque_catalog.selectors):
                raise AcceptanceError(
                    "workspace snapshot publication removed existing recovery data"
                )
            self.access.transition(source_name, "delete", operation_deadline)
            created.remove(source_name)
            restored = self.access.create(restored_name, selector, operation_deadline)
            created.append(restored_name)
            if source.owner_id != restored.owner_id or source.pvc_uid == restored.pvc_uid:
                raise AcceptanceError(
                    "restored workspace did not use a replacement PVC for the same owner"
                )
            self.access.exec(
                restored_name,
                [
                    "/bin/sh",
                    "-ceu",
                    'test "$(cat "$1")" = "$2"',
                    "workspace-kopia-read",
                    sentinel_path,
                    sentinel,
                ],
                operation_deadline,
            )
            return {
                "baselineSelectorCount": len(baseline),
                "restoredPvcUid": restored.pvc_uid,
                "selector": selector,
                "sourcePvcUid": source.pvc_uid,
            }
        finally:
            self._cleanup(created, deadline)


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--suffix", required=True)
    value.add_argument("--timeout-seconds", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    return value


def run(arguments: Sequence[str] | None = None) -> int:
    options = parser().parse_args(arguments)
    if not 600 <= options.timeout_seconds <= 7_200:
        raise AcceptanceError("acceptance timeout must be between 600 and 7200 seconds")
    repository_root = Path(__file__).resolve().parents[4]
    KopiaAcceptance(LocalAccess(repository_root)).run(
        options.suffix,
        time.monotonic() + options.timeout_seconds,
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(run())
    except (AcceptanceError, OSError, ValueError) as error:
        message = error.args[0] if error.args else "local Kopia acceptance failed"
        raise SystemExit(f"Local Kopia acceptance failed: {message}") from None
