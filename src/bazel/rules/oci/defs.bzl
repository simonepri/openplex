"""Define rules and macros for compiling, tagging, pushing, and deploying multi-architecture OCI images."""

load("@rules_gitops//gitops:defs.bzl", "k8s_deploy")
load("@rules_gitops//kubectl:defs.bzl", kubectl_resolved_toolchain = "resolved_toolchain")
load("@rules_multirun//:defs.bzl", "command")
load("@rules_oci//oci:defs.bzl", "oci_image", "oci_image_index", "oci_load", "oci_push")
load("@rules_pkg//pkg:providers.bzl", "PackageFilesInfo")
load("@rules_pkg//pkg:tar.bzl", "pkg_tar")
load("@rules_python//python:defs.bzl", "PyInfo", "py_test")

def _crane_tool_impl(ctx):
    crane = ctx.toolchains["@rules_oci//oci:crane_toolchain_type"].crane_info.binary
    return DefaultInfo(
        files = depset([crane]),
        runfiles = ctx.runfiles(files = [crane]),
    )

_crane_tool = rule(
    implementation = _crane_tool_impl,
    toolchains = ["@rules_oci//oci:crane_toolchain_type"],
)

_DEFAULT_BASE_USERS = {
    "node_base": "65532",
    "ray_base": "1000",
    "ray_torch_base": "1000",
    "workspace_dev_base": "1000:1000",
}

# Docker tests reach the daemon socket and a loopback registry, so they run
# outside the sandbox; their results stay cacheable because the pulled image
# layouts are declared inputs.
_DOCKER_TEST_TAGS = [
    "no-remote-exec",
    "no-sandbox",
    "requires-docker",
    "requires-network",
]

_DOCKER_CLIENT_ENV = [
    "DOCKER_CERT_PATH",
    "DOCKER_CONFIG",
    "DOCKER_CONTEXT",
    "DOCKER_HOST",
    "DOCKER_TLS_VERIFY",
    "HOME",
]

def _python_sources_impl(ctx):
    sources = {}
    for src in ctx.attr.binary[PyInfo].transitive_sources.to_list():
        if src.short_path.startswith("src/"):
            sources["opt/" + src.short_path[4:]] = src
    return [
        PackageFilesInfo(attributes = {}, dest_src_map = sources),
        DefaultInfo(files = depset(sources.values())),
    ]

_python_sources = rule(
    implementation = _python_sources_impl,
    attrs = {"binary": attr.label(mandatory = True, providers = [PyInfo])},
)

def _setup_package_layers(name, binary, files, srcs, tars, package_dir, image_kwargs):
    all_tars = list(tars)
    if files:
        files_layer_name = name + "_files_layer" if (binary or srcs) else name + "_layer"
        layer_files = [files] if type(files) == "string" else files
        pkg_tar(
            name = files_layer_name,
            srcs = layer_files,
        )
        all_tars = [":" + files_layer_name] + all_tars
    if binary or srcs:
        layer_name = name + "_layer"
        layer_srcs = [binary] if binary else srcs
        dest_dir = package_dir
        pkg = native.package_name()
        if pkg.startswith("src/"):
            pkg = pkg[4:]
        if not dest_dir:
            dest_dir = "/opt/" + pkg
        if binary:
            _python_sources(name = name + "_python_sources", binary = binary)
            layer_srcs = [":" + name + "_python_sources"]
            dest_dir = "/"
        pkg_tar(
            name = layer_name,
            srcs = layer_srcs,
            mode = "0555",
            package_dir = dest_dir,
        )
        all_tars = [":" + layer_name] + all_tars
        if binary:
            if "entrypoint" not in image_kwargs:
                bin_name = binary.split(":")[-1]
                module_name = pkg.replace("/", ".") + "." + bin_name
                image_kwargs["entrypoint"] = [
                    "python3",
                    "-m",
                    module_name,
                ]
            image_kwargs.setdefault("workdir", "/opt")
    return all_tars

