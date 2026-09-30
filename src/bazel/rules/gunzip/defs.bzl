"""Define gunzip decompression rule restoring execution permissions for single-binary downloads."""

def _gunzip_impl(ctx):
    out = ctx.actions.declare_file(ctx.attr.out)
    ctx.actions.run_shell(
        inputs = [ctx.file.src],
        outputs = [out],
        command = 'gzip -dc "$1" > "$2" && chmod +x "$2"',
        arguments = [ctx.file.src.path, out.path],
        mnemonic = "Gunzip",
        progress_message = "Decompressing %{label}",
    )
    return [DefaultInfo(executable = out, files = depset([out]))]

gunzip = rule(
    implementation = _gunzip_impl,
    executable = True,
    doc = "Decompress one .gz payload into an executable file.",
    attrs = {
        "src": attr.label(allow_single_file = True, mandatory = True),
        "out": attr.string(mandatory = True),
    },
)
