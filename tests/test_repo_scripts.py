from __future__ import annotations

import hashlib
import base64
import csv
import io
import json
import stat
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from jsonschema import validate
import yaml

from scripts.sign_manifest import validate_manifest
from scripts.stage_simple_index import stage
from scripts.verify_android_wheels import safe_members, verify_wheel


def make_wheel(path: Path, abi: str) -> None:
    filename = f"pyrun_native_probe-0.1.0-cp313-cp313-android_25_{abi}.whl"
    path = path / filename
    dist_info = "pyrun_native_probe-0.1.0.dist-info"
    contents = {
        f"{dist_info}/METADATA": (
            "Metadata-Version: 2.4\nName: pyrun-native-probe\nVersion: 0.1.0\n"
            "Requires-Python: >=3.13,<3.14\n\n"
        ).encode(),
        f"{dist_info}/WHEEL": (
            f"Wheel-Version: 1.0\nRoot-Is-Purelib: false\nTag: cp313-cp313-android_25_{abi}\n"
        ).encode(),
        "pyrun_native_probe/_native.so": b"ELF fixture bytes",
    }
    record = io.StringIO(newline="")
    rows = csv.writer(record, lineterminator="\n")
    for name, payload in contents.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(payload).digest()).decode().rstrip("=")
        rows.writerow((name, f"sha256={digest}", str(len(payload))))
    rows.writerow((f"{dist_info}/RECORD", "", ""))
    contents[f"{dist_info}/RECORD"] = record.getvalue().encode()
    with zipfile.ZipFile(path, "w") as wheel:
        for name, payload in contents.items():
            wheel.writestr(name, payload)


class AndroidWheelVerificationTests(unittest.TestCase):
    def test_workflow_is_read_only_pinned_and_covers_both_abis(self) -> None:
        workflow_path = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "build-android.yml"
        workflow = yaml.load(workflow_path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
        self.assertEqual(workflow["permissions"]["contents"], "read")
        self.assertEqual(
            {item["abi"] for item in workflow["jobs"]["build"]["strategy"]["matrix"]["include"]},
            {"arm64_v8a", "x86_64"},
        )
        self.assertNotIn("publish", workflow["jobs"])
        for job in workflow["jobs"].values():
            for step in job.get("steps", []):
                action = step.get("uses")
                if action:
                    self.assertRegex(action, r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+@[0-9a-f]{40}(?:\s+# .+)?$")

    def test_verifies_abi_machine_hash_and_16kb_load_alignment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            make_wheel(root, "x86_64")
            wheel = next(root.glob("*.whl"))
            outputs = [
                "  Machine:                           Advanced Micro Devices X86-64\n",
                "  LOAD 0x000000 0x000000 0x000000 0x0000 0x0000 R 0x4000\n",
            ]
            with patch("scripts.verify_android_wheels.subprocess.run", side_effect=[SimpleNamespace(stdout=value) for value in outputs]):
                digest = verify_wheel(wheel, "x86_64", 25, "llvm-readelf")
            self.assertEqual(hashlib.sha256(wheel.read_bytes()).hexdigest(), digest)

    def test_rejects_wrong_abi_before_invoking_elf_tool(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            make_wheel(root, "arm64_v8a")
            wheel = next(root.glob("*.whl"))
            with self.assertRaisesRegex(ValueError, "expected cp313"):
                verify_wheel(wheel, "x86_64", 25, "llvm-readelf")

    def test_rejects_elf_load_alignment_below_16kb(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            make_wheel(root, "x86_64")
            wheel = next(root.glob("*.whl"))
            outputs = [
                "  Machine:                           Advanced Micro Devices X86-64\n",
                "  LOAD 0x000000 0x000000 0x000000 0x0000 0x0000 R 0x1000\n",
            ]
            with patch("scripts.verify_android_wheels.subprocess.run", side_effect=[SimpleNamespace(stdout=value) for value in outputs]):
                with self.assertRaisesRegex(ValueError, "below 16 KB"):
                    verify_wheel(wheel, "x86_64", 25, "llvm-readelf")

    def test_rejects_archive_traversal_and_symlink_members(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            traversal = root / "traversal.whl"
            with zipfile.ZipFile(traversal, "w") as archive:
                archive.writestr("../escape.py", "bad")
            with zipfile.ZipFile(traversal) as archive:
                with self.assertRaisesRegex(ValueError, "unsafe wheel member"):
                    safe_members(archive)

            link = root / "link.whl"
            with zipfile.ZipFile(link, "w") as archive:
                item = zipfile.ZipInfo("package/link")
                item.external_attr = (stat.S_IFLNK | 0o777) << 16
                archive.writestr(item, "outside")
            with zipfile.ZipFile(link) as archive:
                with self.assertRaisesRegex(ValueError, "unsafe wheel member"):
                    safe_members(archive)

    def test_stages_two_abi_indexes_with_manifest_hashes_and_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            wheel_dir = root / "wheels"
            wheel_dir.mkdir()
            make_wheel(wheel_dir, "x86_64")
            make_wheel(wheel_dir, "arm64_v8a")
            output = root / "repository"
            manifest = stage(wheel_dir, output, "a" * 40)
            schema = json.loads(
                (Path(__file__).resolve().parents[1] / "schemas" / "repository-manifest.schema.json").read_text()
            )
            validate(instance=manifest, schema=schema)
            recipe_schema = json.loads(
                (Path(__file__).resolve().parents[1] / "schemas" / "build-recipe.schema.json").read_text()
            )
            recipe = json.loads(
                (Path(__file__).resolve().parents[1] / "recipes" / "native_probe" / "recipe.json").read_text()
            )
            validate(instance=recipe, schema=recipe_schema)
            self.assertEqual({item["abi"] for item in manifest["artifacts"]}, {"arm64_v8a", "x86_64"})
            for item in manifest["artifacts"]:
                self.assertEqual(
                    hashlib.sha256((output / item["url"]).read_bytes()).hexdigest(),
                    item["sha256"],
                )
            self.assertEqual(validate_manifest(output / "manifest.json"), manifest)
            self.assertTrue((output / "recipes" / "native_probe" / "recipe.json").is_file())
            self.assertTrue((output / "sbom" / "pyrun-native-probe.cdx.json").is_file())
            index = (output / "simple" / "pyrun-native-probe" / "index.html").read_text()
            self.assertIn("#sha256=", index)
            self.assertIn("android_25_arm64_v8a.whl", index)
            self.assertIn("android_25_x86_64.whl", index)

            first_artifact = output / manifest["artifacts"][0]["url"]
            first_artifact.write_bytes(first_artifact.read_bytes() + b"tampered")
            with self.assertRaisesRegex(ValueError, "hash mismatch"):
                validate_manifest(output / "manifest.json")


if __name__ == "__main__":
    unittest.main()
