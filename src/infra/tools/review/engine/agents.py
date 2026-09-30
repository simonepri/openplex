"""Defines a generic agent adapter interface and registry for pluggable AI review CLIs."""

from __future__ import annotations

import abc
import dataclasses
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from src.infra.tools.review.engine.api_keys import parse_keys, select_best_key
from src.infra.tools.review.engine.report import review_schema

if TYPE_CHECKING:
    from collections.abc import Mapping

DEFAULT_EFFORT_MAPPING: dict[str, str] = {
    "XS": "low",
    "S": "low",
    "M": "medium",
    "L": "high",
    "XL": "high",
    "XXL": "high",
}


def _extract_flag(args: list[str], *flags: str) -> str | None:
    """Finds argument value following any of the given flag strings."""
    for flag in flags:
        if flag in args:
            idx = args.index(flag)
            if idx + 1 < len(args):
                return args[idx + 1]
    return None


@dataclasses.dataclass(frozen=True)
class AgentExecutionResult:
    """Standardized output from an agent execution."""

    raw_output: str
    returncode: int
    stderr: str = ""
    model_used: str = ""


class AgentPlugin(abc.ABC):
    """Abstract interface representing an AI CLI review runner."""

    @property
    @abc.abstractmethod
    def name(self) -> str:
        """The primary executable name for this agent (e.g. claude, codex, agy)."""

    @property
    @abc.abstractmethod
    def provider(self) -> str:
        """The AI provider name for quota and stats tracking (e.g. anthropic, openai, gemini)."""

    @property
    def env_keys(self) -> tuple[str, ...]:
        """Candidate environment variable names for authentication."""
        return (f"{self.provider.upper()}_API_KEY", f"{self.provider.upper()}_API_KEYS")

    @property
    def default_model(self) -> str:
        """Default model to invoke when not explicitly provided via flags."""
        return ""

    @property
    def effort_mapping(self) -> Mapping[str, str]:
        """Maps canonical size buckets (XS..XXL) to reasoning effort strings."""
        return DEFAULT_EFFORT_MAPPING

    def resolve_effort(self, size_bucket: str) -> str:
        """Resolves the reasoning effort for a given size bucket."""
        return self.effort_mapping.get(size_bucket, "medium")

    def is_installed(self) -> bool:
        """Checks if the CLI executable is available in the system PATH."""
        return shutil.which(self.name) is not None

    def has_ambient_credentials(self) -> bool:
        """Checks if ambient local CLI credentials or sessions exist."""
        return False

    def has_credentials(self) -> bool:
        """Checks if valid credentials exist either via environment variables or ambient login."""
        return len(parse_keys(*self.env_keys)) > 0 or self.has_ambient_credentials()

    def is_available(self) -> bool:
        """Checks if the agent can be executed (both installed and credentials available)."""
        return self.is_installed() and self.has_credentials()

    def configure_environment(self, base_env: dict[str, str] | None = None) -> dict[str, str]:
        """Selects the best available API key/token and injects it into the execution environment."""
        env = (base_env if base_env is not None else os.environ).copy()
        keys = parse_keys(*self.env_keys)
        if keys:
            primary_var = self.env_keys[0]
            best_key = select_best_key(self.provider, keys)
            env[primary_var] = best_key
        return env

    @abc.abstractmethod
    def run(
        self,
        bundle_file: Path,
        repo_root: Path,
        env: dict[str, str],
        custom_args: list[str] | None = None,
        size_bucket: str = "M",
    ) -> AgentExecutionResult:
        """Executes the agent CLI against the bundle file with structured output."""


