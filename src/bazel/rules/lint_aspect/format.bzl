"""Define the workspace formatting runner generating and formatting code across the repository."""

# Pushes to main diff against the commit the push replaced instead of forcing all-mode.
# If the replaced commit cannot be found in the repository (e.g. after a force-push),
# fall back to all-mode. BuildBuddy Workflows export CI=true, GIT_BRANCH, and GIT_PR_NUMBER (0 on push);
# GitHub Actions exports GITHUB_ACTIONS=true, GITHUB_EVENT_NAME, and GITHUB_REF_NAME.
# LINT.IfChange(ci_push_to_main)
_CI_PUSH_TO_MAIN_LINES = [
    'if { [ "${CI:-}" = "true" ] && [ "${GIT_BRANCH:-}" = "main" ] && [ "${GIT_PR_NUMBER:-0}" = "0" ]; } || { [ "${GITHUB_ACTIONS:-}" = "true" ] && [ "${GITHUB_EVENT_NAME:-}" = "push" ] && [ "${GITHUB_REF_NAME:-}" = "main" ]; }; then',
    '  ci_base="${BEFORE_COMMIT:-${GIT_PREVIOUS_COMMIT:-${GIT_BEFORE:-${BEFORE:-${GITHUB_BEFORE:-}}}}}"',
    '  ci_base="${ci_base:-HEAD~1}"',
    '  if git rev-parse --verify --quiet "$ci_base^{commit}" >/dev/null 2>&1; then',
    '    diff_base="$(git rev-parse "$ci_base^{commit}")"',
    "  else",
    '    mode="all"',
    "  fi",
    "fi",
    "",
]
# LINT.ThenChange(//src/bazel/tools/diff/git_diff.py:ci_push_to_main)

def _render_nested_bazel_env(label_name):
    # `bazel run` starts the binary in its runfiles tree under
    # <output_base>/execroot/, and <output_base>/install links to the server's
    # install base under <output_user_root>/install/. A `bazel` shim first on
    # PATH passes that exact output base to every nested command, and the
    # derived root keeps the install base, so nested commands from shell and
    # Python reach the running server even when a wrapper chose the output
    # base, as BuildBuddy runners do.
    # They stream to the same BES backend as the outer command; the metadata
    # tags them for BuildBuddy and keeps them from posting commit statuses.
    return [
        # LINT.IfChange(nested_output_root)
        'output_base="${runfiles%%/execroot/*}"',
        'export BAZEL_OUTPUT_ROOT="$(dirname "$(dirname "$(readlink "$output_base/install")")")"',
        'bazel_shim_dir="$(mktemp -d "${TMPDIR:-/tmp}/bazel-nested.XXXXXX")"',
        'printf "#!/bin/sh\\nexec \'%s\' --output_base=\'%s\' \\"\\$@\\"\\n" "$(command -v bazel)" "$output_base" >"$bazel_shim_dir/bazel"',
        'chmod +x "$bazel_shim_dir/bazel"',
        'export PATH="$bazel_shim_dir:$PATH"',
        # LINT.ThenChange(//src/bazel/rules/git_hooks/defs.bzl:nested_output_root)
        'export BAZEL_CONFIG_FLAGS="${BAZEL_CONFIG_FLAGS:-} --build_metadata=TAGS=nested,%s --build_metadata=DISABLE_COMMIT_STATUS_REPORTING=true"' % label_name,
    ]

