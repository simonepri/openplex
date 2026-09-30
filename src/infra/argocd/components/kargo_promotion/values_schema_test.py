"""Validate kargo_promotion values schema against managed smart HTTP, Git daemon, and HTTPS repository URLs."""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path
from typing import Any


def _load_schema() -> dict[str, Any]:
    schema_path = Path(__file__).parent / "helm/values.schema.json"
    if not schema_path.is_file():
        schema_path = Path("src/infra/argocd/components/kargo_promotion/helm/values.schema.json")
    return json.loads(schema_path.read_text(encoding="utf-8"))


class ValuesSchemaTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.schema = _load_schema()
        cls.pattern = cls.schema["allOf"][0]["then"]["properties"]["repositoryURL"]["pattern"]
        cls.regex = re.compile(cls.pattern)

    def test_repository_url_pattern_accepts_valid_transports(self) -> None:
        valid_urls = [
            "http://172.19.255.21:9419/cgi-bin/git/repo.git",
            "http://10.0.0.1:9419/cgi-bin/git/repo.git",
            "http://10.255.255.254:9419/cgi-bin/git/repo-name.git",
            "http://172.16.0.1:9419/cgi-bin/git/my-repo.git",
            "http://172.31.255.254:9419/cgi-bin/git/my-repo.git",
            "http://192.168.1.1:9419/cgi-bin/git/repo.git",
            "http://192.168.254.254:9419/cgi-bin/git/repo.git",
            "git://172.19.255.21:9418/repo.git",
            "git://172.19.0.3:9418/fixture-config.git",
            "git://10.10.10.10:9418/repo.git",
            "git://192.168.0.1:9418/repo.git",
            "https://github.com/example/repo.git",
            "https://example.invalid/fleet-config.git",
        ]
        for url in valid_urls:
            with self.subTest(url=url):
                assert self.regex.match(url) is not None, f"Expected {url} to match pattern"

    def test_repository_url_pattern_rejects_invalid_transports_and_destinations(self) -> None:
        invalid_urls = [
            "http://8.8.8.8:9419/cgi-bin/git/repo.git",
            "http://1.1.1.1:9419/cgi-bin/git/repo.git",
            "http://142.250.190.46:9419/cgi-bin/git/repo.git",
            "http://172.15.0.1:9419/cgi-bin/git/repo.git",
            "http://172.32.0.1:9419/cgi-bin/git/repo.git",
            "http://192.169.0.1:9419/cgi-bin/git/repo.git",
            "http://11.0.0.1:9419/cgi-bin/git/repo.git",
            "http://172.19.255.21:80/cgi-bin/git/repo.git",
            "http://172.19.255.21:443/cgi-bin/git/repo.git",
            "http://172.19.255.21:9418/cgi-bin/git/repo.git",
            "http://172.19.255.21:9420/cgi-bin/git/repo.git",
            "git://172.19.255.21:9419/repo.git",
            "git://172.19.255.21:80/repo.git",
            "http://172.19.255.21:9419/repo.git",
            "http://172.19.255.21:9419/git/repo.git",
            "http://172.19.255.21:9419/cgi-bin/git/",
            "http://172.19.255.21:9419/cgi-bin/git/repo",
            "http://172.19.255.21:9419/cgi-bin/git/nested/repo.git",
            "git://172.19.255.21:9418/cgi-bin/git/repo.git",
            "http://github.com/org/repo.git",
            "http://example.com/cgi-bin/git/repo.git",
            "http://example.local:9419/cgi-bin/git/repo.git",
            "git://8.8.8.8:9418/repo.git",
        ]
        for url in invalid_urls:
            with self.subTest(url=url):
                assert self.regex.match(url) is None, f"Expected {url} to be rejected by pattern"


if __name__ == "__main__":
    unittest.main()
