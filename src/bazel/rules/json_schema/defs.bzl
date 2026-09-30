"""Define build rules validating JSON and YAML documents against schema specifications during compilation."""

def _json_schema_impl(ctx):
    report = ctx.actions.declare_file(ctx.label.name + ".schema")
    tool = ctx.executable._tool
    args = ctx.actions.args()
    args.add("--schemafile", ctx.file.schema)
    args.add_all(ctx.files.srcs)
    ctx.actions.run_shell(
        inputs = ctx.files.srcs + [ctx.file.schema],
        tools = [tool],
        outputs = [report],
        arguments = [args],
        command = '"{tool}" --check-metaschema "{schema}" >"{report}" 2>&1 && "{tool}" "$@" >>"{report}" 2>&1 || {{ cat "{report}" >&2; exit 1; }}'.format(
            schema = ctx.file.schema.path,
            tool = tool.path,
            report = report.path,
        ),
        mnemonic = "JsonSchema",
        progress_message = "Validating %{label} against its schema",
    )
    return [DefaultInfo(files = depset([report])), OutputGroupInfo(_validation = depset([report]))]

json_schema = rule(
    implementation = _json_schema_impl,
    doc = "Fails when a document does not match its JSON Schema.",
    attrs = {
        "srcs": attr.label_list(allow_files = True, mandatory = True),
        "schema": attr.label(allow_single_file = True, mandatory = True),
        "_tool": attr.label(
            default = "//src/bazel/tools:check-jsonschema",
            executable = True,
            cfg = "exec",
        ),
    },
)
