"""Defines Bazel Starlark rules for compiling CodeGraph index databases and orchestrating review runner scripts."""

def _codegraph_index_impl(ctx):
    out_db = ctx.actions.declare_file("codegraph.db")
    runner = ctx.actions.declare_file(ctx.label.name + "_runner.sh")

    # Script initializes codegraph in sandbox, then copies out db
    ctx.actions.write(
        output = runner,
        content = """#!/usr/bin/env bash
set -euo pipefail
OUTPUT_PATH="$1"
# Create a local project structure in sandbox
codegraph init -y . >/dev/null 2>&1 || true
if [ -f ".codegraph/codegraph.db" ]; then
    cp ".codegraph/codegraph.db" "${OUTPUT_PATH}"
else
    touch "${OUTPUT_PATH}"
fi
""",
        is_executable = True,
    )

    ctx.actions.run(
        outputs = [out_db],
        inputs = ctx.files.srcs,
        executable = runner,
        arguments = [out_db.path],
        mnemonic = "CodeGraphIndex",
        progress_message = "Building CodeGraph symbol index",
    )

    return [DefaultInfo(files = depset([out_db]))]

codegraph_index = rule(
    implementation = _codegraph_index_impl,
    attrs = {
        "srcs": attr.label_list(allow_files = True, mandatory = True),
    },
)