def _resolve_base_images(base):
    if base in ["ray_torch_base", "ray_ml_base"]:
        return (
            "//src/bazel/rules/oci:ray_torch_base_linux_amd64",
            "//src/bazel/rules/oci:ray_torch_base_linux_arm64_v8",
        )
    elif base.startswith("//") or base.startswith(":"):
        return (base + "_linux_amd64", base + "_linux_arm64_v8")
    else:
        return ("@{}_linux_amd64".format(base), "@{}_linux_arm64_v8".format(base))

def _declare_import_acceptance(name, binary, staging_tag):
    module = native.package_name().removeprefix("src/").replace("/", ".") + "." + binary.split(":")[-1]
    py_test(
        name = name + "_import_acceptance",
        timeout = "long",
        srcs = ["//src/bazel/rules/oci:runtime_import_acceptance_test.py"],
        args = [
            "$(rootpath //src/bazel/rules/oci:application_import_acceptance.py)",
            module,
            "$(rootpath :" + name + "_host)",
            staging_tag,
        ] + select({
            "@bazel_tools//src/conditions:darwin_arm64": ["linux/arm64"],
            "@bazel_tools//src/conditions:linux_aarch64": ["linux/arm64"],
            "//conditions:default": ["linux/amd64"],
        }),
        data = [
            "//src/bazel/rules/oci:application_import_acceptance.py",
            ":" + name + "_host",
        ],
        env_inherit = _DOCKER_CLIENT_ENV,
        main = "//src/bazel/rules/oci:runtime_import_acceptance_test.py",
        tags = _DOCKER_TEST_TAGS,
    )

def oci_multiarch_image(
        name,
        base,
        staging_tag = None,
        binary = None,
        files = None,
        srcs = None,
        tars = [],
        arch_tars = {},
        package_dir = None,
        push = True,
        **kwargs):
    """Declare the OCI images for one workload.

    Args:
      name: prefix for every generated target; `name` itself aliases the
        native-architecture image.
      base: repository prefix of the digest-pinned base image pulls
        (`@<base>_linux_amd64` and `@<base>_linux_arm64_v8`).
      staging_tag: docker tag written by the `<name>_load` staging target.
        The `<name>_push` target has no fixed remote tag; its caller supplies
        the shared stream tag with `--tag` when it writes a registry.
      binary: Python target whose first-party source closure is packaged under /opt.
      files: pkg_files target or list of targets to package automatically as an image layer.
      srcs: source files to package automatically as an image layer.
      tars: layer tarballs appended onto the base.
      arch_tars: additional architecture-specific layers keyed by `amd64` and
        `arm64`.
      package_dir: container destination for srcs (defaults to /opt/<package>);
        binary sources always retain their module paths under /opt.
      push: declare the `<name>_push` target. Local-only images disable it.
      **kwargs: forwarded to both oci_image rules (entrypoint, env, user, ...).
    """
    if not staging_tag:
        pkg_name = native.package_name().split("/")[-1].replace("_", "-")
        tag_suffix = "bazel-load" if name == "image" else "{}-bazel-load".format(name.replace("_", "-"))
        staging_tag = "{}:{}".format(pkg_name, tag_suffix)

    image_kwargs = dict(kwargs)
    if base in _DEFAULT_BASE_USERS:
        image_kwargs.setdefault("user", _DEFAULT_BASE_USERS[base])

    all_tars = _setup_package_layers(name, binary, files, srcs, tars, package_dir, image_kwargs)
    if base in ["ray_base", "ray_torch_base", "ray_ml_base"]:
        all_tars = ["//src/bazel/rules/oci:ray_otel_layer"] + all_tars
        image_kwargs.setdefault("env", {})["PYTHONPATH"] = "/opt/python:/opt"
    base_amd64, base_arm64 = _resolve_base_images(base)

    oci_image(
        name = name + "_linux_amd64",
        base = base_amd64,
        tars = all_tars + arch_tars.get("amd64", []),
        **image_kwargs
    )
    oci_image(
        name = name + "_linux_arm64",
        base = base_arm64,
        tars = all_tars + arch_tars.get("arm64", []),
        **image_kwargs
    )
    oci_image_index(
        name = name,
        images = [
            ":" + name + "_linux_amd64",
            ":" + name + "_linux_arm64",
        ],
    )
    native.alias(
        name = name + "_multiarch",
        actual = ":" + name,
    )
    native.alias(
        name = name + "_host",
        actual = select({
            "@bazel_tools//src/conditions:darwin_arm64": ":" + name + "_linux_arm64",
            "@bazel_tools//src/conditions:linux_aarch64": ":" + name + "_linux_arm64",
            "//conditions:default": ":" + name + "_linux_amd64",
        }),
    )
    oci_load(
        name = name + "_load",
        image = ":" + name + "_host",
        repo_tags = [staging_tag],
        tags = ["manual"],
    )
    if binary and base in ["ray_base", "ray_torch_base", "ray_ml_base"]:
        _declare_import_acceptance(name, binary, staging_tag)
    if push:
        oci_push(
            name = name + "_push",
            image = ":" + name,
            tags = ["manual"],
        )

