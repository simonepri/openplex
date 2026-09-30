#!/usr/bin/env python3
"""Test Argo CD component integrity, profile resolution, and Helm values file validation logic."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import override

from check_argocd_links import validate_argocd_links


class ArgoCDLinksTest(unittest.TestCase):
    @override
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)

        # Set up a minimal valid repo structure
        apps_dir = self.root / "src/infra/argocd/apps"
        apps_dir.mkdir(parents=True)

        comp_dir = self.root / "src/infra/argocd/components/test_comp"
        comp_profiles = comp_dir / "helm/profiles"
        comp_profiles.mkdir(parents=True)
        (comp_profiles / "default.yaml").write_text("key: value\n", encoding="utf-8")

        # Application referencing the component and its profile
        app_yaml = apps_dir / "test_app.yaml"
        app_yaml.write_text(
            """
apiVersion: argoproj.io/v1alpha1
kind: Application
spec:
  source:
    path: src/infra/argocd/components/test_comp/kustomize
    helm:
      valueFiles:
        - $values/src/infra/argocd/components/test_comp/helm/profiles/default.yaml
""",
            encoding="utf-8",
        )

    @override
    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_valid_references_pass(self) -> None:
        errors = validate_argocd_links(self.root)
        assert errors == []

    def test_unreferenced_component_fails(self) -> None:
        unref_dir = self.root / "src/infra/argocd/components/orphan_comp"
        unref_dir.mkdir(parents=True)
        (unref_dir / "dummy.yaml").write_text("foo: bar\n", encoding="utf-8")

        errors = validate_argocd_links(self.root)
        assert any(
            "Unreferenced component: src/infra/argocd/components/orphan_comp" in e for e in errors
        )

    def test_broken_values_file_fails(self) -> None:
        app_yaml = self.root / "src/infra/argocd/apps/test_app.yaml"
        app_yaml.write_text(
            """
apiVersion: argoproj.io/v1alpha1
kind: Application
spec:
  source:
    path: src/infra/argocd/components/test_comp/kustomize
    helm:
      valueFiles:
        - $values/src/infra/argocd/components/test_comp/helm/profiles/missing.yaml
""",
            encoding="utf-8",
        )

        errors = validate_argocd_links(self.root)
        assert any("Broken $values file reference" in e and "missing.yaml" in e for e in errors)


if __name__ == "__main__":
    unittest.main()
