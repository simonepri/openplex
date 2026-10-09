"""Verify that every Helm chart deployed by Argo CD ApplicationSets is vendored by Bazel."""

from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path
from typing import NamedTuple

import yaml


class VendoredChart(NamedTuple):
    repository: str
    chart_name: str
    version: str


class DeployedChart(NamedTuple):
    application_set: str
    component: str
    chart: str
    repository: str
    version: str


def normalize_repo(repo: str) -> str:
    """Normalize a repository URL or OCI registry path for comparison."""
    return repo.strip().removeprefix("oci://").rstrip("/")


def _parse_vendor_helm_chart(kwargs: dict[str, object]) -> VendoredChart | None:
    if "url" in kwargs:
        raw_url = str(kwargs["url"]).removeprefix("oci://")
        repo_and_chart, version = raw_url.rsplit(":", 1)
        repo, chart = repo_and_chart.rsplit("/", 1)
        return VendoredChart(normalize_repo(repo), chart, version)
    if "repository" in kwargs and "chart_name" in kwargs and "version" in kwargs:
        return VendoredChart(
            normalize_repo(str(kwargs["repository"])),
            str(kwargs["chart_name"]),
            str(kwargs["version"]),
        )
    return None


def _parse_vendor_helm_file(kwargs: dict[str, object]) -> set[VendoredChart]:
    file_path = kwargs.get("downloaded_file_path")
    urls = kwargs.get("urls", [])
    raw_url = str(urls[0]) if isinstance(urls, list) and urls else ""
    if not file_path and raw_url:
        file_path = raw_url.rsplit("/", 1)[-1]
    if not file_path:
        return set()

    base_name = str(file_path).removesuffix(".tgz").removesuffix(".tar.gz")
    chart, version = base_name.rsplit("-", 1)

    url_dir = raw_url.rsplit("/", 1)[0]
    candidate_repos = {normalize_repo(url_dir)}
    if url_dir.endswith("/charts"):
        candidate_repos.add(normalize_repo(url_dir[: -len("/charts")]))

    return {VendoredChart(repo, chart, version) for repo in candidate_repos}


def parse_vendored_charts(module_content: str, filename: str = "<starlark>") -> set[VendoredChart]:
    """Parse vendored Helm charts from Starlark AST in .MODULE.bazel."""
    tree = ast.parse(module_content, filename=filename)
    vendored: set[VendoredChart] = set()

    for node in tree.body:
        if not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)):
            continue
        call = node.value
        func_name = call.func.id if isinstance(call.func, ast.Name) else ""
        if func_name not in {"vendor_helm_chart", "vendor_helm_file"}:
            continue

        kwargs: dict[str, object] = {}
        for kw in call.keywords:
            if kw.arg is not None:
                kwargs[kw.arg] = ast.literal_eval(kw.value)

        if func_name == "vendor_helm_chart":
            chart = _parse_vendor_helm_chart(kwargs)
            if chart is not None:
                vendored.add(chart)
        else:
            vendored.update(_parse_vendor_helm_file(kwargs))

    return vendored


def parse_deployed_charts(application_set_path: str, content: str) -> list[DeployedChart]:
    """Parse deployed Helm charts from an Argo CD ApplicationSet YAML."""
    data = yaml.safe_load(content)
    deployed: list[DeployedChart] = []

    def _walk(node: object) -> None:
        if isinstance(node, dict):
            if "chart" in node and "chartRepo" in node and "chartVersion" in node:
                chart_name = str(node["chart"])
                deployed.append(
                    DeployedChart(
                        application_set=application_set_path,
                        component=str(node.get("component", chart_name)),
                        chart=chart_name,
                        repository=normalize_repo(str(node["chartRepo"])),
                        version=str(node["chartVersion"]),
                    )
                )
            for value in node.values():
                _walk(value)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(data)
    return deployed


def find_unvendored_charts(
    deployed_charts: list[DeployedChart],
    vendored_charts: set[VendoredChart],
) -> list[DeployedChart]:
    """Return all deployed charts that lack an exact vendored match."""
    vendored_lookup = {
        (normalize_repo(v.repository), v.chart_name, v.version) for v in vendored_charts
    }
    return [
        d
        for d in deployed_charts
        if (normalize_repo(d.repository), d.chart, d.version) not in vendored_lookup
    ]


class UnvendoredChartVersionTest(unittest.TestCase):
    def test_find_unvendored_charts_rejects_version_repository_and_chart_drift(self) -> None:
        vendored = {
            VendoredChart("https://charts.example.com", "my-chart", "1.0.0"),
            VendoredChart("quay.io/example/charts", "oci-chart", "v2.0.0"),
        }
        for chart, repository, version, rejected in (
            ("my-chart", "https://charts.example.com", "1.0.0", False),
            ("oci-chart", "oci://quay.io/example/charts/", "v2.0.0", False),
            ("my-chart", "https://charts.example.com", "1.0.1", True),
            ("my-chart", "https://other.example.com", "1.0.0", True),
            ("unknown-chart", "https://charts.example.com", "1.0.0", True),
        ):
            with self.subTest(chart=chart, repository=repository, version=version):
                deployed = [DeployedChart("test.yaml", "component", chart, repository, version)]
                self.assertEqual(
                    find_unvendored_charts(deployed, vendored), deployed if rejected else []
                )

    def test_every_deployed_chart_version_is_vendored(self) -> None:
        app_set_paths = [Path(p) for p in sys.argv[1:3]]
        vendor_module_path = Path(sys.argv[3])
        vendored = parse_vendored_charts(
            vendor_module_path.read_text(encoding="utf-8"),
            filename=str(vendor_module_path),
        )

        all_deployed: list[DeployedChart] = []
        for app_set_path in app_set_paths:
            all_deployed.extend(
                parse_deployed_charts(
                    str(app_set_path),
                    app_set_path.read_text(encoding="utf-8"),
                )
            )

        unvendored = find_unvendored_charts(all_deployed, vendored)
        if unvendored:
            formatted_errors = "\n".join(
                f"  - component={m.component}, chart={m.chart}, repo={m.repository}, version={m.version} (in {m.application_set})"
                for m in unvendored
            )
            self.fail(
                "The following deployed Helm charts have no exact vendored match in "
                f"{vendor_module_path}:\n{formatted_errors}"
            )


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]])