def oci_workload_publish(
        name,
        image = None,
        images = None,
        publication_mode = "stream",
        repository_path = None,
        workload_name = None,
        visibility = ["//visibility:public"]):
    """Declare guarded publication for one or more OCI images.

    Args:
        name: target name.
        image: single image target to publish.
        images: dictionary mapping role names to image targets.
        publication_mode: publication mode, digest or stream.
        repository_path: repository path override.
        workload_name: publication identifier override for long package names.
        visibility: visibility of the generated binary target.
    """
    if not image and not images:
        fail("Either image or images must be specified for %s" % name)
    if image and images:
        fail("Only one of image or images may be specified for %s" % name)
    if publication_mode not in ["digest", "stream"]:
        fail("publication_mode must be digest or stream")

    repo_target = repository_path or native.package_name()
    if not repo_target.startswith("src/"):
        fail("Publish repository target must start with 'src/': %s" % repo_target)

    if image:
        resolved_images = {"": image}
    else:
        if type(images) != "dict" or not images:
            fail("images must be a non-empty dictionary mapping role names to image targets")
        resolved_images = images

    image_specs = []
    data = []

    for role, img_target in sorted(resolved_images.items()):
        suffix = ("." + role) if role else ""
        pusher = name + ".image_push" + suffix
        oci_push(
            name = pusher,
            image = img_target,
            tags = ["manual"],
        )
        data.append(img_target)
        data.append(":" + pusher)
        image_specs.append("%s=$(rootpath :%s)=$(rootpath %s)/index.json" % (role, pusher, img_target))

    crane = name + ".crane"
    data.append(":" + crane)

    _crane_tool(name = crane)

    if len(resolved_images) == 1 and "" in resolved_images:
        pusher_arg = "$(rootpath :%s)" % (name + ".image_push")
        index_arg = "$(rootpath %s)/index.json" % resolved_images[""]
    else:
        pusher_arg = ",".join(image_specs)
        index_arg = "-"

    command(
        name = name,
        command = "//src/bazel/rules/oci:workload_cli",
        arguments = [
            "publish",
            workload_name or native.package_name().split("/")[-1].replace("_", "-"),
            repository_path or native.package_name(),
            pusher_arg,
            index_arg,
            "$(rlocationpath :%s)" % crane,
            publication_mode,
        ],
        data = data,
        tags = ["manual"],
        visibility = visibility,
    )

