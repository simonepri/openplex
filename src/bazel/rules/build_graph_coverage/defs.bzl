"""Define package_sources and source tracking rules ensuring full build graph coverage for static analysis."""

# Content kinds. Each names a shape a tool can consume, not a directory.
HELM_TEMPLATE = "helm-template"
CHAINSAW_SUITE = "chainsaw-suite"
CHAINSAW_FIXTURE = "chainsaw-fixture"
CHAINSAW_CONFIG = "chainsaw-config"
GITHUB_WORKFLOW = "github-workflow"
WEB_SOURCE = "web-source"
GENERATED = "generated"
DATA = "data"

def source_set(name, kind, srcs, visibility = ["//visibility:public"], **kwargs):
    """Declare one set of files as one content kind.

    Args:
        name: target name.
        kind: content kind; one of the constants in this file.
        srcs: files, usually a glob.
        visibility: defaults to public so lint targets in other packages reach it.
        **kwargs: forwarded to the filegroup.
    """
    native.filegroup(
        name = name,
        srcs = srcs,
        tags = [kind],
        visibility = visibility,
        **kwargs
    )

def package_sources(name = "sources", extra_globs = [], exclude = [], visibility = ["//visibility:public"]):
    """Claim every remaining file in this package as data.

    The catch-all that makes the coverage gate satisfiable. It globs what the
    package still holds after its typed targets, so a file added later is
    claimed automatically rather than silently escaping the graph. `glob`
    descends into subdirectories that are not themselves packages, so a
    directory with no BUILD file is covered by its nearest ancestor. Both `*`
    and `**` are needed: `**` alone misses files sitting directly in the package.

    Args:
        name: target name.
        extra_globs: additional patterns beyond `**`.
        exclude: patterns to leave to another target in this package.
        visibility: defaults to public.
    """
    native.filegroup(
        name = name,
        srcs = native.glob(
            ["*", "**"] + extra_globs,
            # Tool state directories hold downloaded providers and caches. They
            # are not sources: globbing them pulls vendored third-party docs and
            # binaries into the graph, where the lint aspects then read them.
            # The repository root .tmp is in .bazelignore; nested ones are not.
            exclude = exclude + [
                "BUILD",
                "BUILD.bazel",
                ".tmp/**",
                ".local/**",
                ".git/**",
                ".terraform/**",
                "**/.tmp/**",
                "**/.local/**",
                "**/.terraform/**",
                # Compiled bytecode is build output, not a source; globbing it
                # puts the same file in two targets.
                "__pycache__/**",
                "**/__pycache__/**",
                ".plans/**",
            ],
            allow_empty = True,
        ),
        tags = [DATA],
        visibility = visibility,
    )
