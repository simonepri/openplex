"""Define the bool_flag rule behind the gpu_runners flag."""

# NOTE(simonepri): this mirrors bazel_skylib's bool_flag; switch to it once
# the root module declares bazel_skylib as a direct dependency.

BoolFlagInfo = provider(
    doc = "Carry the value of a bool_flag.",
    fields = {"value": "the flag value"},
)

def _bool_flag_impl(ctx):
    return [BoolFlagInfo(value = ctx.build_setting_value)]

bool_flag = rule(
    implementation = _bool_flag_impl,
    build_setting = config.bool(flag = True),
    doc = "A boolean flag set with --//pkg:name or --no//pkg:name.",
)
