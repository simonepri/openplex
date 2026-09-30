"""Unit tests for multi-key parsing, quota inspection, and selection logic."""

from __future__ import annotations

import datetime
import os
import unittest
from unittest import mock

from src.infra.tools.review.engine.api_keys import (
    KeyQuotaStatus,
    parse_duration,
    parse_keys,
    select_best_key,
)


class TestApiKeys(unittest.TestCase):
    def test_parse_keys(self) -> None:
        with mock.patch.dict(os.environ, {"TEST_KEY": "key1", "TEST_KEYS": "key2, key3, key1"}):
            keys = parse_keys("TEST_KEY", "TEST_KEYS")
            self.assertEqual(keys, ["key1", "key2", "key3"])

    def test_parse_keys_json_and_multiple_vars(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "OAUTH_TOKEN": "token-single",
                "OAUTH_TOKENS_CSV": "token-csv1, token-csv2",
                "OAUTH_TOKENS_JSON": '["token-json1", "token-json2"]',
            },
        ):
            keys = parse_keys("OAUTH_TOKEN", "OAUTH_TOKENS_CSV", "OAUTH_TOKENS_JSON")
            self.assertEqual(
                keys,
                ["token-single", "token-csv1", "token-csv2", "token-json1", "token-json2"],
            )

    def test_parse_duration(self) -> None:
        self.assertEqual(parse_duration("10s"), datetime.timedelta(seconds=10))
        self.assertEqual(parse_duration("5m"), datetime.timedelta(minutes=5))
        self.assertEqual(parse_duration("1h"), datetime.timedelta(hours=1))
        self.assertIsNone(parse_duration("invalid"))

    def test_select_best_key_sorting(self) -> None:
        now = datetime.datetime.now(datetime.UTC)
        k1 = KeyQuotaStatus(
            key="k1",
            provider="anthropic",
            valid=True,
            has_quota=True,
            tokens_remaining=1000,
            reset_time=now + datetime.timedelta(seconds=30),
        )
        k2 = KeyQuotaStatus(
            key="k2",
            provider="anthropic",
            valid=True,
            has_quota=True,
            tokens_remaining=5000,
            reset_time=now + datetime.timedelta(seconds=10),  # Earlier reset
        )
        k3 = KeyQuotaStatus(
            key="k3",
            provider="anthropic",
            valid=False,
            has_quota=False,
            tokens_remaining=0,
        )

        with mock.patch("src.infra.tools.review.engine.api_keys.check_anthropic_key") as mock_check:
            mock_check.side_effect = lambda k: {"k1": k1, "k2": k2, "k3": k3}[k]
            best = select_best_key("anthropic", ["k1", "k2", "k3"])
            self.assertEqual(best, "k2")


if __name__ == "__main__":
    unittest.main()
