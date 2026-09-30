"""Define reusable lint aspect factories attaching static analysis tools across the Bazel build graph."""

load("@rules_python//python:defs.bzl", "PyInfo")

def _visits(rule, kinds, tags):
    """Match by rule kind where Bazel has one, by content tag where it does not."""
    if rule.kind in kinds:
        return True
    for tag in tags:
        if tag in rule.attr.tags:
            return True
    return False

def _multi_lint_aspect_impl(target, ctx):
    _ = target  # buildifier: disable=unused-variable
    if not _visits(ctx.rule, ctx.attr._kinds, ctx.attr._tags):
        return []
    srcs = [src for src in getattr(ctx.rule.files, "srcs", []) if src.is_source]
    if ctx.attr._extensions:
        srcs = [src for src in srcs if src.extension in ctx.attr._extensions]
    if not srcs:
        return []

    extra_inputs = []
    search_paths = []
    if ctx.attr._needs_deps and PyInfo in target:
        info = target[PyInfo]
        extra_inputs = info.transitive_sources.to_list()
        search_paths = [
            "external/" + entry if not entry.startswith("_main") else entry.replace("_main/", "", 1)
            for entry in info.imports.to_list()
        ]

    rule_tags = getattr(ctx.rule.attr, "tags", [])
    reports = []

    for spec_str in ctx.attr._specs:
        spec = json.decode(spec_str)
        step_tag = spec.get("tag", "")
        if step_tag and step_tag not in rule_tags:
            continue

        step_extensions = spec.get("extensions", [])
        if step_extensions:
            step_srcs = [s for s in srcs if s.extension in step_extensions]
        else:
            step_srcs = srcs

        if not step_srcs:
            continue

        tool_target = ctx.attr._tools[spec["tool_idx"]]
        tool_to_run = tool_target[DefaultInfo].files_to_run
        tool_path = tool_to_run.executable.path

        step_configs = [ctx.files._configs[i] for i in spec.get("config_indices", [])]
        step_name = spec["name"]
        report = ctx.actions.declare_file("{}.{}.lint".format(ctx.label.name, step_name))

        if spec.get("per_file", False):
            if spec.get("chdir", False):
                command = """
status=0
: >"{report}"
root="$PWD"
for file in "$@"; do
  dir=$(dirname "$file")
  base=$(basename "$file")
  (cd "$dir" && "$root/{tool}" {tool_args} "$base") >>"{report}" 2>&1 || status=1
done
[ "$status" -eq 0 ] || {{ cat "{report}" >&2; exit 1; }}
""".format(
                    tool = tool_path,
                    tool_args = " ".join(spec["args"]),
                    report = report.path,
                )
            else:
                command = """
status=0
: >"{report}"
for file in "$@"; do
  "{tool}" {tool_args} "$file" >>"{report}" 2>&1 || status=1
done
[ "$status" -eq 0 ] || {{ cat "{report}" >&2; exit 1; }}
""".format(
                    tool = tool_path,
                    tool_args = " ".join(spec["args"]),
                    report = report.path,
                )
            args = ctx.actions.args()
            args.add_all(step_srcs)

        else:
            command = '"{tool}" "$@" >"{report}" 2>&1 || {{ cat "{report}" >&2; exit 1; }}'.format(
                tool = tool_path,
                report = report.path,
            )
            args = ctx.actions.args()
            args.add_all(spec["args"])
            args.add_all(step_srcs)

        env = {
            "BAZEL_BINDIR": ".",
            "PATH": "/bin:/usr/bin",
        }

        step_inputs = step_srcs + step_configs
        if spec.get("needs_deps", False):
            if search_paths:
                env["PYTHONPATH"] = ":".join(search_paths)
            step_inputs = step_inputs + extra_inputs

        ctx.actions.run_shell(
            inputs = step_inputs,
            tools = [tool_to_run],
            outputs = [report],
            arguments = [args],
            command = command,
            env = env,
            mnemonic = "Lint" + step_name.replace("-", "").capitalize(),
            progress_message = "Linting %{label} with " + step_name,
        )
        reports.append(report)

    if not reports:
        return []

    return [OutputGroupInfo(lint = depset(reports), _validation = depset(reports))]

