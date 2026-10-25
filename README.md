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

Every push and pull request runs the repository tests, builds the probe wheels
for both ABIs, verifies repeatability, wheel tags, hashes and ELF alignment,
and stages an unsigned Simple Repository snapshot. Emulator tests are disabled
by default, including on manual runs, to honor the project's build-only CI
policy. A maintainer may explicitly enable the x86_64 managed-emulator smoke
test for a workflow dispatch; arm64-v8a is statically verified only. OpenCV is
an opt-in source build in manual runs and is also statically verified without
emulator execution. The workflow does not publish to GitHub Pages or release
binaries; its unsigned snapshot is an Actions artifact retained for 14 days.

The optional `opencv-python-headless` recipe pins upstream OpenCV-Python and
OpenCV source revisions, and targets CPython 3.13 / Android API 25 for both
ABIs. It is not part of routine push/PR builds. Dispatch `Build and verify
Android wheels` from `main` with `build_opencv=true`; the run builds the source,
checks both wheel tags/ELF architectures, and includes both ABI artifacts in
the review snapshot. This is not yet runtime import verification. The recipe is
marked experimental, and the signing tool blocks it from a signed snapshot
until a reviewed change approves it after build-output, license, and package
content checks.

The Kivy 2.3.1 recipe is approved and published for both supported ABIs as
`kivy-2.3.1-20261024-r1`. Its wheels passed the package verifier, including the
required SDL extensions/linkage and 16 KB ELF alignment checks. The signed
manifest is available at
<https://dactechstudio.github.io/pyrun-wheels/releases/kivy-2.3.1-20261024-r1/manifest.json>.
This is static build/package verification only; Android import and GUI runtime
acceptance have not been performed.

The separate `Sign and promote wheel snapshot` workflow is manually dispatched
from `main`. It accepts only a successful `build-android.yml` run on `main`,
requires the protected `wheel-release` environment, signs and verifies the
manifest with Ed25519, promotes it into a new release directory, and uploads a
90-day signed review artifact. It does not publish to Pages or create a GitHub
Release.

The `Publish signed wheel snapshot to GitHub Pages` workflow is also manually
dispatched from `main`. Give it the successful signing-workflow run ID and the
same release ID, then approve the protected `github-pages` environment. It
downloads only that run's signed artifact, verifies the signature and all
manifest hashes again, adds the snapshot to an immutable `gh-pages` history
branch, and deploys a cumulative static site with GitHub Pages Actions. The
catalog is at `/catalog.json`; a snapshot is served from
`/releases/<release-id>/`, including its PEP 503 index at
`/releases/<release-id>/simple/`. Re-running the same release with identical
contents is safe; reusing its ID for different contents is rejected.

Before the first publication, set the repository's Pages source to **GitHub
Actions** in Settings → Pages → Build and deployment. The repo is currently
configured for the legacy `main` branch source; the publisher does not change
repository settings automatically. The `gh-pages` branch is only durable
release history, not the Pages build source.

## Android compiler packs (Phase 25)

The separate `Build and prove signed Android compiler packs` workflow builds a
real Android-hosted compiler pack for both `arm64_v8a` and `x86_64` from the
pinned public Termux package index. It contains Clang/Clang++, LLD, LLVM
Make, m4, pkg-config and LLVM Flang, with C/C++/Fortran
proof sources. When `run_android_proof` is enabled, managed Android emulators
compile and execute all three proofs before the protected signing job is
allowed to run. The signing job emits two immutable signed pack artifacts.

Run `Publish signed Android compiler packs to GitHub Pages` with that signing
run ID and a new release ID to publish `catalog/toolchains.json` and the pack
archives. The PyRun app verifies the archive digest, Ed25519 manifest
signature and tool hashes before activation; no compiler is enabled when a
pack is missing or invalid. The source app's proprietary compiler archive is
not redistributed.

No physical device is required for the optional x86_64 smoke test. Full
arm64-v8a import smoke coverage needs a runner that can provide a working
Android ARM64 emulator. See [`PHASE16.md`](PHASE16.md) for status, signing,
yank, and rollback procedures.

## Local build

Use Linux x86_64 or macOS ARM64/x86_64 with Android command-line tools, Java 17,
and the pinned NDK. Windows is not a supported cibuildwheel Android host.
GitHub Actions is the canonical two-ABI build environment. It does not run
emulator or physical-device tests unless the supported x86_64 emulator option
is explicitly enabled.

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

Generate a production Ed25519 keypair in a controlled environment. The helper
refuses to put the private key inside this repository and refuses to overwrite
either output. Only commit the public key. Store the private PEM only as the
`PYRUN_ED25519_PRIVATE_KEY` secret in the protected GitHub Actions environment
named `wheel-release`; configure required reviewers and restrict deployments to
`main`. Never paste the private key into chat, an issue, or a workflow file.
The app must independently pin the public key before it trusts any repository
snapshot. Do not publish or consume unsigned snapshots as trusted releases.

On Windows PowerShell, from the repository root, for example:

```powershell
$privateKey = Join-Path $env:USERPROFILE 'pyrun-signing\pyrun-ed25519-private.pem'
.venv\Scripts\python.exe scripts\generate_signing_key.py --private-key $privateKey
```

The command creates `keys/pyrun-ed25519-public.pem` for review and commit, and
creates the private key under your user profile. Add the exact private PEM text
as the GitHub environment secret; do not commit or upload the private file as
an Actions artifact. Then dispatch `Sign and promote wheel snapshot` from
`main`, provide the run ID of a successful `Build and verify Android wheels`
run on `main`, and choose a new release ID. After that workflow succeeds,
dispatch `Publish signed wheel snapshot to GitHub Pages` from `main` with the
signing run ID and the same release ID. Approve the `github-pages` environment
deployment. The publisher re-verifies the signed artifact before making it
public.

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
- The Pages publisher is separate from the build and signing workflows. It
  requires a protected environment, a valid signature, and a reviewed release
  action; it never receives the private signing key.
- Arbitrary pip packages, sdists, runtime native-wheel downloads,
  TensorFlow, PyTorch and SciPy are outside this proof package's scope. OpenCV
  has a separate experimental, build-only recipe and is not yet a published or
  runtime-verified package.