class ClaudeAgent(AgentPlugin):
    """Adapter for Anthropic's Claude Code CLI with configurable model and effort mapping."""

    def __init__(
        self,
        default_model: str = "claude-opus-5-5",
        effort_mapping: Mapping[str, str] | None = None,
    ) -> None:
        self._default_model = default_model
        self._effort_mapping = effort_mapping or DEFAULT_EFFORT_MAPPING

    @property
    def name(self) -> str:
        return "claude"

    @property
    def provider(self) -> str:
        return "anthropic"

    @property
    def env_keys(self) -> tuple[str, ...]:
        return (
            "CLAUDE_CODE_OAUTH_TOKEN",
            "CLAUDE_CODE_OAUTH_TOKENS",
            "CLAUDE_REVIEW_OAUTH_TOKENS",
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_API_KEYS",
        )

    def has_ambient_credentials(self) -> bool:
        home = Path.home()
        claude_json = home / ".claude.json"
        claude_dir = home / ".claude"
        return claude_json.exists() or claude_dir.is_dir()

    def configure_environment(self, base_env: dict[str, str] | None = None) -> dict[str, str]:
        env = (base_env if base_env is not None else os.environ).copy()
        oauth_tokens = parse_keys(
            "CLAUDE_CODE_OAUTH_TOKEN",
            "CLAUDE_CODE_OAUTH_TOKENS",
            "CLAUDE_REVIEW_OAUTH_TOKENS",
        )
        if oauth_tokens:
            env["CLAUDE_CODE_OAUTH_TOKEN"] = oauth_tokens[0]
            env.pop("ANTHROPIC_API_KEY", None)
            env.pop("ANTHROPIC_API_KEYS", None)
            return env
        api_keys = parse_keys("ANTHROPIC_API_KEY", "ANTHROPIC_API_KEYS")
        if api_keys:
            env["ANTHROPIC_API_KEY"] = select_best_key(self.provider, api_keys)
            env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
            env.pop("CLAUDE_CODE_OAUTH_TOKENS", None)
            env.pop("CLAUDE_REVIEW_OAUTH_TOKENS", None)
        return env

    @property
    def default_model(self) -> str:
        return self._default_model

    @property
    def effort_mapping(self) -> Mapping[str, str]:
        return self._effort_mapping

    def run(
        self,
        bundle_file: Path,
        repo_root: Path,
        env: dict[str, str],
        custom_args: list[str] | None = None,
        size_bucket: str = "M",
    ) -> AgentExecutionResult:
        parts = [self.name, *(custom_args or [])]
        effort = self.resolve_effort(size_bucket)

        model_name = _extract_flag(parts, "--model", "-m")
        if not model_name:
            model_name = self.default_model
            parts.extend(["--model", model_name])

        if "--effort" not in parts and "-e" not in parts:
            parts.extend(["--effort", effort])

        if "-p" not in parts and "--print" not in parts:
            parts.append("-p")
        if "--output-format" not in parts:
            parts.extend(["--output-format", "json"])
        if "--json-schema" not in parts:
            parts.extend(["--json-schema", json.dumps(review_schema())])
        if "--dangerously-skip-permissions" not in parts:
            parts.append("--dangerously-skip-permissions")

        instruction = (
            f"Perform a comprehensive code review using the review instructions, rules rubrics, and diff provided in {bundle_file}. "
            "Output your findings according to the JSON schema."
        )
        parts.append(instruction)

        proc = subprocess.run(
            parts,
            cwd=repo_root,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
        )
        return AgentExecutionResult(
            raw_output=proc.stdout.strip(),
            returncode=proc.returncode,
            stderr=proc.stderr.strip(),
            model_used=model_name,
        )


