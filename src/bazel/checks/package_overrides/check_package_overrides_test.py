"""Unit tests verifying package manager dependency overrides auditing logic."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import yaml
from check_package_overrides import (
    validate_package_overrides,
)


class PackageOverridesValidationTest(unittest.TestCase):
    """Test suite validating override synchronization and dead override detection."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _write_files(
        self,
        pkg_overrides: dict[str, str] | None = None,
        ws_overrides: dict[str, str] | None = None,
        lock_overrides: dict[str, str] | None = None,
        lock_snapshots: dict[str, dict[str, dict[str, str]]] | None = None,
    ) -> None:
        pkg_data: dict[str, object] = {"name": "test-repo"}
        if pkg_overrides is not None:
            pkg_data["overrides"] = pkg_overrides
        (self.root / "package.json").write_text(json.dumps(pkg_data), encoding="utf-8")

        ws_data: dict[str, object] = {"packages": ["src/**"]}
        if ws_overrides is not None:
            ws_data["overrides"] = ws_overrides
        (self.root / "pnpm-workspace.yaml").write_text(yaml.dump(ws_data), encoding="utf-8")

        lock_data: dict[str, object] = {"lockfileVersion": "9.0"}
        if lock_overrides is not None:
            lock_data["overrides"] = lock_overrides
        if lock_snapshots is not None:
            lock_data["snapshots"] = lock_snapshots
        (self.root / "pnpm-lock.yaml").write_text(yaml.dump(lock_data), encoding="utf-8")

    def test_valid_active_override(self) -> None:
        self._write_files(
            pkg_overrides={"cookie": "0.7.2"},
            ws_overrides={"cookie": "0.7.2"},
            lock_overrides={"cookie": "0.7.2"},
            lock_snapshots={
                "@sveltejs/kit@2.70.3(pkg@1.0.0)": {
                    "dependencies": {"cookie": "0.7.2", "svelte": "5.0.0"}
                }
            },
        )
        errors, info = validate_package_overrides(self.root)
        self.assertEqual(errors, [])
        self.assertTrue(
            any("cookie@0.7.2" in msg and "@sveltejs/kit@2.70.3" in msg for msg in info)
        )

    def test_unused_override_is_flagged_for_removal(self) -> None:
        self._write_files(
            pkg_overrides={"stale-pkg": "1.2.3"},
            ws_overrides={"stale-pkg": "1.2.3"},
            lock_overrides={"stale-pkg": "1.2.3"},
            lock_snapshots={"@sveltejs/kit@2.70.3": {"dependencies": {"cookie": "0.7.2"}}},
        )
        errors, _ = validate_package_overrides(self.root)
        self.assertEqual(len(errors), 1)
        self.assertIn("Override 'stale-pkg@1.2.3' is unused", errors[0])
        self.assertIn("obsolete and should be removed", errors[0])

    def test_manifest_mismatch_flagged(self) -> None:
        self._write_files(
            pkg_overrides={"cookie": "0.7.2", "foo": "1.0.0"},
            ws_overrides={"cookie": "0.7.2"},
            lock_overrides={"cookie": "0.7.2"},
        )
        errors, _ = validate_package_overrides(self.root)
        self.assertTrue(
            any(
                "Override mismatch between package.json and pnpm-workspace.yaml" in e
                for e in errors
            )
        )
        self.assertTrue(any("missing in pnpm-workspace.yaml: ['foo']" in e for e in errors))

    def test_version_disagreement_flagged(self) -> None:
        self._write_files(
            pkg_overrides={"cookie": "0.7.2"},
            ws_overrides={"cookie": "0.7.1"},
            lock_overrides={"cookie": "0.7.2"},
        )
        errors, _ = validate_package_overrides(self.root)
        self.assertTrue(any("differing versions" in e for e in errors))

    def test_lockfile_mismatch_flagged(self) -> None:
        self._write_files(
            pkg_overrides={"cookie": "0.7.2"},
            ws_overrides={"cookie": "0.7.2"},
            lock_overrides={"cookie": "0.7.0"},
        )
        errors, _ = validate_package_overrides(self.root)
        self.assertTrue(
            any("pnpm-lock.yaml overrides do not match package.json overrides" in e for e in errors)
        )

    def test_empty_overrides_is_clean(self) -> None:
        self._write_files(
            pkg_overrides={},
            ws_overrides={},
            lock_overrides={},
            lock_snapshots={},
        )
        errors, info = validate_package_overrides(self.root)
        self.assertEqual(errors, [])
        self.assertEqual(info, [])


if __name__ == "__main__":
    unittest.main()