def _render_format_preamble(label_name):
    return [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        "",
        'if [ -n "${RUNFILES_DIR:-}" ] && [ -d "$RUNFILES_DIR/_main" ]; then',
        '  runfiles="$RUNFILES_DIR/_main"',
        'elif [ -n "${RUNFILES_DIR:-}" ] && [ -d "$RUNFILES_DIR" ]; then',
        '  runfiles="$RUNFILES_DIR"',
        'elif [ -d "$0.runfiles/_main" ]; then',
        '  runfiles="$0.runfiles/_main"',
        'elif [ -d "$0.runfiles" ]; then',
        '  runfiles="$0.runfiles"',
        "else",
        '  runfiles="$PWD"',
        "fi",
        'cd "${BUILD_WORKSPACE_DIRECTORY:?%s must be run with bazel run}"' % label_name,
        "",
        'job_dir=$(mktemp -d "${TMPDIR:-/tmp}/fix_jobs.XXXXXX")',
        "trap 'rm -rf \"$job_dir\"' EXIT",
        'running_jobs=""',
        "",
        "# Runs a command in the background with its output captured.",
        "spawn() {",
        '  local name="$1"',
        "  shift",
        '  ( "$@" ) >"$job_dir/$name.log" 2>&1 &',
        '  running_jobs="$running_jobs $name:$!"',
        "}",
        "",
        "# Waits for every spawned command and prints the output of failed ones.",
        "wait_jobs() {",
        "  local failed=0 job name",
        "  for job in $running_jobs; do",
        '    name="${job%%:*}"',
        '    if ! wait "${job#*:}"; then',
        '      echo "FAILED: $name" >&2',
        '      cat "$job_dir/$name.log" >&2',
        "      failed=1",
        "    fi",
        "  done",
        '  running_jobs=""',
        '  [ "$failed" -eq 0 ] || exit 1',
        "}",
        "",
        'export REPO_CACHE_DIR="${XDG_CACHE_HOME:-$HOME/.cache}/repo"',
        'export RUMDL_CACHE_DIR="${RUMDL_CACHE_DIR:-$REPO_CACHE_DIR/rumdl}"',
        'export PYTHONPYCACHEPREFIX="${PYTHONPYCACHEPREFIX:-$REPO_CACHE_DIR/python}"',
        'export RUFF_CACHE_DIR="${RUFF_CACHE_DIR:-$REPO_CACHE_DIR/ruff}"',
        'export BAZEL_BINDIR="${BAZEL_BINDIR:-.}"',
    ] + _render_nested_bazel_env(label_name) + [
        "",
        "check_mode=0",
        'mode="affected"',
        'target=""',
        "",
        'for arg in "$@"; do',
        '  case "$arg" in',
        "    --check|--fail)",
        "      check_mode=1",
        "      ;;",
        "    --staged)",
        '      mode="staged"',
        "      ;;",
        "    --all|-a|.)",
        '      mode="all"',
        "      ;;",
        "    *)",
        '      if [ -z "$target" ]; then',
        '        target="$arg"',
        '        mode="path"',
        "      fi",
        "      ;;",
        "  esac",
        "done",
        "",
    ] + _CI_PUSH_TO_MAIN_LINES + [
        'export FIX_MODE="$mode"',
        "",
        'if [ "$mode" = "staged" ]; then',
        "  changed_all=$(git diff --cached --name-only --diff-filter=ACMR 2>/dev/null || true)",
        "else",
        '  diff_base="${diff_base:-$(git merge-base HEAD origin/main 2>/dev/null || git merge-base HEAD main 2>/dev/null || echo HEAD)}"',
        '  changed_all=$({ git diff --name-only "$diff_base" 2>/dev/null || true; git ls-files --others --exclude-standard; } | sort -u)',
        "fi",
        'export CHANGED_ALL="${changed_all:-}"',
        "",
        "pre_diff=$(git diff 2>/dev/null || true)",
        "pre_status=$(git status --porcelain 2>/dev/null || true)",
        "",
    ]

def _render_format_generator_tools(pnpm_path = "", uv_path = "", rulesync_path = ""):
    return [
        'if [ -n "%s" ] && [ -x "$runfiles/%s" ]; then' % (pnpm_path, pnpm_path),
        '  pnpm_cmd="$runfiles/%s"' % pnpm_path,
        "else",
        '  pnpm_cmd="pnpm"',
        "fi",
        'if [ -n "%s" ] && [ -x "$runfiles/%s" ]; then' % (uv_path, uv_path),
        '  uv_cmd="$runfiles/%s"' % uv_path,
        "else",
        '  uv_cmd="uv"',
        "fi",
        'if [ -n "%s" ] && [ -x "$runfiles/%s" ]; then' % (rulesync_path, rulesync_path),
        '  rulesync_bin="$runfiles/%s"' % rulesync_path,
        '  export PATH="$(dirname "$rulesync_bin"):$PATH"',
        "fi",
        "",
    ]

