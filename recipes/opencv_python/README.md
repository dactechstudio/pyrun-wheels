# OpenCV Python Android wheel

This recipe builds the upstream OpenCV-Python 4.11.0.86 source for CPython 3.13
and Android API 25. It pins the upstream wrapper commit and OpenCV submodule
commit in `recipe.json`; `scripts/prepare_opencv_source.py` verifies both before
applying the Android Python-binding adaptations.

The intended distribution is `opencv-python-headless`; Python code imports it
as `cv2`. The headless build omits desktop GUI integrations and optional
external codecs such as FFmpeg, while retaining OpenCV's built-in JPEG/PNG
support. It is an optional package and must not be included in the app build
until both ABI wheels have been built, reviewed, signed, and published.

The package uses the app's pinned NumPy 1.26.2 ABI. This recipe is build-only:
no OpenCV wheel has been produced or import-tested yet. Emulator/device
execution is intentionally not part of this workflow unless explicitly
enabled by a maintainer. Its `release_status` is `experimental`, so the signing
tool refuses to sign snapshots containing it until a reviewed change marks the
recipe approved after ABI, contents, and license/SBOM checks.
