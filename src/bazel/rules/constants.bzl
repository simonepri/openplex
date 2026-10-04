"""Define canonical file extensions, ignore patterns, and path specifications used across build tooling."""

# LINT.IfChange(gazelle_extensions)
GAZELLE_PATH_SPECS = [
    "*.bzl",
    "*.css",
    "*.go",
    "*.js",
    "*.jsx",
    "*.mjs",
    "*.py",
    "*.tf",
    "*.tfvars",
    "*.ts",
    "*.tsx",
    "BUILD.bazel",
    "Chart.yaml",
    "MODULE.bazel",
    "go.mod",
    "go.sum",
    "kustomization.yaml",
    "package.json",
    "pyproject.toml",
    "requirements_dev_lock.txt",
    "requirements_lock.txt",
    "tsconfig.json",
]
# LINT.ThenChange(//src/bazel/docs/architecture.md:gazelle_extensions)

ARGOCD_LINKS_PATH_SPECS = [
    "src/infra/argocd/**",
    "src/infra/definitions/observability/**",
]

CODEOWNERS_PATH_SPECS = [
    ":(glob)**/CODEOWNERS",
    ":(glob)CODEOWNERS",
    ".github/CODEOWNERS",
    "src/infra/definitions/teams/**",
]

# LINT.IfChange(dotenv_path_specs)
DOTENV_PATH_SPECS = [
    ":(glob)**/.env",
    ":(glob)**/.env.*",
    ":(glob)**/*.env",
    ":(glob)**/*.env.*",
]
# LINT.ThenChange(//src/bazel/checks/dotenv/dotenv.sh:dotenv_path_specs)

RULESYNC_PATH_SPECS = [
    "*.rulesync.md",
    "**/*.rulesync.md",
    "src/bazel/checks/rulesync/**",
    "AGENTS.md",
    "CLAUDE.md",
]

PACKAGE_OVERRIDES_PATH_SPECS = [
    "package.json",
    "pnpm-lock.yaml",
    "pnpm-workspace.yaml",
    "src/bazel/checks/package_overrides/**",
]