def multi_lint_aspect(name, steps, rule_kinds = [], rule_tags = [], extensions = []):
    """Declare a multiplexed static-analysis aspect running multiple tools over matching targets.

    Args:
        name: aspect name.
        steps: list of step dictionaries:
            name: step identifier.
            tool: tool executable target label.
            tool_args: list of arguments.
            config: list of configuration labels.
            per_file: bool, invoke tool once per file.
            needs_deps: bool, stage PyInfo dependencies.
            tag: optional tag filter.
            extensions: optional file extension filter.
        rule_kinds: rule kinds this aspect visits.
        rule_tags: content tags this aspect visits.
        extensions: file extensions this aspect filters on.

    Returns:
        The configured aspect.
    """
    tools = []
    tool_to_idx = {}
    configs = []
    config_to_idx = {}
    specs = []
    needs_deps_any = False

    for step in steps:
        tool_target = step["tool"]
        if tool_target not in tool_to_idx:
            tool_to_idx[tool_target] = len(tools)
            tools.append(tool_target)
        tool_idx = tool_to_idx[tool_target]

        step_config = step.get("config", [])
        config_indices = []
        for c in step_config:
            if c not in config_to_idx:
                config_to_idx[c] = len(configs)
                configs.append(c)
            config_indices.append(config_to_idx[c])

        needs_deps = step.get("needs_deps", False)
        if needs_deps:
            needs_deps_any = True

        specs.append(json.encode({
            "args": step.get("tool_args", []),
            "chdir": step.get("chdir", False),
            "config_indices": config_indices,
            "extensions": step.get("extensions", []),
            "name": step["name"],
            "needs_deps": needs_deps,
            "per_file": step.get("per_file", False),
            "tag": step.get("tag", ""),
            "tool_idx": tool_idx,
        }))

    return aspect(
        implementation = _multi_lint_aspect_impl,
        attrs = {
            "_configs": attr.label_list(allow_files = True, default = configs),
            "_extensions": attr.string_list(default = extensions),
            "_kinds": attr.string_list(default = rule_kinds),
            "_name": attr.string(default = name),
            "_needs_deps": attr.bool(default = needs_deps_any),
            "_specs": attr.string_list(default = specs),
            "_tags": attr.string_list(default = rule_tags),
            "_tools": attr.label_list(default = tools, cfg = "exec", allow_files = True),
        },
    )

def lint_aspect(name, tool, tool_args = [], rule_kinds = [], rule_tags = [], extensions = [], config = [], per_file = False, needs_deps = False):
    """Declare one tool once; it visits every target that says it holds such files.

    Args:
        name: short name used in the report filename and progress message.
        tool: label of the executable, normally an alias in //src/bazel/tools.
        tool_args: arguments placed before the file list.
        rule_kinds: rule kinds this tool reads, such as sh_library.
        rule_tags: content tags from sources.bzl, for content with no rule kind.
        extensions: restrict to these file extensions; empty means every src.
        config: files the tool reads but is not run over.
        per_file: invoke once per file, for tools that do not verdict on a whole list.
        needs_deps: also stage the target's deps, for tools that resolve imports.
    """
    return multi_lint_aspect(
        name = name,
        steps = [{
            "config": config,
            "name": name,
            "needs_deps": needs_deps,
            "per_file": per_file,
            "tool": tool,
            "tool_args": tool_args,
        }],
        rule_kinds = rule_kinds,
        rule_tags = rule_tags,
        extensions = extensions,
    )

# No tool instance is declared yet. helmfmt was evaluated and rejected: it
# indents `{{- ... -}}` control blocks that must sit at column 0, which changes
# whitespace chomping and broke fleet/team/templates/promotion.yaml so the chart
# no longer parsed. The machinery above is validated end to end against it
# (hermetic tool from multitool, tag-matched filegroup, strict action env, build
# failure with readable findings); only the tool was at fault.

