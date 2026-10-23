from setuptools import Extension, setup


setup(
    packages=["pyrun_native_probe"],
    package_dir={"": "src"},
    ext_modules=[
        Extension(
            "pyrun_native_probe._native",
            sources=["src/pyrun_native_probe/native_probe.c"],
        )
    ],
)