def _render_format_generator_meta():
    return [
        "generator_meta=$(python3 src/bazel/rules/lint_aspect/generator_meta.py)",
        "",
        'active_generators=""',
        "gazelle_dirs=()",
        'if [ -n "$generator_meta" ]; then',
        "  first_line=1",
        "  while IFS= read -r line; do",
        '    if [ "$first_line" -eq 1 ]; then',
        '      active_generators="$line"',
        "      first_line=0",
        '    elif [ -n "$line" ]; then',
        '      gazelle_dirs+=("$line")',
        "    fi",
        '  done <<< "$generator_meta"',
        "fi",
        "",
    ]

def _render_format_generator_jobs():
    return [
        "gen_pnpm() {",
        '  "$pnpm_cmd" --dir "$PWD" install --lockfile-only --ignore-scripts --silent',
        "}",
        "gen_uv() {",
        '  "$uv_cmd" pip compile --quiet pyproject.toml --python-version 3.13 --universal --no-strip-extras --generate-hashes -o requirements_lock.txt',
        '  "$uv_cmd" pip compile --quiet --group dev --python-version 3.13 --universal --no-strip-extras --generate-hashes -o requirements_dev_lock.txt',
        "}",
        "gen_team_records() {",
        '  "$uv_cmd" run --quiet --only-group dev python3 src/bazel/checks/records/check_team_records.py --write-workload-images src/infra/images/workload-images.json --write-codeowners .github/CODEOWNERS',
        "}",
        "gen_rulesync() {",
        "  python3 src/bazel/checks/rulesync/rulesync_scan.py --fix",
        "}",
        "gen_bazel() {",
        '  local expr="$*" bins bin pid pids="" failed=0',
        # The binaries run directly below, so their outputs and runfiles must be
        # on local disk even when the CI config only keeps outputs remote.
        '  bazel --output_user_root="${BAZEL_OUTPUT_ROOT}" build ${BAZEL_CONFIG_FLAGS:-} --remote_download_outputs=toplevel --ui_event_filters=-info,-stdout --noshow_progress "$@"',
        '  bins=$(bazel --output_user_root="${BAZEL_OUTPUT_ROOT}" cquery ${BAZEL_CONFIG_FLAGS:-} --ui_event_filters=-info --noshow_progress --output=starlark --starlark:expr=\'providers(target)["FilesToRunProvider"].executable.path\' "${expr// / + }")',
        "  for bin in $bins; do",
        '    case "$bin" in',
        "      */artwork/*) set -- --fix ;;",
        "      *gazelle*)",
        '        if [ "$mode" = "all" ] || [ ${#gazelle_dirs[@]} -eq 0 ]; then',
        "          set --",
        "        else",
        '          set -- "${gazelle_dirs[@]}"',
        "        fi",
        "        ;;",
        "      *) set -- ;;",
        "    esac",
        '    BUILD_WORKSPACE_DIRECTORY="$PWD" "${runfiles%%/bazel-out/*}/$bin" "$@" &',
        '    pids="$pids $!"',
        "  done",
        '  for pid in $pids; do wait "$pid" || failed=1; done',
        '  return "$failed"',
        "}",
        "",
    ]

def _render_format_generator_dispatch():
    return [
        'if [ "$mode" != "staged" ]; then',
        '  bazel_targets=""',
        "  for gen in $active_generators; do",
        '    case "$gen" in',
        "      pnpm) spawn pnpm gen_pnpm ;;",
        "      uv) spawn uv gen_uv ;;",
        "    esac",
        "  done",
        "  wait_jobs",
        "  ran_gazelle=0",
        "  for gen in $active_generators; do",
        '    case "$gen" in',
        "      team_records) spawn team_records gen_team_records ;;",
        "      rulesync) spawn rulesync gen_rulesync ;;",
        "      gazelle)",
        '        bazel_targets="$bazel_targets //:gazelle"',
        "        ran_gazelle=1",
        "        ;;",
        '      artwork) bazel_targets="$bazel_targets //src/infra/docs/artwork:logo //src/infra/docs/artwork:overview" ;;',
        "    esac",
        "  done",
        '  if [ -n "$bazel_targets" ]; then',
        "    spawn bazel gen_bazel $bazel_targets",
        "  fi",
        "  wait_jobs",
        "  ",
        '  if [ "$ran_gazelle" -eq 1 ]; then',
        "    python3 src/bazel/rules/lint_aspect/build_headers.py",
        "  fi",
        "fi",
        "",
    ]

