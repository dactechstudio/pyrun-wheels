# OpenCV Python Android wheel

This recipe builds the upstream OpenCV-Python 4.11.0.86 source for CPython 3.13
and Android API 25. It pins the upstream wrapper commit and OpenCV submodule
commit in recipe.json; scripts/prepare_opencv_source.py verifies both before
applying the Android Python-binding adaptations. Release build run
39987784445 produced wheels for arm64_v8a and x86_64.

The intended distribution is opencv-python-headless; Python code imports it as
cv2. The headless build omits desktop GUI integrations and optional external
codecs such as FFmpeg, while retaining built-in image codecs. The package uses
the app's pinned NumPy 1.26.2 ABI.

CI verified both wheel tags, native ELF architecture, 16 KB load alignment,
package RECORD hashes, and the expected cv2 module and cascade data. The wheel
includes OpenCV's LICENSE.txt and LICENSE-3RD-PARTY.txt; the CycloneDX SBOM
records the third-party components identified in the native build.

No emulator or device import smoke test was run. Keep OpenCV opt-in in the app
until runtime validation is performed on supported Android ABIs. The recipe is
approved for publication to the signed wheel index, not enabled by default in
the app or approved for runtime installation through Google Play.
