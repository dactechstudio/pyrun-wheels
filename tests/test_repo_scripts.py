from __future__ import annotations

import hashlib
import base64
import csv
import io
import json
import os
import stat
import sys
import tempfile
import tomllib
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from jsonschema import validate
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from packaging.requirements import Requirement
import yaml

from scripts.sign_manifest import main as sign_manifest_main, validate_manifest
from scripts.generate_signing_key import generate_keypair
from scripts.promote_snapshot import promote
from scripts.prepare_pages_site import prepare_pages_site
from scripts.prepare_pygame_source import add_android_license_notices, add_native_license_notices
from scripts.configure_pygame_android import configure as configure_pygame_android
from scripts.yank_version import update_policy
from scripts.compare_wheels import compare
from scripts.stage_simple_index import stage
from scripts.verify_android_wheels import safe_members, verify_wheel
from scripts.verify_pygame_wheel import extension_modules


def make_wheel(
    path: Path,
    abi: str,
    *,
    distribution: str = "pyrun-native-probe",
    version: str = "0.1.0",
    module_path: str = "pyrun_native_probe/_native.so",
) -> None:
    wheel_name = distribution.replace("-", "_")
    dist_info = f"{wheel_name}-{version}.dist-info"
    filename = f"{wheel_name}-{version}-cp313-cp313-android_25_{abi}.whl"
    path = path / filename
    contents = {
        f"{dist_info}/METADATA": (
            f"Metadata-Version: 2.4\nName: {distribution}\nVersion: {version}\n"
            "Requires-Python: >=3.13,<3.14\n\n"
        ).encode(),
        f"{dist_info}/WHEEL": (
            f"Wheel-Version: 1.0\nRoot-Is-Purelib: false\nTag: cp313-cp313-android_25_{abi}\n"
        ).encode(),
        module_path: b"ELF fixture bytes",
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
    def test_development_requirements_match_toolchain_lock(self) -> None:
        root = Path(__file__).resolve().parents[1]
        lock = tomllib.loads((root / "toolchain.lock").read_text(encoding="utf-8"))
        requirements = [
            Requirement(line.strip())
            for line in (root / "requirements-dev.txt").read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        locked = {requirement.name.lower().replace("-", "_"): str(next(iter(requirement.specifier)).version) for requirement in requirements}
        for package, version in locked.items():
            self.assertEqual(str(lock[package]), version, f"{package} is out of sync with toolchain.lock")

    def test_workflow_is_read_only_pinned_and_covers_both_abis(self) -> None:
        workflow_path = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "build-android.yml"
        workflow = yaml.load(workflow_path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
        self.assertEqual(workflow["permissions"]["contents"], "read")
        self.assertEqual(
            {item["abi"] for item in workflow["jobs"]["build"]["strategy"]["matrix"]["include"]},
            {"arm64_v8a", "x86_64"},
        )
        toolchain = tomllib.loads((Path(__file__).resolve().parents[1] / "toolchain.lock").read_text(encoding="utf-8"))
        setup_python_versions = [
            step["with"]["python-version"]
            for job in workflow["jobs"].values()
            for step in job.get("steps", [])
            if step.get("uses", "").startswith("actions/setup-python@")
        ]
        self.assertTrue(setup_python_versions)
        self.assertTrue(all(version == toolchain["python"] for version in setup_python_versions))
        self.assertNotIn("publish", workflow["jobs"])
        for job in workflow["jobs"].values():
            for step in job.get("steps", []):
                action = step.get("uses")
                if action:
                    self.assertRegex(action, r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+@[0-9a-f]{40}(?:\s+# .+)?$")
        self.assertEqual(workflow["jobs"]["build"]["strategy"]["matrix"]["include"][0]["runner"], "ubuntu-24.04")
        self.assertEqual(workflow["jobs"]["build"]["strategy"]["matrix"]["include"][1]["runner"], "macos-15")
        self.assertEqual(workflow["jobs"]["build"]["strategy"]["matrix"]["include"][1]["abi"], "arm64_v8a")
        matrix = workflow["jobs"]["build"]["strategy"]["matrix"]["include"]
        self.assertEqual({item["emulator_test"] for item in matrix}, {"false"})
        self.assertEqual(
            workflow["on"]["workflow_dispatch"]["inputs"]["run_emulator_tests"]["default"],
            "false",
        )
        self.assertEqual(
            workflow["on"]["workflow_dispatch"]["inputs"]["build_opencv"]["default"],
            "true",
        )
        self.assertEqual(
            workflow["on"]["workflow_dispatch"]["inputs"]["build_pygame"]["default"],
            "true",
        )
        self.assertEqual(
            workflow["on"]["workflow_dispatch"]["inputs"]["restage_from_build_run_id"]["default"],
            "",
        )
        self.assertEqual(
            workflow["jobs"]["build"]["if"],
            "${{ github.event_name != 'workflow_dispatch' || inputs.restage_from_build_run_id == '' }}",
        )
        restage = workflow["jobs"]["restage-index"]
        self.assertEqual(
            restage["if"],
            "${{ github.event_name == 'workflow_dispatch' && inputs.restage_from_build_run_id != '' }}",
        )
        self.assertEqual(restage["needs"], "test")
        self.assertEqual(restage["permissions"], {"actions": "read", "contents": "read"})
        restage_steps = {step["name"]: step for step in restage["steps"]}
        self.assertIn("gh run download", restage_steps["Download exact wheel artifacts from the source build"]["run"])
        self.assertIn("verify_android_wheels.py", restage_steps["Re-verify wheel artifacts and stage reviewed snapshot"]["run"])
        self.assertIn('for wheel in wheelhouse/*android_25_"$abi".whl', restage_steps["Re-verify wheel artifacts and stage reviewed snapshot"]["run"])
        self.assertIn("stage_simple_index.py", restage_steps["Re-verify wheel artifacts and stage reviewed snapshot"]["run"])
        self.assertEqual(
            restage_steps["Upload unsigned snapshot for signing"]["with"]["name"],
            "simple-repository-snapshot",
        )
        emulator_setup = [
            step for step in workflow["jobs"]["build"]["steps"]
            if step["name"] == "Install Android emulator on supported Linux CI runner"
        ]
        self.assertEqual(len(emulator_setup), 1)
        self.assertEqual(emulator_setup[0]["run"].strip(), '"${ANDROID_HOME}/cmdline-tools/latest/bin/sdkmanager" --install "emulator"')
        preserved_tools = [
            step for step in workflow["jobs"]["build"]["steps"]
            if step["name"] == "Preserve NDK ELF tools before cibuildwheel cleanup"
        ]
        self.assertEqual(len(preserved_tools), 1)
        self.assertIn('cp -R "$prebuilt/." "$RUNNER_TEMP/pyrun-llvm-prebuilt/"', preserved_tools[0]["run"])
        recipe_path = (
            Path(__file__).resolve().parents[1]
            / "recipes"
            / "native_probe"
            / "pyproject.toml"
        )
        recipe = tomllib.loads(recipe_path.read_text(encoding="utf-8"))
        self.assertEqual(recipe["tool"]["cibuildwheel"]["android"]["test-runtime"]["args"], ["--managed", "maxVersion"])
        build_verify = next(
            step for step in workflow["jobs"]["build"]["steps"]
            if step["name"] == "Verify wheel tags, ABI, hashes, and 16 KB ELF alignment"
        )
        self.assertIn("$PYRUN_NDK_LLVM_READELF", build_verify["run"])
        stage_verify = next(
            step for step in workflow["jobs"]["stage-index"]["steps"]
            if step["name"] == "Re-verify both ABI artifacts and stage snapshot"
        )
        self.assertIn(
            'find "$ANDROID_NDK_HOME/toolchains/llvm/prebuilt" -name llvm-readelf',
            stage_verify["run"],
        )
        opencv_steps = [
            step for step in workflow["jobs"]["build"]["steps"]
            if step["name"] in {
                "Prepare pinned upstream OpenCV-Python source",
                "Build optional OpenCV-Python Android wheel",
                "Verify OpenCV wheel tags, ABI, hashes, and 16 KB ELF alignment",
            }
        ]
        self.assertEqual(len(opencv_steps), 3)
        self.assertTrue(all("inputs.build_opencv == true" in step["if"] for step in opencv_steps))
        self.assertIn("CIBW_TEST_SKIP='*'", opencv_steps[1]["run"])

        pygame_steps = [
            step for step in workflow["jobs"]["build"]["steps"]
            if step["name"] in {
                "Prepare pinned Pygame and SDL sources",
                "Build the pinned SDL2 runtime for this ABI",
                "Generate Pygame's Android Setup from upstream SDL configuration",
                "Build Pygame wheel without device/emulator execution",
                "Verify Pygame wheel contents, Android tags, ABI, and 16 KB ELF alignment",
            }
        ]
        self.assertEqual(len(pygame_steps), 5)
        self.assertTrue(all("inputs.build_pygame == true" in step["if"] for step in pygame_steps))
        self.assertEqual(pygame_steps[3]["env"]["CIBW_TEST_SKIP"], "*")
        self.assertEqual(
            pygame_steps[3]["env"]["CIBW_ENVIRONMENT_ANDROID"],
            "${{ matrix.abi == 'x86_64' && 'PYGAME_ANDROID=1 PYGAME_DETECT_AVX2=1' || 'PYGAME_ANDROID=1' }}",
        )

    def test_pygame_recipe_pins_android_source_stack_and_build_inputs(self) -> None:
        root = Path(__file__).resolve().parents[1]
        recipe = json.loads((root / "recipes" / "pygame" / "recipe.json").read_text(encoding="utf-8"))
        schema = json.loads((root / "schemas" / "build-recipe.schema.json").read_text(encoding="utf-8"))
        validate(instance=recipe, schema=schema)
        self.assertEqual(recipe["name"], "pygame")
        self.assertEqual(recipe["version"], "2.6.1")
        self.assertEqual(recipe["release_status"], "experimental")
        self.assertEqual(set(recipe["abis"]), {"arm64_v8a", "x86_64"})
        self.assertEqual(recipe["build"]["cython"], "3.1.3")
        self.assertEqual(recipe["build"]["ninja"], "1.13.0")
        for patch in recipe["patches"]:
            self.assertEqual(
                hashlib.sha256((root / patch["path"]).read_bytes()).hexdigest(),
                patch["sha256"],
            )
        sbom = json.loads((root / recipe["sbom"]).read_text(encoding="utf-8"))
        components = {component["name"] for component in sbom["components"]}
        self.assertTrue({"SDL2", "SDL2_image", "SDL2_mixer", "SDL2_ttf", "FreeType"} <= components)
        self.assertTrue({"libpng", "libjpeg-turbo", "SDL_gfxPrimitives", "stb_image", "stb_vorbis", "minimp3"} <= components)
        self.assertEqual(
            extension_modules([
                "pygame/base.cpython-313-aarch64-linux-android.so",
                "pygame/_sdl2/video.cpython-313-aarch64-linux-android.so",
                "pygame/display.py",
            ]),
            {
                "pygame/base": "pygame/base.cpython-313-aarch64-linux-android.so",
                "pygame/_sdl2/video": "pygame/_sdl2/video.cpython-313-aarch64-linux-android.so",
            },
        )

    def test_pygame_android_source_preserves_runtime_license_notices(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary)
            (source / "docs" / "licenses").mkdir(parents=True)
            (source / "src_py").mkdir()
            (source / "docs" / "LGPL.txt").write_text("pygame LGPL notice", encoding="utf-8")
            (source / "docs" / "licenses" / "LICENSE.sdl_gfx.txt").write_text(
                "SDL_gfx zlib notice", encoding="utf-8"
            )
            setup = source / "setup.py"
            setup.write_text(
                'data_files = [(\'pygame\', ["src_py/pygame_icon_mac.bmp"])]\n'
                "        distutils.ccompiler.spawn(cmd, dry_run=self.dry_run, **kwargs)\n"
                "for e in extensions:\n"
                "    # Only define the ARM_NEON defines if they have been enabled at build time.\n",
                encoding="utf-8",
            )

            add_android_license_notices(source)

            self.assertEqual(
                (source / "src_py" / "LICENSE.txt").read_text(encoding="utf-8"),
                "pygame LGPL notice",
            )
            self.assertEqual(
                (source / "src_py" / "LICENSE.sdl_gfx.txt").read_text(encoding="utf-8"),
                "SDL_gfx zlib notice",
            )
            self.assertIn('"src_py/LICENSE.txt"', setup.read_text(encoding="utf-8"))
            self.assertIn('"src_py/LICENSE.sdl_gfx.txt"', setup.read_text(encoding="utf-8"))
            self.assertIn('"src_py/LICENSE.libpng.txt"', setup.read_text(encoding="utf-8"))
            self.assertIn('"src_py/LICENSE.libjpeg-turbo.txt"', setup.read_text(encoding="utf-8"))
            self.assertIn("self.__spawn(cmd, **kwargs)", setup.read_text(encoding="utf-8"))
            self.assertIn("extension.extra_link_args.append('-Wl,-z,max-page-size=16384')", setup.read_text(encoding="utf-8"))

    def test_pygame_android_preserves_statically_linked_image_library_notices(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pygame = root / "pygame"
            libpng = root / "libpng"
            libjpeg = root / "libjpeg"
            (pygame / "src_py").mkdir(parents=True)
            libpng.mkdir()
            libjpeg.mkdir()
            (libpng / "LICENSE").write_text("libpng license", encoding="utf-8")
            (libjpeg / "LICENSE.md").write_text("libjpeg-turbo license", encoding="utf-8")

            add_native_license_notices(pygame, libpng, libjpeg)

            self.assertEqual(
                (pygame / "src_py" / "LICENSE.libpng.txt").read_text(encoding="utf-8"),
                "libpng license",
            )
            self.assertEqual(
                (pygame / "src_py" / "LICENSE.libjpeg-turbo.txt").read_text(encoding="utf-8"),
                "libjpeg-turbo license",
            )

    def test_pygame_android_setup_links_pinned_image_dependencies(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pygame = root / "pygame" / "pygame-2.6.1"
            template = pygame / "buildconfig" / "Setup.Android.SDL2.in"
            template.parent.mkdir(parents=True)
            template.write_text(
                "SDL = {sdl_includes} -lSDL2\n"
                "JPEG = {jpeg_includes} -ljpeg\n"
                "PNG = {png_includes} -lpng16\n"
                "DEBUG =\n"
                "imageext src_c/imageext.c $(SDL) $(IMAGE) $(PNG) $(JPEG) $(DEBUG)\n"
                "surface src_c/surface.c src_c/alphablit.c src_c/surface_fill.c $(SDL) $(DEBUG)\n"
                "base src_c/base.c $(SDL) $(DEBUG)\n"
                "display src_c/display.c $(SDL) $(DEBUG)\n"
                "event src_c/event.c $(SDL) $(DEBUG)\n"
                "font src_c/font.c $(SDL) $(DEBUG)\n"
                "mixer src_c/mixer.c $(SDL) $(DEBUG)\n"
                "mixer_music src_c/music.c $(SDL) $(DEBUG)\n"
                "_sdl2.video src_c/_sdl2/video.c $(SDL) $(DEBUG)\n",
                encoding="utf-8",
            )
            roots = {
                "SDL-release-2.32.10/SDL-release-2.32.10": "sdl",
                "SDL_image-release-2.8.12/SDL_image-release-2.8.12": "image",
                "SDL_mixer-release-2.8.2/SDL_mixer-release-2.8.2": "mixer",
                "SDL_ttf-release-2.24.0/SDL_ttf-release-2.24.0": "ttf",
                "libpng-1.6.59/libpng-1.6.59": "png",
                "libjpeg-turbo-3.2.0/libjpeg-turbo-3.2.0": "jpeg",
            }
            for relative, _name in roots.items():
                source = root / relative
                source.mkdir(parents=True)
                (source / "CMakeLists.txt").write_text("", encoding="utf-8")
            native = root / "native"
            for library in (
                "libSDL2.so", "libSDL2_image.so", "libSDL2_mixer.so", "libSDL2_ttf.so",
                "libpng16.a", "libjpeg.a",
            ):
                target = native / library
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"")

            result = configure_pygame_android(pygame, root, native, "x86_64").read_text(encoding="utf-8")

            self.assertIn("-lpng16 -lz -lm", result)
            self.assertIn("-ljpeg -lm", result)
            self.assertIn("src_c/simd_blitters_sse2.c", result)
            self.assertIn("src_c/simd_blitters_avx2.c", result)
            self.assertNotIn("-Wl,-z,max-page-size=16384", result)
            self.assertFalse((pygame / "src_c" / "pyrun_avx2_fallbacks.c").exists())

    def test_pygame_android_arm64_setup_uses_neon_compatible_simd_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pygame = root / "pygame" / "pygame-2.6.1"
            template = pygame / "buildconfig" / "Setup.Android.SDL2.in"
            template.parent.mkdir(parents=True)
            template.write_text(
                "SDL = {sdl_includes} -lSDL2\n"
                "JPEG = {jpeg_includes} -ljpeg\n"
                "PNG = {png_includes} -lpng16\n"
                "DEBUG =\n"
                "surface src_c/surface.c src_c/alphablit.c src_c/surface_fill.c $(SDL) $(DEBUG)\n"
                "base src_c/base.c $(SDL) $(DEBUG)\n"
                "display src_c/display.c $(SDL) $(DEBUG)\n"
                "event src_c/event.c $(SDL) $(DEBUG)\n"
                "font src_c/font.c $(SDL) $(DEBUG)\n"
                "imageext src_c/imageext.c $(SDL) $(IMAGE) $(PNG) $(JPEG) $(DEBUG)\n"
                "mixer src_c/mixer.c $(SDL) $(DEBUG)\n"
                "mixer_music src_c/music.c $(SDL) $(DEBUG)\n"
                "_sdl2.video src_c/_sdl2/video.c $(SDL) $(DEBUG)\n",
                encoding="utf-8",
            )
            for relative in (
                "SDL-release-2.32.10/SDL-release-2.32.10",
                "SDL_image-release-2.8.12/SDL_image-release-2.8.12",
                "SDL_mixer-release-2.8.2/SDL_mixer-release-2.8.2",
                "SDL_ttf-release-2.24.0/SDL_ttf-release-2.24.0",
                "libpng-1.6.59/libpng-1.6.59",
                "libjpeg-turbo-3.2.0/libjpeg-turbo-3.2.0",
            ):
                source = root / relative
                source.mkdir(parents=True)
                (source / "CMakeLists.txt").write_text("", encoding="utf-8")
            native = root / "native"
            for library in ("libSDL2.so", "libSDL2_image.so", "libSDL2_mixer.so", "libSDL2_ttf.so", "libpng16.a", "libjpeg.a"):
                target = native / library
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"")

            result = configure_pygame_android(pygame, root, native, "arm64_v8a").read_text(encoding="utf-8")

            self.assertIn("src_c/simd_blitters_sse2.c", result)
            self.assertIn("src_c/pyrun_avx2_fallbacks.c", result)
            self.assertNotIn("src_c/simd_blitters_avx2.c", result)
            fallback = (pygame / "src_c" / "pyrun_avx2_fallbacks.c").read_text(encoding="utf-8")
            self.assertIn("int pg_has_avx2(void) { return 0; }", fallback)

    def test_opencv_recipe_pins_upstream_inputs_and_patch_digest(self) -> None:
        root = Path(__file__).resolve().parents[1]
        recipe = json.loads(
            (root / "recipes" / "opencv_python" / "recipe.json").read_text(encoding="utf-8")
        )
        schema = json.loads(
            (root / "schemas" / "build-recipe.schema.json").read_text(encoding="utf-8")
        )
        validate(instance=recipe, schema=schema)
        self.assertEqual(recipe["source_revision"], "255564a37c4a23275485e827abbe19031347d458")
        self.assertEqual(recipe["source_submodules"]["opencv"], "31b0eeea0b44b370fd0712312df4214d4ae1b158")
        self.assertEqual(set(recipe["abis"]), {"arm64_v8a", "x86_64"})
        self.assertEqual(recipe["build"]["numpy"], "1.26.2")
        self.assertEqual(recipe["release_status"], "approved")
        sbom = json.loads(
            (root / recipe["sbom"]).read_text(encoding="utf-8")
        )
        sbom_components = {component["name"] for component in sbom["components"]}
        self.assertIn("OpenJPEG", sbom_components)
        self.assertIn("Android libc++ shared runtime", sbom_components)
        self.assertTrue(
            any(prop["name"] == "pyrun:source-build-run-id" and prop["value"] == "39987784445"
                for prop in sbom["metadata"]["properties"])
        )
        prepare_source = (root / "scripts" / "prepare_opencv_source.py").read_text(encoding="utf-8")
        self.assertIn('"sdk/etc" if "CIBUILDWHEEL_BUILD_IDENTIFIER" in os.environ', prepare_source)
        self.assertIn('r"/haarcascades/.*\\\\.xml"', prepare_source)
        patch = recipe["patches"][0]
        self.assertEqual(
            hashlib.sha256((root / patch["path"]).read_bytes()).hexdigest(),
            patch["sha256"],
        )

    def test_snapshot_stages_probe_and_opencv_as_two_signed_index_packages(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            wheel_dir = root / "wheels"
            wheel_dir.mkdir()
            for abi in ("x86_64", "arm64_v8a"):
                make_wheel(wheel_dir, abi)
                make_wheel(
                    wheel_dir,
                    abi,
                    distribution="opencv-python-headless",
                    version="4.11.0.86",
                    module_path="cv2/cv2.abi3.so",
                )

            output = root / "snapshot"
            manifest = stage(wheel_dir, output, "b" * 40)
            opencv_artifacts = [
                item for item in manifest["artifacts"]
                if item["name"] == "opencv-python-headless"
            ]
            self.assertEqual({item["abi"] for item in opencv_artifacts}, {"arm64_v8a", "x86_64"})
            self.assertEqual(
                {item["upstreamRevision"] for item in opencv_artifacts},
                {"255564a37c4a23275485e827abbe19031347d458"},
            )
            index = output / "simple" / "opencv-python-headless" / "index.html"
            self.assertTrue(index.is_file())
            self.assertEqual(index.read_text(encoding="utf-8").count("<a href="), 2)
            self.assertTrue((output / "scripts" / "prepare_opencv_source.py").is_file())
            validate_manifest(output / "manifest.json")

    def test_signing_workflow_is_separate_manual_protected_and_read_only(self) -> None:
        workflow_path = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "sign-snapshot.yml"
        workflow = yaml.load(workflow_path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
        self.assertIn("workflow_dispatch", workflow["on"])
        self.assertEqual(workflow["permissions"], {"actions": "read", "contents": "read"})
        job = workflow["jobs"]["sign-and-promote"]
        self.assertEqual(job["if"], "github.ref == 'refs/heads/main'")
        self.assertEqual(job["environment"]["name"], "wheel-release")
        self.assertEqual(job["permissions"], {"actions": "read", "contents": "read"})
        self.assertEqual(job["env"]["PYRUN_ED25519_PRIVATE_KEY"], "${{ secrets.PYRUN_ED25519_PRIVATE_KEY }}")
        step_names = {step["name"] for step in job["steps"]}
        self.assertIn("Validate source build run and release identifier", step_names)
        self.assertIn("Sign and verify the exact manifest", step_names)
        self.assertIn("Promote into a fresh release directory", step_names)
        self.assertIn("Upload signed snapshot for reviewed publication", step_names)
        for step in job["steps"]:
            if step.get("uses"):
                self.assertRegex(step["uses"], r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+@[0-9a-f]{40}(?:\s+# .+)?$")

    def test_key_generation_keeps_private_key_outside_repo_and_refuses_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = root / "repo"
            key_directory = repository / "keys"
            key_directory.mkdir(parents=True)
            private_key = root / "protected" / "pyrun-ed25519-private.pem"
            public_key = key_directory / "pyrun-ed25519-public.pem"

            created_private, created_public = generate_keypair(private_key, public_key, repository)
            self.assertEqual(created_private, private_key.resolve())
            self.assertEqual(created_public, public_key.resolve())
            private = serialization.load_pem_private_key(private_key.read_bytes(), password=None)
            public = serialization.load_pem_public_key(public_key.read_bytes())
            self.assertIsInstance(private, Ed25519PrivateKey)
            self.assertEqual(
                private.public_key().public_bytes(
                    serialization.Encoding.PEM,
                    serialization.PublicFormat.SubjectPublicKeyInfo,
                ),
                public.public_bytes(
                    serialization.Encoding.PEM,
                    serialization.PublicFormat.SubjectPublicKeyInfo,
                ),
            )
            with self.assertRaisesRegex(ValueError, "outside the repository"):
                generate_keypair(repository / "private.pem", key_directory / "another-public.pem", repository)
            with self.assertRaisesRegex(FileExistsError, "refusing to replace"):
                generate_keypair(private_key, public_key, repository)

    def test_signer_reads_private_key_from_secret_environment_variable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            wheels = root / "wheels"
            wheels.mkdir()
            make_wheel(wheels, "x86_64")
            make_wheel(wheels, "arm64_v8a")
            snapshot = root / "snapshot"
            stage(wheels, snapshot, "e" * 40)
            key = Ed25519PrivateKey.generate()
            private_bytes = key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
            public_key = root / "public.pem"
            public_key.write_bytes(
                key.public_key().public_bytes(
                    serialization.Encoding.PEM,
                    serialization.PublicFormat.SubjectPublicKeyInfo,
                )
            )
            with (
                patch.dict(os.environ, {"PYRUN_TEST_SIGNING_KEY": private_bytes.decode("utf-8")}),
                patch.object(
                    sys,
                    "argv",
                    [
                        "sign_manifest.py",
                        "sign",
                        str(snapshot / "manifest.json"),
                        "--private-key-env",
                        "PYRUN_TEST_SIGNING_KEY",
                    ],
                ),
            ):
                sign_manifest_main()
            signature = snapshot / "manifest.json.sig"
            self.assertTrue(signature.is_file())
            with patch.object(
                sys,
                "argv",
                [
                    "sign_manifest.py", "verify", str(snapshot / "manifest.json"),
                    "--public-key", str(public_key), "--signature", str(signature),
                ],
            ):
                sign_manifest_main()
            release = root / "promoted"
            promote(snapshot, release, public_key)
            self.assertEqual((release / "manifest.json.sig").read_bytes(), signature.read_bytes())

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
            self.assertEqual({item["sourceUrl"] for item in manifest["artifacts"]}, {
                "https://github.com/dactechstudio/pyrun-wheels/tree/" + "a" * 40 + "/recipes/native_probe"
            })
            self.assertEqual(manifest["simpleIndexes"][0]["name"], "root")
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
            self.assertEqual(
                hashlib.sha256((output / manifest["simpleIndexes"][0]["path"]).read_bytes()).hexdigest(),
                manifest["simpleIndexes"][0]["sha256"],
            )
            self.assertTrue(all(artifact["playRuntimeInstallAllowed"] is False for artifact in manifest["artifacts"]))

            first_artifact = output / manifest["artifacts"][0]["url"]
            first_artifact.write_bytes(first_artifact.read_bytes() + b"tampered")
            with self.assertRaisesRegex(ValueError, "hash mismatch"):
                validate_manifest(output / "manifest.json")

    def test_yank_policy_updates_atomically_and_marks_pep592_index(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            policy_path = root / "yanks.json"
            policy_path.write_text('{"schemaVersion":1,"yanked":[]}\n', encoding="utf-8")
            policy = update_policy(policy_path, "yank", "PyRun_Native.Probe", "0.1.0", "security issue")
            self.assertEqual(policy["yanked"][0]["name"], "pyrun-native-probe")
            self.assertEqual(policy["yanked"][0]["version"], "0.1.0")
            wheel_dir = root / "wheels"
            wheel_dir.mkdir()
            make_wheel(wheel_dir, "x86_64")
            make_wheel(wheel_dir, "arm64_v8a")
            output = root / "snapshot"
            manifest = stage(wheel_dir, output, "b" * 40, policy_path)
            index = (output / "simple" / "pyrun-native-probe" / "index.html").read_text(encoding="utf-8")
            self.assertEqual({artifact["yanked"] for artifact in manifest["artifacts"]}, {True})
            self.assertEqual({artifact["yankReason"] for artifact in manifest["artifacts"]}, {"security issue"})
            self.assertEqual(index.count('data-yanked="security issue"'), 2)
            self.assertEqual(validate_manifest(output / "manifest.json"), manifest)
            update_policy(policy_path, "unyank", "pyrun-native-probe", "0.1.0")
            self.assertEqual(json.loads(policy_path.read_text(encoding="utf-8"))["yanked"], [])

    def test_snapshot_stage_refuses_to_replace_existing_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            wheel_dir = root / "wheels"
            wheel_dir.mkdir()
            make_wheel(wheel_dir, "x86_64")
            make_wheel(wheel_dir, "arm64_v8a")
            output = root / "snapshot"
            output.mkdir()
            marker = output / "keep.txt"
            marker.write_text("keep", encoding="utf-8")
            with self.assertRaisesRegex(FileExistsError, "refusing to replace"):
                stage(wheel_dir, output, "c" * 40)
            self.assertEqual(marker.read_text(encoding="utf-8"), "keep")

    def test_signed_snapshot_promotes_to_new_directory_for_release_or_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            wheel_dir = root / "wheels"
            wheel_dir.mkdir()
            make_wheel(wheel_dir, "x86_64")
            make_wheel(wheel_dir, "arm64_v8a")
            snapshot = root / "snapshot"
            stage(wheel_dir, snapshot, "d" * 40)
            private_key = root / "private.pem"
            public_key = root / "public.pem"
            signature = snapshot / "manifest.json.sig"
            key = Ed25519PrivateKey.generate()
            private_key.write_bytes(
                key.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption(),
                )
            )
            public_key.write_bytes(
                key.public_key().public_bytes(
                    serialization.Encoding.PEM,
                    serialization.PublicFormat.SubjectPublicKeyInfo,
                )
            )
            with patch.object(
                sys,
                "argv",
                ["sign_manifest.py", "sign", str(snapshot / "manifest.json"), "--private-key", str(private_key)],
            ):
                sign_manifest_main()
            with patch.object(
                sys,
                "argv",
                [
                    "sign_manifest.py", "verify", str(snapshot / "manifest.json"),
                    "--public-key", str(public_key), "--signature", str(signature),
                ],
            ):
                sign_manifest_main()

            release = root / "release-1"
            promote(snapshot, release, public_key)
            self.assertEqual(validate_manifest(release / "manifest.json"), validate_manifest(snapshot / "manifest.json"))
            self.assertEqual((release / "manifest.json.sig").read_bytes(), signature.read_bytes())
            with self.assertRaisesRegex(FileExistsError, "refusing to overwrite"):
                promote(snapshot, release, public_key)

            rollback = root / "rollback-release"
            promote(release, rollback, public_key)
            self.assertEqual(validate_manifest(rollback / "manifest.json"), validate_manifest(snapshot / "manifest.json"))
            snapshot_index = snapshot / "simple" / "pyrun-native-probe" / "index.html"
            snapshot_index.write_text(snapshot_index.read_text(encoding="utf-8") + "<!-- changed -->\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "hash mismatch"):
                promote(snapshot, root / "tampered-release", public_key)

    def test_pages_publisher_preserves_signed_releases_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            wheel_dir = root / "wheels"
            wheel_dir.mkdir()
            make_wheel(wheel_dir, "x86_64")
            make_wheel(wheel_dir, "arm64_v8a")
            private_key = root / "private.pem"
            public_key = root / "public.pem"
            key = Ed25519PrivateKey.generate()
            private_key.write_bytes(
                key.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption(),
                )
            )
            public_key.write_bytes(
                key.public_key().public_bytes(
                    serialization.Encoding.PEM,
                    serialization.PublicFormat.SubjectPublicKeyInfo,
                )
            )

            def signed_snapshot(name: str, revision: str) -> Path:
                snapshot = root / name
                stage(wheel_dir, snapshot, revision * 40)
                with patch.object(
                    sys,
                    "argv",
                    ["sign_manifest.py", "sign", str(snapshot / "manifest.json"), "--private-key", str(private_key)],
                ):
                    sign_manifest_main()
                return snapshot

            first_snapshot = signed_snapshot("snapshot-1", "e")
            site = root / "site"
            prepare_pages_site(first_snapshot, site, "release-1", public_key)
            first_manifest = (site / "releases" / "release-1" / "manifest.json").read_bytes()
            prepare_pages_site(first_snapshot, site, "release-1", public_key)
            self.assertEqual(
                (site / "releases" / "release-1" / "manifest.json").read_bytes(),
                first_manifest,
            )

            second_snapshot = signed_snapshot("snapshot-2", "f")
            prepare_pages_site(second_snapshot, site, "release-2", public_key)
            catalog = json.loads((site / "catalog.json").read_text(encoding="utf-8"))
            self.assertEqual(catalog["latestRelease"], "release-2")
            self.assertEqual([item["id"] for item in catalog["releases"]], ["release-1", "release-2"])
            self.assertEqual(
                (site / "releases" / "release-1" / "manifest.json").read_bytes(),
                first_manifest,
            )
            self.assertIn("releases/release-2/", (site / "index.html").read_text(encoding="utf-8"))

            with self.assertRaisesRegex(ValueError, "already exists with different contents"):
                prepare_pages_site(second_snapshot, site, "release-1", public_key)

    def test_pages_publisher_rejects_unsafe_release_ids_and_tampered_snapshots(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            wheel_dir = root / "wheels"
            wheel_dir.mkdir()
            make_wheel(wheel_dir, "x86_64")
            make_wheel(wheel_dir, "arm64_v8a")
            snapshot = root / "snapshot"
            stage(wheel_dir, snapshot, "a" * 40)
            private_key = root / "private.pem"
            public_key = root / "public.pem"
            key = Ed25519PrivateKey.generate()
            private_key.write_bytes(
                key.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption(),
                )
            )
            public_key.write_bytes(
                key.public_key().public_bytes(
                    serialization.Encoding.PEM,
                    serialization.PublicFormat.SubjectPublicKeyInfo,
                )
            )
            with patch.object(
                sys,
                "argv",
                ["sign_manifest.py", "sign", str(snapshot / "manifest.json"), "--private-key", str(private_key)],
            ):
                sign_manifest_main()
            site = root / "site"
            with self.assertRaisesRegex(ValueError, "release ID"):
                prepare_pages_site(snapshot, site, "../escape", public_key)
            (snapshot / "packages" / next((snapshot / "packages").iterdir()).name).write_bytes(b"tampered")
            with self.assertRaisesRegex(ValueError, "hash mismatch"):
                prepare_pages_site(snapshot, site, "release-1", public_key)

    def test_publisher_workflow_is_manual_protected_and_pinned(self) -> None:
        workflow_path = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "publish-pages.yml"
        workflow = yaml.load(workflow_path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
        self.assertIn("workflow_dispatch", workflow["on"])
        self.assertEqual(workflow["permissions"], {"actions": "read", "contents": "read"})
        job = workflow["jobs"]["publish"]
        self.assertEqual(job["if"], "github.ref == 'refs/heads/main'")
        self.assertEqual(job["environment"]["name"], "github-pages")
        self.assertEqual(
            job["permissions"],
            {"actions": "read", "contents": "write", "id-token": "write", "pages": "write"},
        )
        step_names = {step["name"] for step in job["steps"]}
        self.assertIn("Configure GitHub credentials for state-branch pushes", step_names)
        self.assertIn("Validate the signed workflow run and release ID", step_names)
        self.assertIn("Download the exact signed snapshot artifact", step_names)
        self.assertIn("Verify signature, manifest, and every snapshot digest", step_names)
        self.assertIn("Add release to the immutable Pages site history", step_names)
        self.assertIn("Upload the cumulative static site", step_names)
        self.assertIn("Deploy to GitHub Pages", step_names)
        for step in job["steps"]:
            if step.get("uses"):
                self.assertRegex(step["uses"], r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+@[0-9a-f]{40}(?:\s+# .+)?$")

    def test_repeat_build_comparator_rejects_different_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "first"
            second = root / "second"
            first.mkdir()
            second.mkdir()
            (first / "probe.whl").write_bytes(b"first")
            (second / "probe.whl").write_bytes(b"second")
            with self.assertRaisesRegex(ValueError, "wheel outputs differ"):
                compare(first, second)


if __name__ == "__main__":
    unittest.main()
