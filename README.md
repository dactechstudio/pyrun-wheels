# PyRun Android wheels

This is PyRun's separate source repository for reviewed native Android wheels.
It is not the Android app and is not a public PyPI index. The initial package,
`pyrun-native-probe`, is a tiny compiled extension used to exercise the complete
build, ABI, import, integrity, 16 KB ELF, index, signing, yank, and rollback
pipeline before adding larger native libraries.

## Supported proof matrix

| CPython | Android ABI | Minimum API | Wheel platform tag |
| --- | --- | ---: | --- |
| 3.13 | `arm64-v8a` | 25 | `android_25_arm64_v8a` |
| 3.13 | `x86_64` | 25 | `android_25_x86_64` |

The build pins CPython 3.13.17, NDK 30.0.16248370, cibuildwheel 4.3.1,
`build` 1.3.0, setuptools 80.9.0, wheel 0.45.1 and the repository tooling in
`toolchain.lock`, `requirements-dev.txt` and each package recipe. The CI runner
is pinned to Ubuntu 24.04 x64 for the x86_64 wheel and macOS 15 ARM64 for the
arm64-v8a wheel. A commit timestamp is passed as
`SOURCE_DATE_EPOCH`; each ABI is built twice and the output wheel bytes must
match exactly. NDK `llvm-readelf` then verifies the ELF machine and every
`PT_LOAD` segment's 16 KB alignment.

## CI behavior

Every push and pull request runs the repository tests, builds both ABI wheels,
verifies repeatability, wheel tags, hashes and ELF alignment, and stages an
unsigned Simple Repository snapshot. The x86_64 wheel also gets a native import
smoke test in cibuildwheel's managed Android emulator on the GitHub-hosted
Linux x86_64 runner using the latest managed Android version available to the
test runner. The arm64-v8a wheel is built and statically verified on a
native macOS ARM runner, but its managed emulator test is skipped: the current
hosted macOS runner could not start that emulator, and cibuildwheel documents
GitHub Actions Linux x86_64 as its supported hosted Android-emulator
environment. The workflow does not publish to GitHub Pages or release
binaries; its unsigned snapshot is an Actions artifact retained for 14 days.
Manual workflow dispatch also runs the supported emulator test by default; it
can be disabled for a build-only diagnostic run.

No physical device is required for the x86_64 smoke test. Full arm64-v8a import
smoke coverage needs a runner that can provide a working Android ARM64
emulator. See [`PHASE16.md`](PHASE16.md) for status, signing, yank, and rollback
procedures.

## Local build

Use Linux x86_64 or macOS ARM64/x86_64 with Android command-line tools, Java 17,
and the pinned NDK. Windows is not a supported cibuildwheel Android host.
GitHub Actions is the canonical two-ABI build and emulator-test environment.

```sh
python3.13 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-dev.txt
yes | "$ANDROID_HOME/cmdline-tools/latest/bin/sdkmanager" --licenses
"$ANDROID_HOME/cmdline-tools/latest/bin/sdkmanager" --install "ndk;30.0.16248370"
export ANDROID_NDK_HOME="$ANDROID_HOME/ndk/30.0.16248370"
export ANDROID_API_LEVEL=25
export SOURCE_DATE_EPOCH="$(git show -s --format=%ct HEAD)"
python -m cibuildwheel --platform android --archs x86_64 \
  --output-dir wheelhouse recipes/native_probe
```

Build on a native ARM64 macOS runner for the ARM64 wheel and its ELF/static
verification. Cross-building another ABI on a host does not prove that ABI can
import.

## Signed repository snapshots

`scripts/stage_simple_index.py` creates a new snapshot directory and refuses to
replace an existing one. Its manifest records wheel, recipe, patch, SBOM and
Simple-index SHA-256 digests. The signed manifest therefore authenticates the
generated PEP 503 index as well as every wheel. `playRuntimeInstallAllowed` is
always false: the Play app must not download or execute native wheels at
runtime.

Generate a production Ed25519 keypair in a controlled environment; keep the
private key outside Git and store it only in a protected release environment.
The app must independently pin the public key before it trusts any repository
snapshot. Do not publish or consume unsigned snapshots as trusted releases.

```sh
python scripts/sign_manifest.py sign dist/repository/manifest.json \
  --private-key /secure/path/pyrun-ed25519-private.pem
python scripts/sign_manifest.py verify dist/repository/manifest.json \
  --public-key keys/pyrun-ed25519-public.pem \
  --signature dist/repository/manifest.json.sig
python scripts/promote_snapshot.py dist/repository dist/releases/2026-10-23 \
  --public-key keys/pyrun-ed25519-public.pem
```

Promotion verifies the signature and every recorded file digest, copies the
snapshot into a new destination, and never overwrites a previous release. To
roll back, promote a previously signed snapshot into another new deployment
directory; do not mutate the historical snapshot or replace it in place.

## Yank a release

Yanking is an explicit, reviewed change to `repository/yanks.json`. It retains
the wheel and marks its PEP 503 link with `data-yanked`, including the reason.
The resulting index and yank state are covered by the signed manifest. Rebuild
and sign a new snapshot after changing the policy.

```sh
python scripts/yank_version.py yank pyrun-native-probe 0.1.0 \
  --reason "Withdrawn while investigating a compatibility issue"
python scripts/stage_simple_index.py --wheel-dir wheelhouse \
  --output-dir dist/yanked-snapshot --source-revision "<40-character-commit>"
python scripts/yank_version.py unyank pyrun-native-probe 0.1.0
```

## Scope and release safety

- Only reviewed recipes with pinned build inputs, SPDX license, source-tree
  digest, patch hashes and CycloneDX SBOM may enter a snapshot.
- Pull-request workflows have read-only repository permissions and no signing
  credentials.
- CI snapshots are temporary review artifacts, not a published package index.
- A future Pages/release publisher must require a protected environment, a
  valid signature, and a reviewed release action. It must remain separate from
  the build workflow.
- Arbitrary pip packages, sdists, runtime native-wheel downloads, compilers,
  TensorFlow, PyTorch, OpenCV and SciPy are outside this proof package's scope.
