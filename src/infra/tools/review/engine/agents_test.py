"""Unit tests for agent plugin registry and adapters."""

from __future__ import annotations

import os
import unittest
from typing import TYPE_CHECKING
from unittest import mock

from src.infra.tools.review.engine.agents import (
    DEFAULT_EFFORT_MAPPING,
    DEFAULT_REGISTRY,
    AgentExecutionResult,
    AgentPlugin,
    AgentRegistry,
    AgyAgent,
    ClaudeAgent,
    CodexAgent,
    GenericCliAgent,
)

if TYPE_CHECKING:
    from pathlib import Path


class DummyAgent(AgentPlugin):
    @property
    def name(self) -> str:
        return "dummy"

    @property
    def provider(self) -> str:
        return "dummy_prov"

    def run(
        self,
        bundle_file: Path,
        repo_root: Path,
        env: dict[str, str],
        custom_args: list[str] | None = None,
        size_bucket: str = "M",
    ) -> AgentExecutionResult:
        del size_bucket
        return AgentExecutionResult(raw_output="{}", returncode=0, model_used="dummy-model")


class TestAgents(unittest.TestCase):
    def test_default_registry_contents(self) -> None:
        self.assertIsInstance(DEFAULT_REGISTRY.get("claude"), ClaudeAgent)
        self.assertIsInstance(DEFAULT_REGISTRY.get("codex"), CodexAgent)
        self.assertIsInstance(DEFAULT_REGISTRY.get("agy"), AgyAgent)

    def test_default_effort_mapping(self) -> None:
        claude = ClaudeAgent()
        self.assertEqual(claude.resolve_effort("XS"), "low")
        self.assertEqual(claude.resolve_effort("S"), "low")
        self.assertEqual(claude.resolve_effort("M"), "medium")
        self.assertEqual(claude.resolve_effort("L"), "high")
        self.assertEqual(claude.resolve_effort("XL"), "high")
        self.assertEqual(claude.resolve_effort("XXL"), "high")

    def test_custom_effort_mapping(self) -> None:
        custom_mapping = {**DEFAULT_EFFORT_MAPPING, "M": "high"}
        claude = ClaudeAgent(default_model="custom-opus", effort_mapping=custom_mapping)
        self.assertEqual(claude.default_model, "custom-opus")
        self.assertEqual(claude.resolve_effort("M"), "high")
        self.assertEqual(claude.resolve_effort("XS"), "low")

    def test_resolve_custom_plugin(self) -> None:
        registry = AgentRegistry()
        dummy = DummyAgent()
        registry.register(dummy)

        plugin, args = registry.resolve("dummy --flag test")
        self.assertEqual(plugin.name, "dummy")
        self.assertEqual(args, ["--flag", "test"])

    def test_resolve_generic_fallback(self) -> None:
        registry = AgentRegistry()
        plugin, args = registry.resolve("my-custom-cli --foo")
        self.assertIsInstance(plugin, GenericCliAgent)
        self.assertEqual(plugin.name, "my-custom-cli")
        self.assertEqual(args, ["--foo"])

    def test_resolve_auto_raises_when_none_available(self) -> None:
        registry = AgentRegistry()
        registry.register(DummyAgent())
        with mock.patch("shutil.which", return_value=None):
            with self.assertRaises(RuntimeError) as ctx:
                registry.resolve("auto")
            self.assertIn("No supported review agent CLI found", str(ctx.exception))

    def test_claude_prioritizes_oauth_over_api_key(self) -> None:
        claude = ClaudeAgent()
        with mock.patch.dict(
            os.environ,
            {
                "CLAUDE_CODE_OAUTH_TOKEN": "oauth-tok-123",
                "ANTHROPIC_API_KEY": "sk-ant-api03-test",
            },
        ):
            configured = claude.configure_environment({})
            self.assertEqual(configured.get("CLAUDE_CODE_OAUTH_TOKEN"), "oauth-tok-123")
            self.assertNotIn("ANTHROPIC_API_KEY", configured)

    def test_claude_falls_back_to_api_key_when_no_oauth(self) -> None:
        claude = ClaudeAgent()
        with mock.patch.dict(
            os.environ,
            {
                "CLAUDE_CODE_OAUTH_TOKEN": "",
                "CLAUDE_CODE_OAUTH_TOKENS": "",
                "CLAUDE_REVIEW_OAUTH_TOKENS": "",
                "ANTHROPIC_API_KEY": "sk-ant-api03-test",
            },
        ):
            configured = claude.configure_environment({})
            self.assertEqual(configured.get("ANTHROPIC_API_KEY"), "sk-ant-api03-test")
            self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", configured)

    def test_ambient_credentials_detection(self) -> None:
        claude = ClaudeAgent()
        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch.object(claude, "has_ambient_credentials", return_value=True):
                self.assertTrue(claude.has_credentials())
            with mock.patch.object(claude, "has_ambient_credentials", return_value=False):
                self.assertFalse(claude.has_credentials())


if __name__ == "__main__":
    unittest.main()
