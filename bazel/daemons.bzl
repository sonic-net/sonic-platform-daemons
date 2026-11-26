"""Build each daemon's setuptools wheel and install it through the shared rule."""

load("@sonic_build_infra//python:wheel_layer.bzl", "wheel_layer")

DAEMONS = [
    "sonic-bmcctld",
    "sonic-chassisd",
    "sonic-ledd",
    "sonic-pcied",
    "sonic-psud",
    "sonic-sensormond",
    "sonic-stormond",
    "sonic-syseepromd",
    "sonic-thermalctld",
    "sonic-xcvrd",
    "sonic-ycabled",
]

def daemon_distribution(name):
    """Expose a pure wheel and its installed image payload from the owner's setup.py."""
    version = "1.1" if name == "sonic-ledd" else "1.0"
    stem = name.replace("-", "_") + "-" + version
    setup = name + "/setup.py"
    generated = [":ycabled_proto"] if name == "sonic-ycabled" else []
    native.filegroup(
        name = name + ".sources",
        srcs = native.glob([name + "/**/*.py", name + "/setup.cfg"]) +
               native.glob([name + "/scripts/*"], allow_empty = True),
    )
    native.genrule(
        name = name + ".wheel",
        srcs = [setup, ":" + name + ".sources"] + generated,
        outs = [stem + "-py3-none-any.whl"],
        tools = [":wheel_builder"],
        cmd = "PYTHONHASHSEED=0 $(execpath :wheel_builder) --output $@ --setup $(location " + setup +
              ") --sources $(locations :" + name + ".sources)" +
              (" --generated $(locations :ycabled_proto)" if generated else ""),
        visibility = ["//visibility:public"],
    )
    wheel_layer(
        name = name + ".runtime",
        wheels = [":" + name + ".wheel"],
        site_packages = "/usr/local/lib/python3.13/dist-packages",
        headers = "/usr/local/include/python3.13",
        visibility = ["//visibility:public"],
    )
    native.filegroup(
        name = name + ".runtime_metadata",
        srcs = [":" + name + ".runtime"],
        output_group = "metadata",
        visibility = ["//visibility:public"],
    )