def oci_workload_run(
        name,
        image = None,
        images = None,
        manifest = None,
        namespace = None,
        publisher = None,
        repository_path = None,
        visibility = None):
    """Declare guarded publication and on-demand execution for workload image(s).

    Args:
        name: target name.
        image: single image target to publish and run.
        images: dictionary mapping role names to image targets.
        manifest: Kubernetes manifest template path.
        namespace: target namespace override.
        publisher: existing publisher target override.
        repository_path: repository path override.
        visibility: target visibility.
    """
    if not manifest:
        fail("manifest is required for %s" % name)
    if not publisher and not image and not images:
        fail("Either publisher, image, or images must be specified for %s" % name)

    publisher_target = publisher
    if not publisher_target:
        publisher_target = ":" + name + ".publisher"
        oci_workload_publish(
            name = name + ".publisher",
            image = image,
            images = images,
            publication_mode = "digest",
            repository_path = repository_path,
            visibility = ["//visibility:private"],
        )

    rendered = name + ".manifest"
    k8s_deploy(
        name = rendered,
        cluster = "",
        gitops = False,
        manifests = [manifest],
        namespace = namespace,
        tags = ["manual"],
        user = "",
        verify_images = False,
        visibility = ["//visibility:private"],
    )

    kubectl = name + ".kubectl"
    kubectl_resolved_toolchain(name = kubectl)

    data = [
        ":" + rendered,
        publisher_target,
        ":" + kubectl,
    ]

    command(
        name = name,
        command = "//src/bazel/rules/oci:workload_cli",
        arguments = [
            "run",
            native.package_name().split("/")[-1].replace("_", "-"),
            repository_path or native.package_name(),
            "$(rootpath %s)" % publisher_target,
            "$(rootpath :%s)" % rendered,
            namespace,
            "$(rlocationpath :%s)" % kubectl,
        ],
        data = data,
        tags = ["manual"],
        visibility = visibility,
    )

def _normalize_bases(base, kwargs):
    """Normalize base images into (is_multiarch, {arch: base_label})."""
    arch = kwargs.pop("architecture", None)
    if type(base) == "dict":
        return True, {a.split("/")[-1]: b for a, b in base.items()}
    if type(base) in ("list", "tuple"):
        archs = kwargs.pop("architectures", ["amd64", "arm64"])
        result = {}
        for i in range(len(base)):
            arch_key = archs[i] if i < len(archs) else str(i)
            result[arch_key] = base[i]
        return True, result
    if arch:
        return False, {arch: base}
    if base == None:
        archs = kwargs.pop("architectures", ["amd64", "arm64"])
        return True, {a: None for a in archs}
    for suffix, a in [
        ("_linux_amd64", "amd64"),
        ("_linux_arm64_v8", "arm64"),
        ("_linux_arm64", "arm64"),
        ("_amd64", "amd64"),
        ("_arm64", "arm64"),
    ]:
        if base.endswith(suffix):
            return False, {a: base}
    amd64_base, arm64_base = _resolve_base_images(base)
    return True, {"amd64": amd64_base, "arm64": arm64_base}

def _resolve_created_label(name, created):
    if not created:
        return None
    if created == True:
        created = "1970-01-01T00:00:00Z"
    if type(created) == "string" and not (created.startswith(":") or created.startswith("//") or created.startswith("@")):
        created_target = name + "_created"
        native.genrule(
            name = created_target,
            outs = [name + "_created.txt"],
            cmd = "printf %s " + repr(created) + " > $@",
            visibility = ["//visibility:private"],
        )
        return ":" + created_target
    return created

