#!/usr/bin/env python3
"""Tests Kopia acceptance sequencing, payload preservation, and snapshot boundary denial logic offline to defend the local backup verification harness."""

from __future__ import annotations

import unittest
from typing import TYPE_CHECKING

import kopia_acceptance

if TYPE_CHECKING:
    from collections.abc import Sequence

OWNER_ID = "8826ee2e-7933-4665-aef2-2393f84a0d05"
SOURCE_PVC_ID = "497f6eca-6276-4993-bfeb-53cbbbba6f08"
RESTORED_PVC_ID = "67804749-64e2-4033-86a2-1dfa08e15411"


def opaque_catalog(*selectors: str) -> kopia_acceptance.Catalog:
    snapshots: tuple[dict[str, object], ...] = tuple(
        {
            "display": (
                "2026-09-06T00:00:00Z | ops@machine:/var/lib/workspace | "
                f"cell-eaws-lh1 | {'backup-src-deadbeef' if selector == 'new-selector' else 'old-workspace'} | lineage"
            ),
            "selector": selector,
        }
        for selector in selectors
    )
    return kopia_acceptance.Catalog("workspace-snapshots-principal", snapshots)


def catalog_config_map(
    data_key: str,
    app_name: str,
    schema: int,
    snapshots: list[dict[str, object]],
) -> dict[str, object]:
    return {
        "metadata": {
            "labels": {"app.kubernetes.io/name": app_name},
            "name": "workspace-snapshots-principal",
        },
        "data": {data_key: kopia_acceptance.json.dumps({"schema": schema, "snapshots": snapshots})},
    }


class FakeAccess:
    def __init__(self, baseline: kopia_acceptance.Catalog | None) -> None:
        self.baseline = baseline
        self.stopped = False
        self.events: list[tuple[str, object]] = []

    def create(
        self,
        name: str,
        restore_selector: str | None,
        deadline: float,
    ) -> kopia_acceptance.Workspace:
        self.events.append(("create", (name, restore_selector)))
        return kopia_acceptance.Workspace(
            name,
            "team-examples-workspaces",
            OWNER_ID,
            RESTORED_PVC_ID if restore_selector else SOURCE_PVC_ID,
        )

    def exec(self, name: str, command: Sequence[str], deadline: float) -> None:
        self.events.append(("exec", (name, tuple(command))))

    def transition(self, name: str, action: str, deadline: float) -> None:
        self.events.append((action, name))
        if action == "stop" and name.startswith("backup-src-"):
            self.stopped = True

    def indexes(
        self,
        namespace: str,
        deadline: float,
    ) -> tuple[kopia_acceptance.Catalog, ...]:
        if self.stopped:
            selectors = sorted(
                (self.baseline.selectors if self.baseline else set()) | {"new-selector"}
            )
            return (opaque_catalog(*selectors),)
        return (self.baseline,) if self.baseline else ()

    def protected_catalog(
        self,
        team: str,
        name: str,
        deadline: float,
    ) -> kopia_acceptance.Catalog:
        self.events.append(("protected", (team, name)))
        return kopia_acceptance.Catalog(
            name,
            (
                {
                    "origin": {"path": "/var/lib/workspace"},
                    "selector": "new-selector",
                    "sizeBytes": 1_572_864,
                    "workspace": "backup-src-deadbeef",
                },
            ),
        )


class KopiaAcceptanceTest(unittest.TestCase):
    def test_catalog_contracts_keep_opaque_v2_and_accept_sized_protected_v3(self) -> None:
        opaque = kopia_acceptance.LocalAccess._catalog(
            catalog_config_map(
                "snapshots.json",
                "workspace-snapshot-index",
                2,
                [{"selector": "opaque-selector"}],
            ),
            "snapshots.json",
            "workspace-snapshot-index",
            2,
        )
        protected = kopia_acceptance.LocalAccess._catalog(
            catalog_config_map(
                "catalog.json",
                "workspace-snapshot-catalog",
                3,
                [{"selector": "protected-selector", "sizeBytes": 1_572_864}],
            ),
            "catalog.json",
            "workspace-snapshot-catalog",
            3,
        )

        self.assertEqual(opaque.selectors, {"opaque-selector"})
        self.assertEqual(protected.selectors, {"protected-selector"})

    def test_protected_catalog_rejects_schema_v2(self) -> None:
        with self.assertRaisesRegex(kopia_acceptance.AcceptanceError, "invalid contract"):
            kopia_acceptance.LocalAccess._catalog(
                catalog_config_map(
                    "catalog.json",
                    "workspace-snapshot-catalog",
                    2,
                    [{"selector": "protected-selector", "sizeBytes": 1_572_864}],
                ),
                "catalog.json",
                "workspace-snapshot-catalog",
                3,
            )

    def test_protected_catalog_rejects_invalid_snapshot_sizes(self) -> None:
        for size_bytes in (None, -1, True, 1 << 63, "1572864"):
            with (
                self.subTest(size_bytes=size_bytes),
                self.assertRaisesRegex(
                    kopia_acceptance.AcceptanceError,
                    "invalid contract",
                ),
            ):
                kopia_acceptance.LocalAccess._catalog(
                    catalog_config_map(
                        "catalog.json",
                        "workspace-snapshot-catalog",
                        3,
                        [{"selector": "protected-selector", "sizeBytes": size_bytes}],
                    ),
                    "catalog.json",
                    "workspace-snapshot-catalog",
                    3,
                )

    def test_real_lifecycle_sequence_restores_a_replacement_pvc(self) -> None:
        access = FakeAccess(opaque_catalog("existing-selector"))

        result = kopia_acceptance.KopiaAcceptance(access, lambda _seconds: None).run(
            "deadbeef",
            kopia_acceptance.time.monotonic() + 30,
        )

        self.assertEqual(result["baselineSelectorCount"], 1)
        self.assertEqual(result["selector"], "new-selector")
        self.assertEqual(result["sourcePvcUid"], SOURCE_PVC_ID)
        self.assertEqual(result["restoredPvcUid"], RESTORED_PVC_ID)
        self.assertEqual(
            [event for event in access.events if event[0] in {"create", "stop", "delete"}],
            [
                ("create", ("backup-src-deadbeef", None)),
                ("stop", "backup-src-deadbeef"),
                ("delete", "backup-src-deadbeef"),
                ("create", ("backup-restore-deadbeef", "new-selector")),
                ("delete", "backup-restore-deadbeef"),
            ],
        )
        exec_events = [event for event in access.events if event[0] == "exec"]
        self.assertEqual(len(exec_events), 2)

    def test_full_catalog_is_rejected_before_backup_mutation(self) -> None:
        access = FakeAccess(opaque_catalog(*(f"selector-{index}" for index in range(50))))

        with self.assertRaisesRegex(kopia_acceptance.AcceptanceError, "catalog is full"):
            kopia_acceptance.KopiaAcceptance(access, lambda _seconds: None).run(
                "deadbeef",
                kopia_acceptance.time.monotonic() + 30,
            )

        self.assertNotIn(("stop", "backup-src-deadbeef"), access.events)
        self.assertIn(("delete", "backup-src-deadbeef"), access.events)


if __name__ == "__main__":
    unittest.main()