class CodexAgent(AgentPlugin):
    """Adapter for OpenAI's Codex CLI with configurable model and effort mapping."""

    def __init__(
        self,
        default_model: str = "6-luna",
        effort_mapping: Mapping[str, str] | None = None,
    ) -> None:
        self._default_model = default_model
        self._effort_mapping = effort_mapping or DEFAULT_EFFORT_MAPPING

    @property
    def name(self) -> str:
        return "codex"

    @property
    def provider(self) -> str:
        return "openai"

    @property
    def env_keys(self) -> tuple[str, ...]:
        return (
            "CODEX_API_KEY",
            "CODEX_API_KEYS",
            "OPENAI_API_KEY",
            "OPENAI_API_KEYS",
        )

    def has_ambient_credentials(self) -> bool:
        home = Path.home()
        codex_auth = home / ".codex" / "auth.json"
        codex_cfg = home / ".codex" / "config.toml"
        return codex_auth.exists() or codex_cfg.exists()

    def configure_environment(self, base_env: dict[str, str] | None = None) -> dict[str, str]:
        env = (base_env if base_env is not None else os.environ).copy()
        codex_keys = parse_keys("CODEX_API_KEY", "CODEX_API_KEYS")
        if codex_keys:
            env["CODEX_API_KEY"] = select_best_key(self.provider, codex_keys)
            return env
        openai_keys = parse_keys("OPENAI_API_KEY", "OPENAI_API_KEYS")
        if openai_keys:
            env["OPENAI_API_KEY"] = select_best_key(self.provider, openai_keys)
        return env

    @property
    def default_model(self) -> str:
        return self._default_model

    @property
    def effort_mapping(self) -> Mapping[str, str]:
        return self._effort_mapping

    def run(
        self,
        bundle_file: Path,
        repo_root: Path,
        env: dict[str, str],
        custom_args: list[str] | None = None,
        size_bucket: str = "M",
    ) -> AgentExecutionResult:
        parts = [self.name, *(custom_args or [])]
        if len(parts) == 1:
            parts.append("exec")

        effort = self.resolve_effort(size_bucket)
        model_name = _extract_flag(parts, "-m", "--model")
        if not model_name:
            model_name = self.default_model
            parts.extend(["-m", model_name])

        if "--effort" not in parts and "-e" not in parts:
            parts.extend(["--effort", effort])

        review_dir = bundle_file.parent
        schema_file = review_dir / "review-schema.json"
        schema_file.write_text(json.dumps(review_schema(), indent=2), encoding="utf-8")
        codex_out = review_dir / "codex-out.json"

        if "--output-schema" not in parts:
            parts.extend(["--output-schema", str(schema_file)])
        if "--dangerously-bypass-approvals-and-sandbox" not in parts:
            parts.append("--dangerously-bypass-approvals-and-sandbox")
        if "-o" not in parts and "--output-last-message" not in parts:
            parts.extend(["-o", str(codex_out)])

        instruction = (
            f"Perform a comprehensive code review using the review instructions, rules rubrics, and diff provided in {bundle_file}. "
            "Output your findings according to the JSON schema."
        )
        parts.append(instruction)

        proc = subprocess.run(
            parts,
            cwd=repo_root,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
        )

        raw_output = (
            codex_out.read_text(encoding="utf-8").strip()
            if codex_out.exists()
            else proc.stdout.strip()
        )
        return AgentExecutionResult(
            raw_output=raw_output,
            returncode=proc.returncode,
            stderr=proc.stderr.strip(),
            model_used=model_name,
        )


class AgyAgent(AgentPlugin):
    """Adapter for Gemini / Antigravity's Agy CLI with configurable model and effort mapping."""

    def __init__(
        self,
        default_model: str = "flash-3.8",
        effort_mapping: Mapping[str, str] | None = None,
    ) -> None:
        self._default_model = default_model
        self._effort_mapping = effort_mapping or DEFAULT_EFFORT_MAPPING

    @property
    def name(self) -> str:
        return "agy"

    @property
    def provider(self) -> str:
        return "gemini"

    @property
    def default_model(self) -> str:
        return self._default_model

    @property
    def effort_mapping(self) -> Mapping[str, str]:
        return self._effort_mapping

    @property
    def env_keys(self) -> tuple[str, ...]:
        return (
            "GEMINI_API_KEY",
            "GEMINI_API_KEYS",
        )

    def has_ambient_credentials(self) -> bool:
        home = Path.home()
        gemini_dir = home / ".gemini"
        return gemini_dir.is_dir()

    def configure_environment(self, base_env: dict[str, str] | None = None) -> dict[str, str]:
        env = (base_env if base_env is not None else os.environ).copy()
        keys = parse_keys(*self.env_keys)
        if keys:
            env["GEMINI_API_KEY"] = select_best_key(self.provider, keys)
        return env

    def run(
        self,
        bundle_file: Path,
        repo_root: Path,
        env: dict[str, str],
        custom_args: list[str] | None = None,
        size_bucket: str = "M",
    ) -> AgentExecutionResult:
        parts = [self.name, *(custom_args or [])]
        effort = self.resolve_effort(size_bucket)

        model_name = _extract_flag(parts, "--model", "-m")
        if not model_name:
            model_name = self.default_model
            parts.extend(["--model", model_name])

        if "--effort" not in parts and "-e" not in parts:
            parts.extend(["--effort", effort])

        if "-p" not in parts and "--print" not in parts:
            parts.append("-p")
        if "--output-format" not in parts:
            parts.extend(["--output-format", "json"])
        if "--json-schema" not in parts:
            parts.extend(["--json-schema", json.dumps(review_schema())])
        if "--dangerously-skip-permissions" not in parts:
            parts.append("--dangerously-skip-permissions")

        instruction = (
            f"Perform a comprehensive code review using the review instructions, rules rubrics, and diff provided in {bundle_file}. "
            "Output your findings according to the JSON schema."
        )
        parts.append(instruction)

        proc = subprocess.run(
            parts,
            cwd=repo_root,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
        )
        return AgentExecutionResult(
            raw_output=proc.stdout.strip(),
            returncode=proc.returncode,
            stderr=proc.stderr.strip(),
            model_used=model_name,
        )