def _wire_third_party_publish_targets(
        name,
        image_target,
        repository,
        repo_tags,
        publication_mode,
        visibility,
        push = True,
        publish = True,
        standard_aliases = False):
    load_name = "load" if name == "image" else name + "_load"
    push_name = "push" if name == "image" else name + "_push"
    publish_name = "publish" if name == "image" else name + "_publish"

    oci_load(
        name = load_name,
        image = image_target,
        repo_tags = repo_tags,
        tags = ["manual"],
        visibility = visibility,
    )
    if push:
        oci_push(
            name = push_name,
            image = ":" + name,
            tags = ["manual"],
            visibility = visibility,
        )
    if publish:
        oci_workload_publish(
            name = publish_name,
            image = ":" + name,
            repository_path = repository,
            publication_mode = publication_mode,
            visibility = visibility,
        )
    if name == "image":
        native.alias(name = "image_load", actual = ":load", visibility = visibility)
        if push:
            native.alias(name = "image_push", actual = ":push", visibility = visibility)
        if publish:
            native.alias(name = "image_publish", actual = ":publish", visibility = visibility)
    elif standard_aliases:
        native.alias(name = "image", actual = ":" + name, visibility = visibility)
        native.alias(name = "load", actual = ":" + load_name, visibility = visibility)
        if push:
            native.alias(name = "push", actual = ":" + push_name, visibility = visibility)
        if publish:
            native.alias(name = "publish", actual = ":" + publish_name, visibility = visibility)

def _declare_multiarch_images(name, bases, common_kwargs, all_layers, arch_tars, visibility):
    arch_images = []
    for arch, arch_base in sorted(bases.items()):
        arch_img_name = "{}_{}".format(name, arch) if arch.startswith("linux_") else "{}_linux_{}".format(name, arch)
        arch_kwargs = dict(common_kwargs)
        arch_kwargs["tars"] = all_layers + arch_tars.get(arch, [])
        if arch_base:
            arch_kwargs["base"] = arch_base
        else:
            arch_kwargs.setdefault("os", "linux")
            arch_kwargs["architecture"] = arch
        oci_image(
            name = arch_img_name,
            visibility = ["//visibility:private"],
            **arch_kwargs
        )
        arch_images.append(":" + arch_img_name)

    oci_image_index(
        name = name,
        images = arch_images,
        visibility = visibility,
    )
    native.alias(
        name = name + "_multiarch",
        actual = ":" + name,
        visibility = visibility,
    )
    native.alias(
        name = name + "_host",
        actual = select({
            "@bazel_tools//src/conditions:darwin_arm64": ":" + name + "_linux_arm64",
            "@bazel_tools//src/conditions:linux_aarch64": ":" + name + "_linux_arm64",
            "//conditions:default": ":" + name + "_linux_amd64",
        }),
        visibility = visibility,
    )
    return ":" + name + "_host"

def _declare_singlearch_image(name, bases, common_kwargs, all_layers, arch_tars, visibility):
    arch, arch_base = list(bases.items())[0]
    single_kwargs = dict(common_kwargs)
    single_kwargs["tars"] = all_layers + arch_tars.get(arch, [])
    if arch_base:
        single_kwargs["base"] = arch_base
    else:
        single_kwargs.setdefault("os", "linux")
        single_kwargs["architecture"] = arch
    oci_image(
        name = name,
        visibility = visibility,
        **single_kwargs
    )
    return ":" + name