def _runfiles_path(ctx, file):
    if file.short_path.startswith("../"):
        return file.short_path[3:]
    return ctx.workspace_name + "/" + file.short_path

def _tool_runner_impl(ctx):
    # The runner gives its binary the environment of a runfiles tree it owns:
    # RUNFILES_DIR set, the tools on PATH by label name, and the argument tool
    # as the first argument.
    launcher = ctx.actions.declare_file(ctx.label.name + ".sh")
    lines = [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        'export RUNFILES_DIR="$0.runfiles"',
        'export BAZEL_BINDIR="${BAZEL_BINDIR:-.}"',
    ]
    if ctx.attr.tools:
        lines.extend([
            'tools_bin="$(mktemp -d "${TMPDIR:-/tmp}/runner_tools.XXXXXX")"',
            "trap 'rm -rf \"$tools_bin\"' EXIT",
        ])
        for tool in ctx.attr.tools:
            lines.append('ln -s "$RUNFILES_DIR/%s" "$tools_bin/%s"' % (
                _runfiles_path(ctx, tool[DefaultInfo].files_to_run.executable),
                tool.label.name,
            ))
        lines.append('export PATH="$tools_bin:$PATH"')
    command = ['"$RUNFILES_DIR/%s"' % _runfiles_path(ctx, ctx.executable.binary)]
    if ctx.attr.argument:
        command.append('"$RUNFILES_DIR/%s"' % _runfiles_path(ctx, ctx.executable.argument))
    command.append('"$@"')
    lines.append(" ".join(command if ctx.attr.tools else ["exec"] + command))
    ctx.actions.write(output = launcher, content = "\n".join(lines) + "\n", is_executable = True)

    targets = [ctx.attr.binary] + ctx.attr.tools + ([ctx.attr.argument] if ctx.attr.argument else [])
    runfiles = ctx.runfiles(files = [t[DefaultInfo].files_to_run.executable for t in targets])
    runfiles = runfiles.merge_all([t[DefaultInfo].default_runfiles for t in targets])
    return [DefaultInfo(executable = launcher, runfiles = runfiles)]

# //:check names its runners instead of depending on them, and builds only
# the runners a run needs: a dependency would make `bazel run` fetch and
# stage every tool before the launcher knows which ones it will use.
_tool_runner = rule(
    implementation = _tool_runner_impl,
    doc = "Runs one binary from its own runfiles tree, with tools on PATH.",
    attrs = {
        "argument": attr.label(executable = True, cfg = "target", doc = "Tool passed as the first argument"),
        "binary": attr.label(mandatory = True, executable = True, cfg = "target"),
        "tools": attr.label_list(cfg = "target", providers = [DefaultInfo], doc = "Tools placed on PATH by label name"),
    },
    executable = True,
)

def _runner_name(launcher_name, label):
    return "%s.%s" % (launcher_name, label.lstrip("/").replace("/", "_").replace(":", "_"))

def _runner_target(ctx, runner):
    """Return the label to build and the bin-relative executable path of a same-package runner."""
    package = ctx.label.package + "/" if ctx.label.package else ""
    return {
        "label": "//%s:%s" % (ctx.label.package, runner),
        "path": package + runner + ".sh",
    }

def _render_runner_bin_dir(launcher):
    # Runners sit beside the launcher in the same configuration, which the
    # nested commands share through BAZEL_CONFIG_FLAGS.
    return ['bin_dir="${runfiles%%/%s.runfiles/*}"' % launcher.short_path]

# Fix mode. A Bazel action cannot write to the source tree, so formatting cannot
# be an aspect; it is a run target that edits the workspace directly. The tool
# and its arguments come from the same declaration the aspect uses, so check and
# fix can never disagree about how a file should look.