def _render_format_generators(pnpm_path = "", uv_path = "", rulesync_path = ""):
    lines = []
    lines.extend(_render_format_generator_tools(pnpm_path, uv_path, rulesync_path))
    lines.extend(_render_format_generator_meta())
    lines.extend(_render_format_generator_jobs())
    lines.extend(_render_format_generator_dispatch())
    return lines

def _render_format_drift_check():
    return [
        'if [ "$check_mode" -eq 1 ]; then',
        '  if [ "$mode" = "staged" ]; then',
        "    staged_drift=()",
        "    while IFS= read -r f; do",
        '      if [ -n "$f" ]; then',
        '        if ! git diff --quiet -- "$f" 2>/dev/null; then',
        '          staged_drift+=("$f")',
        "        fi",
        "      fi",
        '    done <<< "$changed_all"',
        "    if [ ${#staged_drift[@]} -gt 0 ]; then",
        '      echo "ERROR: Drift detected! The following staged files require formatting:" >&2',
        '      for f in "${staged_drift[@]}"; do echo "  $f" >&2; done',
        '      git --no-pager diff -- "${staged_drift[@]}" >&2 || true',
        "      exit 1",
        "    fi",
        "  else",
        "    post_diff=$(git diff 2>/dev/null || true)",
        "    post_status=$(git status --porcelain 2>/dev/null || true)",
        '    if [ "$post_diff" != "$pre_diff" ] || [ "$post_status" != "$pre_status" ]; then',
        '      echo "ERROR: Drift detected! Files require formatting or generated artifacts are out of date:" >&2',
        "      git --no-pager diff >&2 || true",
        '      if [ -z "$pre_status" ]; then',
        "        git checkout -- . 2>/dev/null || true",
        "        git clean -fd 2>/dev/null || true",
        "      fi",
        "      exit 1",
        "    fi",
        "  fi",
        '  echo "Drift check passed: all files are formatted and generated artifacts are up to date."',
        "fi",
        "",
    ]

def _is_whole_tree(spec):
    patterns = spec.partition("|")[0]
    return not patterns or patterns == "''" or patterns == "'*'"

def _render_format_tool(tool, spec, index):
    patterns, _, rest = spec.partition("|")
    args, _, per_file = rest.partition("|")
    executable = tool[DefaultInfo].files_to_run.executable
    head = "fmt_%d() {" % index
    if not patterns or patterns == "''":
        return [
            head,
            '  if [ "$mode" = "all" ]; then',
            '    "$runfiles/%s" %s' % (executable.short_path, args),
            "  fi",
            "}",
            "",
        ]
    return [
        head,
        "  local files",
        '  if [ "$mode" = "all" ]; then',
        '    files=$(git ls-files --cached --others --exclude-standard -- %s | while IFS= read -r f; do if [ -f "$f" ]; then printf "%%s\\n" "$f"; fi; done)' % patterns,
        '  elif [ "$mode" = "path" ]; then',
        '    clean_target="${target#/}"',
        '    clean_target="${clean_target#/}"',
        '    clean_target="${clean_target%/...}"',
        '    files=$(git ls-files --cached --others --exclude-standard -- %s | while IFS= read -r f; do if [ -f "$f" ] && [[ "$f" == "$clean_target"* ]]; then printf "%%s\\n" "$f"; fi; done)' % patterns,
        '  elif [ "$mode" = "staged" ]; then',
        '    files=$(git diff --cached --name-only --diff-filter=ACMR -- %s 2>/dev/null | while IFS= read -r f; do if [ -f "$f" ]; then printf "%%s\\n" "$f"; fi; done)' % patterns,
        "  else",
        '    files=$({ git diff --name-only "$diff_base" -- %s 2>/dev/null || true; git ls-files --others --exclude-standard -- %s; } | while IFS= read -r f; do if [ -f "$f" ]; then printf "%%s\\n" "$f"; fi; done)' % (patterns, patterns),
        "  fi",
        '  if [ -n "$files" ]; then',
        '    printf "%%s\\n" "$files" | tr "\\n" "\\0" | xargs -0 %s"$runfiles/%s" %s' % (
            "-n1 -P 8 " if per_file == "per_file" else "",
            executable.short_path,
            args,
        ),
        "  fi",
        "}",
        "",
    ]