def third_party_image(
        name,
        repository,
        base = None,
        layers = [],
        entrypoint = None,
        cmd = None,
        env = {},
        user = None,
        workdir = None,
        visibility = None,
        **kwargs):
    """Declare a standardized third-party OCI image with load and publish targets.

    Args:
        name: Name of the primary image target (or :image).
        repository: Repository identifier or path used for tagging and publishing.
        base: Base image reference (dict for multi-arch, string, list, or None for scratch).
        layers: List of tar archives to append as container layers.
        entrypoint: Container entrypoint executable or list.
        cmd: Default command arguments for container execution.
        env: Dictionary of container environment variables.
        user: User identity to execute the container payload as.
        workdir: Working directory within the container.
        visibility: Target visibility list.
        **kwargs: Additional arguments forwarded to oci_image or publish helpers.
    """
    if not repository.startswith("src/"):
        fail("Publish repository target must start with 'src/': %s" % repository)

    image_env = dict(env or {})
    if "source_date_epoch" in kwargs:
        sde = kwargs.pop("source_date_epoch")
        if sde != None and sde != False:
            image_env["SOURCE_DATE_EPOCH"] = str(sde)
    elif env or base == None:
        image_env.setdefault("SOURCE_DATE_EPOCH", "0")

    tag = kwargs.pop("tag", "latest")
    repo_tags = kwargs.pop("repo_tags", [repository + ":" + tag])
    publication_mode = kwargs.pop("publication_mode", "digest")
    push = kwargs.pop("push", True)
    publish = kwargs.pop("publish", True)
    standard_aliases = kwargs.pop("standard_aliases", False)
    arch_tars = kwargs.pop("arch_tars", {})
    all_layers = list(layers) + kwargs.pop("tars", [])

    created = _resolve_created_label(name, kwargs.pop("created", None))
    is_multiarch, bases = _normalize_bases(base, kwargs)

    common_kwargs = dict(
        entrypoint = entrypoint,
        cmd = cmd,
        user = user,
        workdir = workdir,
        **kwargs
    )
    if image_env:
        common_kwargs["env"] = image_env
    if created:
        common_kwargs["created"] = created

    if is_multiarch:
        load_image = _declare_multiarch_images(name, bases, common_kwargs, all_layers, arch_tars, visibility)
    else:
        load_image = _declare_singlearch_image(name, bases, common_kwargs, all_layers, arch_tars, visibility)

    _wire_third_party_publish_targets(
        name = name,
        image_target = load_image,
        repository = repository,
        repo_tags = repo_tags,
        publication_mode = publication_mode,
        visibility = visibility,
        push = push,
        publish = publish,
        standard_aliases = standard_aliases,
    )

_SITE_PACKAGES = "/site-packages/"

def _python_site_packages_impl(ctx):
    dest_src_map = {}
    for target in ctx.attr.srcs:
        target_files = depset(
            transitive = [
                target[DefaultInfo].files,
                target[DefaultInfo].default_runfiles.files,
            ],
        )
        for src in target_files.to_list():
            marker_index = src.short_path.find(_SITE_PACKAGES)
            if marker_index < 0:
                continue
            relative_path = src.short_path[marker_index + len(_SITE_PACKAGES):]
            destination = "usr/local/lib/python{}/site-packages/{}".format(
                ctx.attr.python_version,
                relative_path,
            )
            if destination in dest_src_map and dest_src_map[destination] != src:
                fail("multiple wheels own {}".format(destination))
            dest_src_map[destination] = src

    files = depset(dest_src_map.values())
    return [
        PackageFilesInfo(attributes = {}, dest_src_map = dest_src_map),
        DefaultInfo(files = files),
    ]

python_site_packages = rule(
    implementation = _python_site_packages_impl,
    attrs = {
        "python_version": attr.string(mandatory = True),
        "srcs": attr.label_list(allow_files = True, mandatory = True),
    },
)

def _python_wheel_site_packages_impl(ctx):
    destination_prefix = "home/ray/anaconda3/lib/python3.10/site-packages/"
    dest_src_map = {}
    for wheel in ctx.attr.wheels:
        workspace_prefix = "../{}/".format(wheel.label.workspace_name)
        for src in wheel[DefaultInfo].files.to_list():
            if not src.short_path.startswith(workspace_prefix):
                fail("wheel file {} does not belong to {}".format(src.short_path, wheel.label))
            relative_path = src.short_path[len(workspace_prefix):]
            if relative_path in ["BUILD.bazel", "REPO.bazel", "WORKSPACE"]:
                continue
            destination = destination_prefix + relative_path
            if destination in dest_src_map and dest_src_map[destination] != src:
                fail("multiple runtime wheels own {}".format(destination))
            dest_src_map[destination] = src
    return [
        PackageFilesInfo(attributes = {}, dest_src_map = dest_src_map),
        DefaultInfo(files = depset(dest_src_map.values())),
    ]

python_wheel_site_packages = rule(
    implementation = _python_wheel_site_packages_impl,
    attrs = {"wheels": attr.label_list(mandatory = True)},
)