class GenericCliAgent(AgentPlugin):
    """Fallback adapter for arbitrary review CLI binaries piped with prompt text."""

    def __init__(self, binary_name: str) -> None:
        self._binary_name = binary_name

    @property
    def name(self) -> str:
        return self._binary_name

    @property
    def provider(self) -> str:
        return self._binary_name

    def run(
        self,
        bundle_file: Path,
        repo_root: Path,
        env: dict[str, str],
        custom_args: list[str] | None = None,
        size_bucket: str = "M",
    ) -> AgentExecutionResult:
        del size_bucket
        parts = [self.name, *(custom_args or [])]
        prompt_content = bundle_file.read_text(encoding="utf-8")
        proc = subprocess.run(
            parts,
            cwd=repo_root,
            env=env,
            input=prompt_content,
            capture_output=True,
            text=True,
            check=False,
        )
        return AgentExecutionResult(
            raw_output=proc.stdout.strip(),
            returncode=proc.returncode,
            stderr=proc.stderr.strip(),
            model_used=self.name,
        )


class AgentRegistry:
    """Registry managing available AI review agents."""

    def __init__(self) -> None:
        self._plugins: dict[str, AgentPlugin] = {}

    def register(self, plugin: AgentPlugin) -> None:
        """Registers a new agent plugin."""
        self._plugins[plugin.name] = plugin

    def get(self, name: str) -> AgentPlugin | None:
        """Looks up an agent plugin by executable name."""
        return self._plugins.get(name)

    def registered_agents(self) -> list[AgentPlugin]:
        """Returns all registered agent plugins."""
        return list(self._plugins.values())

    def available_agents(self) -> list[AgentPlugin]:
        """Returns all plugins whose binary is installed and has valid credentials configured."""
        return [p for p in self._plugins.values() if p.is_available()]

    def installed_agents(self) -> list[AgentPlugin]:
        """Returns all plugins whose binary is present on the system."""
        return [p for p in self._plugins.values() if p.is_installed()]

    def resolve(self, agent_spec: str) -> tuple[AgentPlugin, list[str]]:
        """Resolves an agent command string or 'auto' to a plugin and extra arguments."""
        if agent_spec == "auto":
            available = self.available_agents()
            if available:
                return available[0], []
            installed = self.installed_agents()
            if installed:
                return installed[0], []
            checked = ", ".join(f"'{p.name}'" for p in self.registered_agents())
            raise RuntimeError(
                f"No supported review agent CLI found (checked {checked}). "
                "Please ensure at least one agent CLI is installed and its corresponding API key is set."
            )

        parts = agent_spec.split()
        if not parts:
            raise RuntimeError("Empty agent command specified.")

        name = parts[0]
        plugin = self.get(name) or GenericCliAgent(name)
        return plugin, parts[1:]


# Global default registry preloaded with standard agent plugins
DEFAULT_REGISTRY = AgentRegistry()
DEFAULT_REGISTRY.register(ClaudeAgent())
DEFAULT_REGISTRY.register(CodexAgent())
DEFAULT_REGISTRY.register(AgyAgent())
