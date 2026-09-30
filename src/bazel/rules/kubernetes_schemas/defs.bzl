"""Fetch pinned Kubernetes OpenAPI JSON schemas via blobless git clones for offline manifest validation."""

_BUILD = """\
filegroup(
    name = "schemas",
    srcs = glob(["**/*.json"]),
    visibility = ["//visibility:public"],
)
"""

def _kubernetes_schemas_impl(ctx):
    directory = "v{}-standalone-strict".format(ctx.attr.kubernetes_version)

    def run(*arguments):
        result = ctx.execute(
            ["git"] + list(arguments),
            environment = {
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_DIR": ".git",
                "GIT_INDEX_FILE": ".git/index",
                "GIT_OBJECT_DIRECTORY": ".git/objects",
                "GIT_WORK_TREE": ".",
            },
            timeout = 1200,
        )
        if result.return_code != 0:
            fail("git {}: {}".format(" ".join(arguments), result.stderr))

    # --filter=blob:none keeps the checkout to the one directory below; a full
    # clone of every published version is 1.4 GB.
    run("init", "--quiet")
    run("remote", "add", "origin", ctx.attr.remote)
    run("config", "core.sparseCheckout", "true")
    run("sparse-checkout", "init", "--cone")
    run("sparse-checkout", "set", directory)
    run("fetch", "--quiet", "--depth=1", "--filter=blob:none", "origin", ctx.attr.commit)
    run("checkout", "--quiet", "FETCH_HEAD")

    # The history is the bulk of the clone and nothing reads it after checkout.
    ctx.delete(".git")

    if not ctx.path(directory).exists:
        fail("{} holds no {}; is the Kubernetes version published upstream?".format(
            ctx.attr.remote,
            directory,
        ))
    ctx.file("BUILD.bazel", _BUILD)

kubernetes_schemas = repository_rule(
    implementation = _kubernetes_schemas_impl,
    doc = "Checks out one version directory of yannh/kubernetes-json-schema.",
    attrs = {
        "commit": attr.string(mandatory = True, doc = "Exact upstream commit to pin."),
        "kubernetes_version": attr.string(mandatory = True, doc = "e.g. 1.36.0."),
        "remote": attr.string(default = "https://github.com/yannh/kubernetes-json-schema"),
    },
)
