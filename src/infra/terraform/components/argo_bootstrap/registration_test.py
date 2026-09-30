"""Tests cluster registration secret annotations precedence in argo_bootstrap."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

import hcl2


class RegistrationTest(unittest.TestCase):
    def test_registration_annotations_precedence(self) -> None:
        with Path(sys.argv[1]).open(encoding="utf-8") as source:
            parsed = hcl2.load(source)

        # Verify local.registered_cells is defined
        locals_blocks = parsed.get("locals", [])
        registered_cells_local = None
        for block in locals_blocks:
            if "registered_cells" in block:
                registered_cells_local = block["registered_cells"]
                break
        self.assertIsNotNone(registered_cells_local, "local.registered_cells should be defined")

        # Inspect resources
        resources = parsed.get("resource", [])
        ctrl_secret = None
        cell_secret = None
        for res in resources:
            for r_type, r_instances in res.items():
                if r_type.strip('"') == "kubernetes_secret_v1":
                    for name, body in r_instances.items():
                        clean_name = name.strip('"')
                        if clean_name == "control_registration":
                            ctrl_secret = body
                        elif clean_name == "cell_registration":
                            cell_secret = body

        self.assertIsNotNone(ctrl_secret, "control_registration secret resource not found")
        self.assertIsNotNone(cell_secret, "cell_registration secret resource not found")
        assert ctrl_secret is not None
        assert cell_secret is not None

        # In control_registration, verify that registered-cells in annotations
        # appears AFTER var.annotations in the merge call so computed value wins.
        metadata = ctrl_secret.get("metadata", [{}])[0]
        annotations_raw = metadata.get("annotations", "")
        self.assertIn("merge", annotations_raw)
        var_annotations_idx = annotations_raw.find("var.annotations")
        registered_cells_idx = annotations_raw.find("registered-cells")
        self.assertGreater(
            registered_cells_idx,
            var_annotations_idx,
            "registered-cells must appear after var.annotations in merge() so computed value wins",
        )

        # In cell_registration, verify registered-cells appears AFTER annotations
        cell_metadata = cell_secret.get("metadata", [{}])[0]
        cell_annotations_raw = cell_metadata.get("annotations", "")
        self.assertIn("merge", cell_annotations_raw)
        cell_ann_idx = cell_annotations_raw.find("each.value.annotations")
        cell_reg_idx = cell_annotations_raw.find("registered-cells")
        self.assertGreater(
            cell_reg_idx,
            cell_ann_idx,
            "registered-cells must appear after each.value.annotations in cell_registration merge()",
        )


if __name__ == "__main__":
    unittest.main(argv=sys.argv[:1])
