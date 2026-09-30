"""Resolve the effective build flags of Bazel rc files and validate BuildBuddy profile selections."""

from __future__ import annotations

import os
import shlex
from dataclasses import dataclass
from pathlib import Path

# Commands whose rc lines apply to `bazel build`, in the order Bazel applies them.
BUILD_COMMANDS = ("always", "common", "build")
# A selection names at most one profile from each group.
FLAVORS = ("bb-cloud", "bb-cloud-proxy", "bb-community")
EXECUTION_PROFILES = ("bb-rbe-cloud", "bb-rbe-sh")
EXECUTORLESS_FLAVORS = ("bb-community",)
_IMPORTS = ("import", "try-import")
_WORKSPACE_PREFIX = "%workspace%/"
_CONFIG = "config"


@dataclass(frozen=True)
class _RcLine:
    command: str
    config: str | None
    flags: tuple[str, ...]


def _split_flag(flag: str) -> tuple[str, str]:
    """Split `--name=value` into its parts; a bare `--name` is a boolean set to true."""
    name, sep, value = flag.removeprefix("--").partition("=")
    return name, value if sep else "true"


def _workspace_import(target: str, workspace: Path) -> Path | None:
    """Return the file an import points to inside the workspace, or None for any other import."""
    if not target.startswith(_WORKSPACE_PREFIX):
        return None
    # Normalize without resolving: runfiles symlink each file to its source.
    root = Path(os.path.normpath(workspace.absolute()))
    path = Path(os.path.normpath(root / target.removeprefix(_WORKSPACE_PREFIX)))
    if not path.is_relative_to(root):
        msg = f"import {target!r} leaves the workspace"
        raise ValueError(msg)
    return path


def _parse(text: str, workspace: Path | None) -> list[_RcLine]:
    """Return the rc lines of `text`, inlining imports that point into the workspace."""
    lines: list[_RcLine] = []
    for raw in text.splitlines():
        tokens = shlex.split(raw, comments=True)
        if not tokens:
            continue
        if tokens[0] in _IMPORTS:
            path = _workspace_import(tokens[1], workspace) if workspace else None
            if path is None:
                continue
            if not path.is_file():
                if tokens[0] == "try-import":
                    continue
                msg = f"imported file {tokens[1]!r} does not exist"
                raise ValueError(msg)
            lines.extend(_parse(path.read_text(encoding="utf-8"), workspace))
            continue
        command, _, config = tokens[0].partition(":")
        lines.append(_RcLine(command, config or None, tuple(tokens[1:])))
    return lines


class Bazelrc:
    """The profiles of an rc file and its workspace imports, resolved as `bazel build` resolves them."""

    def __init__(self, *texts: str, workspace: Path | None = None) -> None:
        """Parse `texts` in order, as consecutive imports; with a workspace, follow `%workspace%/` imports."""
        self._lines = [line for text in texts for line in _parse(text, workspace)]
        self.profiles = frozenset(line.config for line in self._lines if line.config)

    @classmethod
    def load(cls, path: Path, workspace: Path) -> Bazelrc:
        return cls(path.read_text(encoding="utf-8"), workspace=workspace)

    @property
    def default_selection(self) -> list[str]:
        """Return the profiles that unconditional `--config=` lines select on every build."""
        return [
            value
            for command in BUILD_COMMANDS
            for line in self._lines
            if line.command == command and line.config is None
            for name, value in map(_split_flag, line.flags)
            if name == _CONFIG
        ]

    def _expand(self, config: str | None, stack: tuple[str, ...]) -> list[tuple[str, str]]:
        """Return the flags of one profile (or of the unconfigured lines) with nested profiles inlined."""
        if config is not None and config not in self.profiles:
            msg = f"profile {config!r} is not defined"
            raise ValueError(msg)
        if config in stack:
            msg = f"profile cycle: {' -> '.join((*stack, config))}"
            raise ValueError(msg)
        inner = stack if config is None else (*stack, config)
        flags: list[tuple[str, str]] = []
        for command in BUILD_COMMANDS:
            for line in self._lines:
                if line.command != command or line.config != config:
                    continue
                for flag in line.flags:
                    name, value = _split_flag(flag)
                    if name == _CONFIG:
                        flags.extend(self._expand(value, inner))
                    else:
                        flags.append((name, value))
        return flags

    def _extends(self, config: str) -> set[str]:
        """Return every profile that `config` pulls in through nested `--config=` flags."""
        reached: set[str] = set()
        pending = [config]
        while pending:
            current = pending.pop()
            for line in self._lines:
                if line.config != current or line.command not in BUILD_COMMANDS:
                    continue
                for name, value in map(_split_flag, line.flags):
                    if name == _CONFIG and value not in reached:
                        reached.add(value)
                        pending.append(value)
        return reached

    def effective_flags(self, configs: list[str]) -> dict[str, str]:
        """Return the last value of each flag for `bazel build --config=<each config>`."""
        flags = self._expand(None, ())
        for config in configs:
            flags.extend(self._expand(config, ()))
        return dict(flags)

    def validate_selection(self, configs: list[str]) -> list[str]:
        """Return one error string per rule that the selected profiles break."""
        selected = set(configs)
        errors = [
            f"{config}: profiles starting with '_' are building blocks, not selections"
            for config in sorted(selected)
            if config.startswith("_")
        ]
        errors.extend(
            f"{config}: profile is not defined" for config in sorted(selected - self.profiles)
        )
        flavors = selected.intersection(FLAVORS)
        # A flavor that another selected flavor extends is refined by it, not a second flavor.
        refined = {base for flavor in flavors for base in self._extends(flavor)}
        if len(flavors - refined) > 1:
            errors.append(f"select at most one flavor, got {', '.join(sorted(flavors))}")
        execution = sorted(selected.intersection(EXECUTION_PROFILES))
        if len(execution) > 1:
            errors.append(f"select at most one execution profile, got {', '.join(execution)}")
        executorless = sorted(flavors.intersection(EXECUTORLESS_FLAVORS))
        if executorless and execution:
            errors.append(
                f"{', '.join(executorless)} has no executor, so it cannot use {', '.join(execution)}"
            )
        return errors
