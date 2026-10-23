# PyRun Android wheels

This is the separate source repository for reproducible Android wheels used by
PyRun. It is deliberately separate from the Android app and from PyPI. The
first recipe is a tiny compiled extension used to prove the build, ABI, index,
integrity, and 16 KB ELF checks before adding scientific packages.

## Support matrix

| Python | Android ABI | Minimum API | Build target |
| --- | --- | ---: | --- |
| CPython 3.13 | `arm64-v8a` | 25 | `android_25_arm64_v8a` |
| CPython 3.13 | `x86_64` | 25 | `android_25_x86_64` |

The GitHub Actions workflow builds each ABI on a matching hosted runner and
lets cibuildwheel run its Android testbed on that runner's architecture. The
Android SDK/NDK versions, cibuildwheel, Python build backend, recipe, and package
inputs are pinned. NDK r30 is used; it emits 16 KB-aligned ELF load segments by
default. Every native `.so` is checked again before it can enter the index.

## Build locally

Android wheel builds require Linux x86_64 or macOS ARM64/x86_64, Android command
line tools, Java, and the NDK version in `toolchain.lock`. Windows is not a
supported cibuildwheel host for Android. On a supported host:

```sh
python3.13 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-dev.txt
yes | "$ANDROID_HOME/cmdline-tools/latest/bin/sdkmanager" --licenses
"$ANDROID_HOME/cmdline-tools/latest/bin/sdkmanager" --install "ndk;30.0.16248370"
export ANDROID_NDK_HOME="$ANDROID_HOME/ndk/30.0.16248370"
ANDROID_API_LEVEL=25 python -m cibuildwheel --platform android \
  --archs arm64_v8a,x86_64 --output-dir wheelhouse recipes/native_probe
```

Then validate each wheel and build a local Simple Repository snapshot:

```sh
python scripts/verify_android_wheels.py wheelhouse/*.whl \
  --android-api 25 --readelf "$ANDROID_NDK_HOME/toolchains/llvm/prebuilt/linux-x86_64/bin/llvm-readelf"
python scripts/stage_simple_index.py --wheel-dir wheelhouse --output-dir dist/repository \
  --source-revision "$(git rev-parse HEAD)"
```

On macOS, find `llvm-readelf` under the NDK's `toolchains/llvm/prebuilt/*/bin`
directory and use that path. Cibuildwheel executes the native import smoke test
on a Gradle-managed Android emulator for the runner's architecture; it does not
test a different ABI by pretending to run it on the host.

## Repository and release security

- A wheel is executable code. Only reviewed recipes with pinned sources, an
  SPDX license, a source-tree digest, a build recipe, and an SBOM may be staged.
- The workflow on pull requests and branches only builds, tests, verifies, and
  uploads a temporary Actions artifact. It does not publish Pages, create a
  release, or access signing credentials.
- A production snapshot must have `manifest.json` signed with Ed25519. Use
  `scripts/sign_manifest.py` with a private key stored in a protected GitHub
  Actions environment; never commit the private key. The public key must be
  pinned in the consuming app before that app trusts this repository.
- The Play-distributed PyRun app must not download or execute native wheels at
  runtime. Native libraries need to be included at build time or delivered
  through an approved Play mechanism. A separate distribution profile requires
  a separate policy and release review.
- Repository index and manifest checksums detect accidental corruption; only a
  signature verified against a public key already trusted by the app protects
  against a substituted manifest.
- Never add unreviewed artifacts, secrets, credentials, or user workspace data.

## Current state

The local repository skeleton and CI are not yet published to GitHub. The proof
wheel has not yet been built in a Linux/macOS Android toolchain in this local
Windows session. Do not describe the ABI artifacts as available until CI
produces and verifies them.
