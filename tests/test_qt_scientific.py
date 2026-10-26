"""Build contract checks for the optional PyRun Qt scientific runtime."""

import io
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest


TOOLS = Path(__file__).resolve().parents[1] / "tools" / "phase22"
sys.path.insert(0, str(TOOLS))

from enable_qt_scientific_requirements import (  # noqa: E402
    ORIGINAL,
    P4A_COMMIT,
    SCIENTIFIC,
    patch_deployer,
)
from verify_qt_runtime_aar import _verify_scientific_bundle  # noqa: E402


class QtScientificTests(unittest.TestCase):
    def test_pinned_deployer_patch_is_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "buildozer.py"
            source.write_text(
                f'P4A_COMMIT = "{P4A_COMMIT}"\n{ORIGINAL}\n', encoding="utf-8"
            )
            patch_deployer(source)
            self.assertIn(SCIENTIFIC, source.read_text(encoding="utf-8"))
            with self.assertRaises(ValueError):
                patch_deployer(source)

    def test_scientific_bundle_needs_packages_backend_and_native_extensions(self):
        files = (
            "_python_bundle/site-packages/numpy/__init__.py",
            "_python_bundle/site-packages/matplotlib/__init__.py",
            "_python_bundle/site-packages/matplotlib/backends/backend_qtagg.py",
        )
        payload = io.BytesIO()
        with tarfile.open(fileobj=payload, mode="w:gz") as archive:
            for name in files:
                entry = tarfile.TarInfo(name)
                entry.size = 0
                archive.addfile(entry, io.BytesIO())
        members = {
            "jni/arm64-v8a/libpybundle.so": payload.getvalue(),
            "jni/arm64-v8a/numpy._core._multiarray_umath.so": b"native",
            "jni/arm64-v8a/matplotlib.ft2font.so": b"native",
        }
        _verify_scientific_bundle(members, "arm64-v8a")
        del members["jni/arm64-v8a/matplotlib.ft2font.so"]
        with self.assertRaisesRegex(ValueError, "ft2font"):
            _verify_scientific_bundle(members, "arm64-v8a")


if __name__ == "__main__":
    unittest.main()
