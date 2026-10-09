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