# BuildBuddy Workflows export CI=true, GIT_BRANCH, and GIT_PR_NUMBER (0 on push). A push to main
# has an empty merge-base diff, so every gate runs on everything.
_CI_PUSH_TO_MAIN_LINES = [
    'if [ "${CI:-}" = "true" ] && [ "${GIT_BRANCH:-}" = "main" ] && [ "${GIT_PR_NUMBER:-0}" = "0" ]; then',
    '  mode="all"',
    "fi",
    "",
]

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
        "runfiles=\"$PWD\"",
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
        "diff_base=$(git merge-base HEAD origin/main 2>/dev/null || git merge-base HEAD main 2>/dev/null || echo HEAD)",
        'changed_all=$({ git diff --name-only "$diff_base" 2>/dev/null || true; git ls-files --others --exclude-standard; } | sort -u)',
        'export CHANGED_ALL="${changed_all:-}"',
        "",
        "pre_diff=$(git diff 2>/dev/null || true)",
        "pre_status=$(git status --porcelain 2>/dev/null || true)",
        "",
    ]

def _render_format_generators(pnpm_path = "", uv_path = "", rulesync_path = ""):
    # Each generator runs at most once per invocation. The lockfile generators
    # run first because the Bazel build below reads them; the rest run
    # concurrently. One `bazel build` serves every Bazel-based generator, whose
    # binaries then run directly.
    lines = [
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
        "active_generators=$(python3 - << 'PYEOF'",
        "import os, sys",
        'raw_changed = os.environ.get("CHANGED_ALL", "")',
        'mode = os.environ.get("FIX_MODE", "affected")',
        "changed_files = [line.strip() for line in raw_changed.splitlines() if line.strip()]",
        "try:",
        "    from src.bazel.tools.diff.gate_diff import filter_generators",
        "except ImportError:",
        "    sys.path.insert(0, os.getcwd())",
        "    from src.bazel.tools.diff.gate_diff import filter_generators",
        "",
        'gens = filter_generators(changed_files, run_all=(mode == "all"))',
        'print(" ".join(gens))',
        "PYEOF",
        ")",
        "",
        "gen_pnpm() {",
        '  "$pnpm_cmd" --dir "$PWD" install --lockfile-only --ignore-scripts --silent',
        "}",
        "gen_uv() {",
        '  "$uv_cmd" pip compile --quiet pyproject.toml --python-version 3.13 --universal --no-strip-extras --generate-hashes -o requirements_lock.txt',
        '  "$uv_cmd" pip compile --quiet --group dev --python-version 3.13 --universal --no-strip-extras --generate-hashes -o requirements_dev_lock.txt',
        "}",
        "gen_team_records() {",
        '  "$uv_cmd" run --quiet --only-group dev python3 src/bazel/checks/records/check_team_records.py --write-workload-images src/infra/images/workload-images.json',
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
        "      *) set -- ;;",
        "    esac",
        '    BUILD_WORKSPACE_DIRECTORY="$PWD" "${runfiles%%/bazel-out/*}/$bin" "$@" &',
        '    pids="$pids $!"',
        "  done",
        '  for pid in $pids; do wait "$pid" || failed=1; done',
        '  return "$failed"',
        "}",
        "",
        'bazel_targets=""',
        "for gen in $active_generators; do",
        '  case "$gen" in',
        "    pnpm) spawn pnpm gen_pnpm ;;",
        "    uv) spawn uv gen_uv ;;",
        "  esac",
        "done",
        "wait_jobs",
        "for gen in $active_generators; do",
        '  case "$gen" in',
        "    team_records) spawn team_records gen_team_records ;;",
        "    rulesync) spawn rulesync gen_rulesync ;;",
        '    gazelle) bazel_targets="$bazel_targets //:gazelle" ;;',
        '    artwork) bazel_targets="$bazel_targets //src/infra/docs/artwork:logo //src/infra/docs/artwork:overview" ;;',
        "  esac",
        "done",
        'if [ -n "$bazel_targets" ]; then',
        "  spawn bazel gen_bazel $bazel_targets",
        "fi",
        "wait_jobs",
        "",
        "python3 src/bazel/rules/lint_aspect/build_headers.py",
        "",
    ]
    return lines

