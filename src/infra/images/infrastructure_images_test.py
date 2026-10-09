"""Validates infrastructure container images inventory and Renovate integration."""

import json
import os
import re
import unittest
from pathlib import Path

ALLOWED_INDEX_MEDIA_TYPES = {
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
}


def _resolve_data_path(rel_path: str) -> Path:
    test_srcdir = os.environ.get("TEST_SRCDIR")
    if test_srcdir:
        workspace = os.environ.get("TEST_WORKSPACE", "_main")
        candidate = Path(test_srcdir) / workspace / rel_path
        if candidate.exists():
            return candidate
    repo_root = Path(__file__).resolve().parents[3]
    candidate = repo_root / rel_path
    if candidate.exists():
        return candidate
    return Path(rel_path)


class InfrastructureImagesTest(unittest.TestCase):
    def setUp(self) -> None:
        inventory_file = _resolve_data_path("src/infra/images/infrastructure-images.json")
        self.inventory_text = inventory_file.read_text(encoding="utf-8")
        self.inventory = json.loads(self.inventory_text)
        renovate_file = _resolve_data_path(".github/renovate.json")
        self.renovate = json.loads(renovate_file.read_text(encoding="utf-8"))
        kyverno_file = _resolve_data_path(
            "src/infra/argocd/components/kyverno/kustomize/policies.yaml"
        )
        self.kyverno_text = kyverno_file.read_text(encoding="utf-8")

    def test_every_image_pin_is_multi_arch_index_digest(self) -> None:
        images = self.inventory.get("images", [])
        self.assertGreater(len(images), 0, "Inventory must contain images")
        digest_regex = re.compile(r"^sha256:[a-f0-9]{64}$")

        for img in images:
            with self.subTest(image=img.get("name", img.get("repository"))):
                media_type = img.get("mediaType")
                self.assertIn(
                    media_type,
                    ALLOWED_INDEX_MEDIA_TYPES,
                    f"Image {img.get('name')} mediaType must be a multi-arch index digest",
                )
                digest = img.get("imageDigest", "")
                self.assertRegex(
                    digest,
                    digest_regex,
                    f"Image {img.get('name')} imageDigest must be valid sha256 digest",
                )
                tag = img.get("tag", "")
                self.assertTrue(
                    bool(tag),
                    f"Image {img.get('name')} must have a tag field for Renovate versioning",
                )

    def test_renovate_custom_manager_matches_every_inventory_entry(self) -> None:
        infra_manager = next(
            m
            for m in self.renovate.get("customManagers", [])
            if "infrastructure-images" in m.get("description", "")
        )
        match_strings = infra_manager["matchStrings"]
        py_patterns = [
            re.compile(re.sub(r"\(\?<([a-zA-Z0-9_]+)>", r"(?P<\1>", p)) for p in match_strings
        ]

        images = self.inventory.get("images", [])
        matched_repos = set()
        for p in py_patterns:
            for match in p.finditer(self.inventory_text):
                matched_repos.add(match.group("repository"))
                self.assertTrue(match.group("currentDigest").startswith("sha256:"))
                self.assertTrue(len(match.group("currentValue")) > 0)

        for img in images:
            repo = img["repository"]
            self.assertIn(
                repo,
                matched_repos,
                f"Renovate custom manager must match repository {repo}",
            )

    def test_renovate_custom_manager_matches_every_generated_line_in_applicationsets(self) -> None:
        appset_manager = next(
            m
            for m in self.renovate.get("customManagers", [])
            if "ApplicationSet templates" in m.get("description", "")
        )
        match_strings = appset_manager["matchStrings"]
        py_patterns = [
            re.compile(re.sub(r"\(\?<([a-zA-Z0-9_]+)>", r"(?P<\1>", p)) for p in match_strings
        ]

        images = self.inventory.get("images", [])
        expected_repos = {img["repository"] for img in images}

        for rel_path in ["src/infra/argocd/apps/ctrl.yaml", "src/infra/argocd/apps/cells.yaml"]:
            file_path = _resolve_data_path(rel_path)
            content = file_path.read_text(encoding="utf-8")
            matched_repos = set()
            for p in py_patterns:
                for match in p.finditer(content):
                    repo = match.group("repository")
                    matched_repos.add(repo)
                    self.assertTrue(match.group("currentDigest").startswith("sha256:"))
                    self.assertTrue(len(match.group("currentValue")) > 0)
            self.assertEqual(
                matched_repos,
                expected_repos,
                f"Renovate custom manager must match every repository in {rel_path}",
            )

    def test_dragonfly_client_republish_follows_base_pin(self) -> None:
        package_rules = self.renovate.get("packageRules", [])
        dragonfly_rules = [
            r
            for r in package_rules
            if "src/third_party/dragonflyoss/client" in r.get("matchPackageNames", [])
        ]
        self.assertEqual(
            len(dragonfly_rules),
            1,
            "Renovate must declare one package rule for the Dragonfly client republish",
        )
        self.assertIs(
            dragonfly_rules[0].get("enabled"),
            False,
            "The Dragonfly client republish follows its MODULE.bazel base pin, not Renovate",
        )
        entry = next(e for e in self.inventory["images"] if e["name"] == "dragonfly-client")
        self.assertRegex(entry["tag"], r"^upstream-[0-9a-f]{7}$")

    def test_ci_tag_regex_versioning_sorts_timestamps(self) -> None:
        infra_manager = next(
            m
            for m in self.renovate.get("customManagers", [])
            if "infrastructure-images" in m.get("description", "")
        )
        versioning_template = infra_manager["versioningTemplate"]
        self.assertTrue(versioning_template.startswith("regex:"))
        raw_pattern = versioning_template.removeprefix("regex:")
        py_pattern = re.compile(re.sub(r"\(\?<([a-zA-Z0-9_]+)>", r"(?P<\1>", raw_pattern))

        tag_earlier = "ci-20261004T072918Z_7a1f4bb4814d"
        tag_later = "ci-20261004T182245Z_386650a7bac1"

        m_earlier = py_pattern.match(tag_earlier)
        m_later = py_pattern.match(tag_later)

        self.assertIsNotNone(m_earlier, f"{tag_earlier} must match versioning regex")
        self.assertIsNotNone(m_later, f"{tag_later} must match versioning regex")
        assert m_earlier is not None
        assert m_later is not None

        v_earlier = (int(m_earlier.group("minor")), int(m_earlier.group("patch")))
        v_later = (int(m_later.group("minor")), int(m_later.group("patch")))
        self.assertLess(v_earlier, v_later, "Earlier tag must sort before later tag")

        tag_hash_high = "ci-20261004T104440Z_acb0d8160b4b"
        m_hash_high = py_pattern.match(tag_hash_high)
        self.assertIsNotNone(m_hash_high)
        assert m_hash_high is not None
        v_hash_high = (int(m_hash_high.group("minor")), int(m_hash_high.group("patch")))
        self.assertLess(v_hash_high, v_later)

    def test_kyverno_policy_dragonfly_client_digest_matches_inventory(self) -> None:
        dragonfly_client = next(
            img for img in self.inventory.get("images", []) if img.get("name") == "dragonfly-client"
        )
        expected_digest = dragonfly_client["imageDigest"]
        self.assertTrue(expected_digest.startswith("sha256:"))

        matches = re.findall(
            r"src/third_party/dragonflyoss/client@(sha256:[a-f0-9]{64})",
            self.kyverno_text,
        )
        self.assertGreater(
            len(matches),
            0,
            "policies.yaml must contain dragonfly-client image references",
        )
        for digest in matches:
            self.assertEqual(
                digest,
                expected_digest,
                f"Kyverno policy dragonfly-client digest {digest} must match inventory {expected_digest}",
            )


if __name__ == "__main__":
    unittest.main()