def _render_format_schedule(specs):
    # A tool matching every file runs alone and first so that tools matching
    # disjoint file sets can then run concurrently without racing on a file.
    lines = []
    for index, spec in enumerate(specs):
        if _is_whole_tree(spec):
            lines.append("fmt_%d" % index)
    for index, spec in enumerate(specs):
        if not _is_whole_tree(spec):
            lines.append("spawn fmt_%d fmt_%d" % (index, index))
    lines.extend(["wait_jobs", ""])
    return lines

def _format_all_impl(ctx):
    launcher = ctx.actions.declare_file(ctx.label.name + ".sh")
    pnpm_exe = ctx.attr.pnpm[DefaultInfo].files_to_run.executable if ctx.attr.pnpm else None
    pnpm_path = pnpm_exe.short_path if pnpm_exe else ""
    uv_exe = ctx.attr.uv[DefaultInfo].files_to_run.executable if ctx.attr.uv else None
    uv_path = uv_exe.short_path if uv_exe else ""
    rulesync_exe = ctx.attr.rulesync[DefaultInfo].files_to_run.executable if ctx.attr.rulesync else None
    rulesync_path = rulesync_exe.short_path if rulesync_exe else ""
    lines = _render_format_preamble(ctx.label.name)
    lines.extend(_render_format_generators(pnpm_path = pnpm_path, uv_path = uv_path, rulesync_path = rulesync_path))
    for index, (tool, spec) in enumerate(zip(ctx.attr.tools, ctx.attr.specs)):
        lines.extend(_render_format_tool(tool, spec, index))
    lines.extend(_render_format_schedule(ctx.attr.specs))
    lines.extend(_render_format_drift_check())
    ctx.actions.write(output = launcher, content = "\n".join(lines) + "\n", is_executable = True)
    tools_to_run = list(ctx.attr.tools)
    if ctx.attr.pnpm:
        tools_to_run.append(ctx.attr.pnpm)
    if ctx.attr.uv:
        tools_to_run.append(ctx.attr.uv)
    if ctx.attr.rulesync:
        tools_to_run.append(ctx.attr.rulesync)
    runfiles = ctx.runfiles().merge_all([t[DefaultInfo].default_runfiles for t in tools_to_run])
    runfiles = runfiles.merge(ctx.runfiles(files = [
        t[DefaultInfo].files_to_run.executable
        for t in tools_to_run
    ]))
    return [DefaultInfo(executable = launcher, runfiles = runfiles)]

_format_all = rule(
    implementation = _format_all_impl,
    doc = "Runs every declared formatter in write mode against the source tree.",
    attrs = {
        "pnpm": attr.label(
            default = "//src/bazel/tools:pnpm",
            executable = True,
            cfg = "exec",
        ),
        "rulesync": attr.label(
            default = "//src/bazel/tools:rulesync",
            executable = True,
            cfg = "exec",
        ),
        "specs": attr.string_list(mandatory = True, doc = "'<git pathspecs>|<args>' per tool."),
        "tools": attr.label_list(mandatory = True, cfg = "exec", providers = [DefaultInfo]),
        "uv": attr.label(
            default = "//src/bazel/tools:uv",
            executable = True,
            cfg = "exec",
        ),
    },
    executable = True,
)

def format_all(name, formatters, pnpm = "//src/bazel/tools:pnpm", uv = "//src/bazel/tools:uv", rulesync = "//src/bazel/tools:rulesync", **kwargs):
    """Declare the repository formatter.

    Args:
        name: target name; run it with `bazel run //:<name>`.
        formatters: list of (tool label, git pathspecs, argument string) with an
            optional fourth element, True when the tool takes one path at a time.
        pnpm: label of the pnpm executable target.
        uv: label of the uv executable target.
        **kwargs: forwarded to the target.
    """
    _format_all(
        name = name,
        pnpm = pnpm,
        uv = uv,
        tools = [entry[0] for entry in formatters],
        specs = [
            "%s|%s|%s" % (patterns, args, "per_file" if len(entry) > 3 and entry[3] else "")
            for entry in formatters
            for patterns, args in [(entry[1], entry[2])]
        ],
        **kwargs
    )