def _render_format_drift_check():
    return [
        'if [ "$check_mode" -eq 1 ]; then',
        "  post_diff=$(git diff 2>/dev/null || true)",
        "  post_status=$(git status --porcelain 2>/dev/null || true)",
        '  if [ "$post_diff" != "$pre_diff" ] || [ "$post_status" != "$pre_status" ]; then',
        '    echo "ERROR: Drift detected! Files require formatting or generated artifacts are out of date:" >&2',
        "    git --no-pager diff >&2 || true",
        '    if [ -z "$pre_status" ]; then',
        "      git checkout -- . 2>/dev/null || true",
        "      git clean -fd 2>/dev/null || true",
        "    fi",
        "    exit 1",
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

def _render_pre_command(cmd):
    if "//..." in cmd:
        return [
            'if [ "$mode" = "all" ]; then',
            "  " + cmd,
            'elif [ "$mode" = "path" ]; then',
            '  clean_target="${target%/}"',
            "  " + cmd.replace("//...", "//${clean_target#//}/..."),
            'elif [ "$mode" = "affected" ]; then',
            "  if [ ${#unique_pkgs[@]} -gt 0 ]; then",
            "    " + cmd.replace("//...", '"${unique_pkgs[@]}"'),
            "  fi",
            "fi",
            "",
        ]
    return [
        'if [ "$mode" = "all" ]; then',
        "  " + cmd,
        'elif [ "$mode" = "path" ]; then',
        '  clean_target="${target%/}"',
        '  clean_pkg="//${clean_target#//}"',
        '  if [[ "' + cmd + '" == *"$clean_pkg"* ]]; then',
        "    " + cmd,
        "  fi",
        'elif [ "$mode" = "affected" ]; then',
        "  run_pre_cmd=0",
        '  for pkg in "${unique_pkgs[@]:-}"; do',
        '    pkg_dir="${pkg%/...}"',
        '    if [[ "' + cmd + '" == *"$pkg_dir"* ]]; then',
        "      run_pre_cmd=1",
        "      break",
        "    fi",
        "  done",
        '  if [ "$run_pre_cmd" -eq 1 ]; then',
        "    " + cmd,
        "  fi",
        "fi",
        "",
    ]

def _render_check_preamble(label_name):
    return [
        "#!/usr/bin/env bash",
        "# Generated check runner that executes all repository static analysis gates.",
        "set -euo pipefail",
        "",
        'runfiles="$PWD"',
        'cd "${BUILD_WORKSPACE_DIRECTORY:?%s must be run with bazel run}"' % label_name,
        'export BUILD_WORKSPACE_DIRECTORY="$PWD"',
        'export REPO_CACHE_DIR="${XDG_CACHE_HOME:-$HOME/.cache}/repo"',
        'export TFLINT_PLUGIN_DIR="${TFLINT_PLUGIN_DIR:-$REPO_CACHE_DIR/tflint/plugins}"',
        'export TF_PLUGIN_CACHE_DIR="${TF_PLUGIN_CACHE_DIR:-$REPO_CACHE_DIR/opentofu/plugins}"',
        'export RUMDL_CACHE_DIR="${RUMDL_CACHE_DIR:-$REPO_CACHE_DIR/rumdl}"',
        'export PYTHONPYCACHEPREFIX="${PYTHONPYCACHEPREFIX:-$REPO_CACHE_DIR/python}"',
        'export RUFF_CACHE_DIR="${RUFF_CACHE_DIR:-$REPO_CACHE_DIR/ruff}"',
        'export BAZEL_BINDIR="${BAZEL_BINDIR:-.}"',
    ] + _render_nested_bazel_env(label_name) + [
        "",
        'target="${1:-}"',
        'if [ "$target" = "." ] || [ "$target" = "--all" ]; then',
        '  mode="all"',
        'elif [ -n "$target" ]; then',
        '  mode="path"',
        "else",
        '  mode="affected"',
        "fi",
        "",
    ] + _CI_PUSH_TO_MAIN_LINES + [
        "unique_pkgs=()",
        'if [ "$mode" = "affected" ]; then',
        "  diff_base=$(git merge-base HEAD origin/main 2>/dev/null || git merge-base HEAD main 2>/dev/null || echo HEAD)",
        '  changed_all=$({ git diff --name-only "$diff_base" 2>/dev/null || true; git ls-files --others --exclude-standard; } | sort -u)',
        '  if [ -z "$changed_all" ]; then',
        '    echo "No changed files detected. All checks passed."',
        "    exit 0",
        "  fi",
        '  if echo "$changed_all" | grep -q -E "(^MODULE\\.bazel|^MODULE\\.bazel\\.lock|^\\.bazelrc|^\\.bazelversion|^BUILD\\.bazel$|^multitool\\.lock\\.json|^requirements_lock\\.txt|^requirements_dev_lock\\.txt|^src/bazel/profiles/profiles\\.bazelrc)"; then',
        '    mode="all"',
        "  else",
        "    affected_pkgs=()",
        "    while IFS= read -r f; do",
        '      [ -z "$f" ] && continue',
        '      dir=$(dirname "$f")',
        '      while [ "$dir" != "." ] && [ "$dir" != "/" ] && [ -n "$dir" ]; do',
        '        if [ -f "$dir/BUILD.bazel" ] || [ -f "$dir/BUILD" ]; then',
        '          affected_pkgs+=("//$dir/...")',
        "          break",
        "        fi",
        '        dir=$(dirname "$dir")',
        "      done",
        '    done <<< "$changed_all"',
        "    if [ ${#affected_pkgs[@]} -gt 0 ]; then",
        '      unique_pkgs=($(printf "%s\\n" "${affected_pkgs[@]}" | sort -u))',
        "    fi",
        "  fi",
        "fi",
        "",
    ]

def _render_gate_runner_python(gates):
    return [
        'export CHECK_MODE="$mode"',
        'export CHANGED_ALL="${changed_all:-}"',
        'export TARGET="${target:-}"',
        'export RUNNER_BIN_DIR="$bin_dir"',
        "python3 - << 'PYEOF'",
        "import concurrent.futures, os, shlex, subprocess, sys, time",
        "from pathlib import Path",
        "",
        'max_jobs = int(os.environ.get("MAX_JOBS", "4"))',
        "mode = os.environ.get('CHECK_MODE', 'affected')",
        "raw_changed = os.environ.get('CHANGED_ALL', '')",
        "target = os.environ.get('TARGET', '')",
        "changed_files = [line.strip() for line in raw_changed.splitlines() if line.strip()]",
        "gates = " + json.encode(gates),
        "",
        "try:",
        "    from src.bazel.tools.diff.gate_diff import filter_gates",
        "except ImportError:",
        "    sys.path.insert(0, os.getcwd())",
        "    from src.bazel.tools.diff.gate_diff import filter_gates",
        "",
        "gate_map = {g['label']: g for g in gates}",
        "if mode == 'all':",
        "    selected_gates = gates",
        "elif mode == 'path':",
        "    clean_target = target.strip('/').removeprefix('//')",
        "    selected_gates = [g for g in gates if any(f.startswith(clean_target) for f in changed_files) or clean_target in g['label']]",
        "    if not selected_gates:",
        "        selected_gates = gates",
        "else:",
        "    active_labels = set(filter_gates(list(gate_map.keys()), changed_files, repo_root=Path.cwd()))",
        "    selected_gates = [g for g in gates if g['label'] in active_labels]",
        "",
        "if not selected_gates:",
        "    print('No static checks triggered for changed files. All checks passed.')",
        "    sys.exit(0)",
        "",
        "subprocess.run(",
        "    ['bazel', '--output_user_root=' + os.environ['BAZEL_OUTPUT_ROOT'], 'build',",
        "     *shlex.split(os.environ.get('BAZEL_CONFIG_FLAGS', '')),",
        "     '--remote_download_outputs=toplevel', '--ui_event_filters=-info,-stdout', '--noshow_progress',",
        "     '--', *[g['runner']['label'] for g in selected_gates]],",
        "    check=True,",
        ")",
        "",
        "gate_env = {k: v for k, v in os.environ.items() if k not in ('RUNFILES_DIR', 'RUNFILES_MANIFEST_FILE', 'PYTHON_RUNFILES', 'JAVA_RUNFILES')}",
        "",
        "def run_gate(gate):",
        "    t0 = time.perf_counter()",
        "    runner = os.path.join(os.environ['RUNNER_BIN_DIR'], gate['runner']['path'])",
        "    proc = subprocess.run([runner], env=gate_env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)",
        "    return gate['label'], proc.returncode, proc.stdout, time.perf_counter() - t0",
        "",
        "failed = False",
        "with concurrent.futures.ThreadPoolExecutor(max_workers=max_jobs) as pool:",
        "    futures = [pool.submit(run_gate, g) for g in selected_gates]",
        "    for fut in concurrent.futures.as_completed(futures):",
        "        label, code, output, dt = fut.result()",
        "        if code == 0:",
        "            print(f'[PASS] {label} ({dt:.2f}s)')",
        "        else:",
        "            print(f'[FAIL] {label} ({dt:.2f}s)\\n{output}', file=sys.stderr)",
        "            failed = True",
        "",
        "if failed:",
        "    sys.exit(1)",
        "PYEOF",
        "",
    ]

def _check_all_impl(ctx):
    launcher = ctx.actions.declare_file(ctx.label.name + ".sh")
    lines = _render_check_preamble(ctx.label.name) + _render_runner_bin_dir(launcher)
    for cmd in ctx.attr.pre_commands:
        lines.extend(_render_pre_command(cmd))

    gates = [
        {"label": gate, "runner": _runner_target(ctx, runner)}
        for gate, runner in zip(ctx.attr.gates, ctx.attr.runners)
    ]
    lines.extend(_render_gate_runner_python(gates))

    for cmd in ctx.attr.post_commands:
        lines.append(cmd)
        lines.append("")

    ctx.actions.write(output = launcher, content = "\n".join(lines) + "\n", is_executable = True)
    return [DefaultInfo(executable = launcher)]

_check_all = rule(
    implementation = _check_all_impl,
    doc = "Runs declared static analysis checks and gates.",
    attrs = {
        "gates": attr.string_list(doc = "Gate labels, as gate_diff knows them"),
        "post_commands": attr.string_list(doc = "Commands to run after gates"),
        "pre_commands": attr.string_list(doc = "Commands to run before gates, e.g. bazel build"),
        "runners": attr.string_list(doc = "Same-package runner target name per gate"),
    },
    executable = True,
)

def check_all(name, checks = [], tools = {}, pre_commands = [], post_commands = [], **kwargs):
    """Declare the repository check runner.

    Args:
        name: target name; run it with `bazel run //:<name>`.
        checks: list of check target labels or (target label, tool label)
            tuples; the tool is passed to the check as its first argument.
        tools: dict from check label to the tool labels the check calls from PATH.
        pre_commands: commands to execute before checks.
        post_commands: commands to execute after checks.
        **kwargs: forwarded to the target.
    """
    gates = []
    runners = []
    for check in checks:
        gate, argument = (check, None) if type(check) == "string" else check
        runner = _runner_name(name, gate)
        _tool_runner(
            name = runner,
            binary = gate,
            argument = argument,
            tools = tools.get(gate, []),
            tags = ["manual"],
        )
        gates.append(gate)
        runners.append(runner)

    _check_all(
        name = name,
        gates = gates,
        runners = runners,
        pre_commands = pre_commands,
        post_commands = post_commands,
        **kwargs
    )

# Cross-record policy. Unlike the lint aspects, this is a test rather than a
# build action: the question is about a set of records considered together, not
# about one target's sources, and conftest --combine is what makes that
# expressible. It runs under `bazel test //...` like everything else.
