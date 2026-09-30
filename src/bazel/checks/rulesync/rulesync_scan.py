#!/usr/bin/env python3
"""Discover distributed RuleSync markdown definitions across the repository and synchronize agent guidance."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path, PurePosixPath

try:
    from python.runfiles import runfiles  # type: ignore[import-untyped,import-not-found]
except ImportError:
    runfiles = None  # type: ignore[assignment]

SUPPORTED_FEATURES = ("rules", "skills", "commands", "subagents", "hooks")
DEFAULT_TARGET_CONFIG: dict[str, list[str]] = {
    "claudecode": ["rules", "skills", "commands", "subagents", "hooks", "permissions"],
    "antigravity-cli": ["rules", "skills", "commands", "subagents", "hooks", "permissions"],
    "codexcli": ["skills", "commands", "subagents", "hooks", "permissions"],
}
DEFAULT_RULE_TARGETS = ["claudecode", "antigravity-cli"]
EXCLUDED_DIR_PARTS = {".tmp", ".git", "node_modules", ".cache"}


def find_repo_root() -> Path:
    env_dir = os.environ.get("BUILD_WORKSPACE_DIRECTORY")
    if env_dir:
        return Path(env_dir).resolve()
    try:
        root_str = subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"], text=True
        ).strip()
        return Path(root_str).resolve()
    except (subprocess.CalledProcessError, OSError):
        return Path.cwd().resolve()


def match_rulesync_feature(stem: str) -> tuple[str, str] | None:
    for feat in SUPPORTED_FEATURES:
        if stem == f"{feat}.rulesync.md":
            return feat, ""
        suffix = f".{feat}.rulesync.md"
        if stem.endswith(suffix):
            return feat, stem[: -len(suffix)]
    return None


def discover_rulesync_files(repo_root: Path) -> list[PurePosixPath]:
    try:
        output = subprocess.check_output(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
            cwd=repo_root,
        )
        raw_files = [f for f in output.decode("utf-8", errors="surrogateescape").split("\0") if f]
    except (subprocess.CalledProcessError, OSError):
        raw_files = [
            p.relative_to(repo_root).as_posix()
            for p in repo_root.rglob("*.rulesync.md")
            if not any(part.startswith((".tmp", "bazel-")) for part in p.parts)
        ]

    matched: list[PurePosixPath] = []
    for f in raw_files:
        p = PurePosixPath(f)
        if any(part in EXCLUDED_DIR_PARTS or part.startswith("bazel-") for part in p.parts):
            continue
        if not (repo_root / p).is_file():
            continue
        if match_rulesync_feature(p.name) is not None:
            matched.append(p)
    return matched


def compute_scope_dir(rel_path: PurePosixPath) -> PurePosixPath:
    parent = rel_path.parent
    if parent.name == "agents":
        return parent.parent
    return parent


GLOBAL_RULE_ORDER = [
    "meta",
    "rules",
    "structure",
    "architecture",
    "security",
    "tooling",
    "integrity",
    "git",
    "interaction",
    "writing",
]


def is_global_rule(rel_path: PurePosixPath, fm: str | None) -> bool:
    scope_dir = compute_scope_dir(rel_path)
    if len(scope_dir.parts) != 0:
        return False
    if not fm:
        return True
    return not re.search(r"^\s*globs\s*:", fm, re.MULTILINE)


def compute_slug(
    rel_path: PurePosixPath, feature: str, name: str, *, is_global: bool = False
) -> str:
    scope_dir = compute_scope_dir(rel_path)
    parts = list(scope_dir.parts)
    if is_global and feature == "rules":
        order_idx = GLOBAL_RULE_ORDER.index(name) if name in GLOBAL_RULE_ORDER else 99
        return f"global_{order_idx:02d}_{name}"
    if name and (not parts or name != parts[-1]):
        parts.append(name)
    elif not name and not parts:
        return f"root_{feature}"
    return "_".join(parts).replace("/", "_").replace("-", "_")


def _build_default_frontmatter(rel_path: PurePosixPath) -> str:
    scope_dir = compute_scope_dir(rel_path)
    is_root = len(scope_dir.parts) == 0
    rel_dir = scope_dir.as_posix()
    is_global = is_global_rule(rel_path, None)
    lines = []
    if is_global:
        lines.append("root: true")
    lines.append("targets:")
    for t in DEFAULT_RULE_TARGETS:
        lines.append(f"  - {t}")
    lines.append("globs:")
    if is_global or is_root:
        lines.append('  - "**/*"')
    else:
        lines.append(f'  - "{rel_dir}/**/*"')
    return "\n".join(lines)


def _augment_frontmatter(fm: str, rel_path: PurePosixPath) -> str:
    scope_dir = compute_scope_dir(rel_path)
    is_root = len(scope_dir.parts) == 0
    rel_dir = scope_dir.as_posix()
    is_global = is_global_rule(rel_path, fm)
    result = fm
    if is_global and not re.search(r"^\s*root\s*:", result, re.MULTILINE):
        result = "root: true\n" + result
    if not re.search(r"^\s*globs\s*:", result, re.MULTILINE):
        glob_line = '  - "**/*"' if (is_global or is_root) else f'  - "{rel_dir}/**/*"'
        result += f"\nglobs:\n{glob_line}"
    if not re.search(r"^\s*targets\s*:", result, re.MULTILINE):
        targets_block = "\n".join(f"  - {t}" for t in DEFAULT_RULE_TARGETS)
        result += f"\ntargets:\n{targets_block}"
    return result


ALLOWED_H2_SECTIONS = {"Context", "Principles", "Decisions", "Best Practices"}


def _lint_section_header(line: str, rel_path: PurePosixPath, i: int) -> tuple[bool, str | None]:
    if not line.startswith("## "):
        return False, None
    h2 = line[3:].strip()
    if h2 not in ALLOWED_H2_SECTIONS:
        return True, (
            f"{rel_path}:{i}: invalid section '## {h2}'. Allowed: {sorted(ALLOWED_H2_SECTIONS)}"
        )
    return True, None


def _lint_rule_bullet(line: str, rel_path: PurePosixPath, i: int) -> str | None:
    ls = line.strip()
    if not line.startswith("- **"):
        return f"{rel_path}:{i}: rule bullet must start with bold name '- **<Name>**: ...': '{ls[:50]}'"
    if "**:" not in line and "** —" not in line and "**" not in line[4:]:
        return f"{rel_path}:{i}: rule bullet missing ':' after bold name: '{ls[:50]}'"
    return None


def _lint_rule_containment(rel_path: PurePosixPath, fm: str, scope_dir: PurePosixPath) -> list[str]:
    errors: list[str] = []
    rel_dir = scope_dir.as_posix()
    in_globs = False
    for line in fm.splitlines():
        ls = line.strip()
        if re.match(r"^globs\s*:", ls):
            in_globs = True
            continue
        if in_globs:
            if ls.startswith("-"):
                glob_val = ls.lstrip("-").strip().strip("\"'")
                if not (glob_val == rel_dir or glob_val.startswith(f"{rel_dir}/")):
                    errors.append(
                        f"{rel_path}: glob '{glob_val}' violates subtree containment. Non-root rules under '{rel_dir}' must only match within '{rel_dir}/**/*'"
                    )
            elif ls and not ls.startswith("#"):
                in_globs = False
    return errors


def _lint_rule_body(rel_path: PurePosixPath, body: str) -> list[str]:
    errors: list[str] = []
    in_section = False
    in_code_block = False

    for i, line in enumerate(body.splitlines(), 1):
        if line.strip().startswith("```"):
            in_code_block = not in_code_block
            continue
        if in_code_block or not line.strip() or line.strip().startswith("# "):
            continue

        ls = line.strip()
        is_header, header_err = _lint_section_header(ls, rel_path, i)
        if is_header:
            in_section = True
            if header_err:
                errors.append(header_err)
            continue

        if ls.startswith("###"):
            errors.append(
                f"{rel_path}:{i}: disallowed header '{ls}'. No H3 or deeper headers allowed."
            )
        elif not in_section:
            errors.append(f"{rel_path}:{i}: floating text outside a section: '{ls[:50]}'")
        elif line.startswith("1.") or re.match(r"^\d+\.", line):
            errors.append(
                f"{rel_path}:{i}: numbered list item found: '{ls[:50]}'. Precedence is implicit from line order; use '- **<Name>**: ...'"
            )
        elif line.startswith("- "):
            bullet_err = _lint_rule_bullet(line, rel_path, i)
            if bullet_err:
                errors.append(bullet_err)
        elif line.startswith(("  ", "\t")):
            errors.append(
                f"{rel_path}:{i}: soft-wrapped continuation line detected ('{ls[:40]}...'). Rules must not be hard-wrapped; keep each rule on a single continuous line."
            )
        else:
            errors.append(f"{rel_path}:{i}: non-bullet line in section: '{ls[:50]}'")

    return errors


def lint_rule_file(rel_path: PurePosixPath, content: str, feat: str, name: str) -> list[str]:
    errors: list[str] = []
    scope_dir = compute_scope_dir(rel_path)
    is_root = len(scope_dir.parts) == 0

    if not name:
        errors.append(
            f"{rel_path}: rule file must have an explicit name prefix: '<name>.{feat}.rulesync.md' (e.g. 'global.{feat}.rulesync.md')"
        )

    if rel_path.parent.name != "agents":
        errors.append(
            f"{rel_path}: rulesync file must be located inside an 'agents/' directory (e.g. '{rel_path.parent}/agents/{rel_path.name}')"
        )

    m = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", content, re.DOTALL)
    if m:
        fm = m.group(1)
        body = m.group(2)
        if re.search(r"^\s*root\s*:", fm, re.MULTILINE):
            errors.append(
                f"{rel_path}: manual 'root' frontmatter is prohibited. Root status is automatically inferred."
            )
        if not is_root:
            errors.extend(_lint_rule_containment(rel_path, fm, scope_dir))
    else:
        body = content

    errors.extend(_lint_rule_body(rel_path, body))
    return errors


def transform_rule_content(content: str, rel_path: PurePosixPath) -> str:
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", content, re.DOTALL)
    if not m:
        fm = _build_default_frontmatter(rel_path)
        body = content
    else:
        fm = _augment_frontmatter(m.group(1), rel_path)
        body = m.group(2)
    return f"---\n{fm.strip()}\n---\n\n{body.lstrip()}"


def _stage_feature_item(
    staging_dir: Path, feat: str, slug: str, content: str, rel_path: PurePosixPath
) -> None:
    feat_dir = staging_dir / feat
    feat_dir.mkdir(parents=True, exist_ok=True)

    if feat == "rules":
        transformed = transform_rule_content(content, rel_path)
        (feat_dir / f"{slug}.md").write_text(transformed, encoding="utf-8")
    elif feat == "skills":
        skill_sub_dir = feat_dir / slug
        skill_sub_dir.mkdir(parents=True, exist_ok=True)
        (skill_sub_dir / "SKILL.md").write_text(content, encoding="utf-8")
    elif feat in {"commands", "subagents"}:
        (feat_dir / f"{slug}.md").write_text(content, encoding="utf-8")
    else:
        (feat_dir / f"{slug}.jsonc").write_text(content, encoding="utf-8")


def _overlay_legacy_dir(staging_dir: Path, legacy_dir: Path, features_present: set[str]) -> None:
    if not legacy_dir.is_dir():
        return
    for feat in SUPPORTED_FEATURES:
        legacy_feat_dir = legacy_dir / feat
        if not legacy_feat_dir.is_dir():
            continue
        features_present.add(feat)
        target_feat_dir = staging_dir / feat
        target_feat_dir.mkdir(parents=True, exist_ok=True)
        for item in legacy_feat_dir.iterdir():
            if item.name.startswith("."):
                continue
            dest = target_feat_dir / item.name
            if item.is_dir():
                if dest.exists():
                    shutil.rmtree(dest)
                shutil.copytree(item, dest)
            elif item.is_file():
                shutil.copy2(item, dest)


def _load_base_config(legacy_config_file: Path, features_present: set[str]) -> dict[str, object]:
    effective_features = features_present or {"rules"}
    active_targets: dict[str, list[str]] = {}
    for target, feats in DEFAULT_TARGET_CONFIG.items():
        filtered = [f for f in feats if f in effective_features]
        if filtered:
            active_targets[target] = filtered

    base_config: dict[str, object] = {
        "targets": active_targets,
        "outputRoots": ["."],
        "delete": False,
    }
    if not legacy_config_file.is_file():
        return base_config

    try:
        raw_lines = [
            line
            for line in legacy_config_file.read_text(encoding="utf-8").splitlines()
            if not line.strip().startswith("//")
        ]
        loaded = json.loads("\n".join(raw_lines))
        if isinstance(loaded, dict):
            base_config.update(loaded)
            base_config["outputRoots"] = ["."]
    except Exception:
        pass
    return base_config


def stage_distributed_rulesync(repo_root: Path) -> tuple[Path, list[str]]:
    staging_dir = repo_root / ".tmp" / "state" / "rulesync"
    if staging_dir.exists():
        shutil.rmtree(staging_dir)
    staging_dir.mkdir(parents=True, exist_ok=True)

    discovered_files = discover_rulesync_files(repo_root)
    features_present: set[str] = set()
    lint_errors: list[str] = []

    for rel_path in discovered_files:
        matched = match_rulesync_feature(rel_path.name)
        if not matched:
            continue
        feat, name = matched
        features_present.add(feat)
        content = (repo_root / rel_path).read_text(encoding="utf-8")

        m = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", content, re.DOTALL)
        fm = m.group(1) if m else None
        is_global = feat == "rules" and is_global_rule(rel_path, fm)
        slug = compute_slug(rel_path, feat, name, is_global=is_global)

        if feat == "rules":
            errors = lint_rule_file(rel_path, content, feat, name)
            if errors:
                lint_errors.extend(errors)

        _stage_feature_item(staging_dir, feat, slug, content, rel_path)

    if lint_errors:
        for err in lint_errors:
            sys.stderr.write(f"ERROR: {err}\n")
        raise ValueError(f"RuleSync lint failed with {len(lint_errors)} error(s)")

    config_file = repo_root / "src/bazel/checks/rulesync/rulesync.jsonc"
    cfg = _load_base_config(config_file, features_present)
    (staging_dir / "rulesync.jsonc").write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    return staging_dir, sorted(features_present)


def _find_rulesync_binary() -> str:
    rulesync_bin = shutil.which("rulesync")
    if rulesync_bin:
        return rulesync_bin

    if runfiles:
        try:
            r = runfiles.Create()
            if r:
                candidate = r.Rlocation(
                    "rules_multitool++multitool+multitool/tools/rulesync/rulesync"
                )
                if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
                    return candidate
        except Exception:
            pass

    runfiles_dir = os.environ.get("RUNFILES_DIR")
    if runfiles_dir:
        for p in Path(runfiles_dir).glob("**/tools/rulesync/rulesync"):
            if p.is_file() and os.access(p, os.X_OK):
                return str(p)

    home = Path.home()
    candidates = [
        home / ".local/share/mise/shims/rulesync",
        Path("/opt/homebrew/bin/rulesync"),
        Path("/usr/local/bin/rulesync"),
    ]
    candidates.extend(
        home.glob(".local/share/mise/installs/npm-rulesync/*/node_modules/.bin/rulesync")
    )
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)

    return "rulesync"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", action="store_true", help="Check if generated guidance is up to date"
    )
    parser.add_argument("--fix", "--write", action="store_true", help="Regenerate guidance files")
    parser.add_argument("hook_args", nargs="*", help="Arguments git passes to a hook; ignored")
    args = parser.parse_args(argv)

    repo_root = find_repo_root()
    os.chdir(repo_root)

    staging_dir, _features = stage_distributed_rulesync(repo_root)

    rulesync_bin = _find_rulesync_binary()

    cmd = [
        rulesync_bin,
        "generate",
        "--config",
        str(staging_dir / "rulesync.jsonc"),
        "--input-roots",
        str(staging_dir),
        "--output-roots",
        ".",
    ]

    if args.check:
        cmd.append("--check")

    result = subprocess.run(
        cmd, cwd=repo_root, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True
    )
    if result.returncode != 0 and result.stderr:
        sys.stderr.write(result.stderr)
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
