# Phase 16 — wheel repository and Android native build pipeline

## Goal

Establish the independent, reproducible build and verification pipeline before
adding large native packages. The proof package is intentionally small; passing
this phase does not mean arbitrary PyPI packages can be installed by PyRun.

## Implemented

- Separate public repository: `dactechstudio/pyrun-wheels`.
- PEP 503 Simple Repository snapshot with a deterministic package index.
- CPython 3.13.17 / Android API 25 recipes for `arm64-v8a` and `x86_64`.
- Native CI runner split: Ubuntu 24.04 x86_64 for the `x86_64` wheel and
  macOS 15 ARM64 for the `arm64-v8a` wheel, matching cibuildwheel's supported
  Android host platforms and test-architecture selection.
- Locked NDK, cibuildwheel, build frontend, setuptools, wheel and validation
  tools; Actions are pinned to immutable commit SHAs.
- Repeat-build comparison using the same commit timestamp via
  `SOURCE_DATE_EPOCH`.
- Automated native import smoke test for the `x86_64` wheel in a managed
  Android emulator on GitHub Actions Linux x86_64, targeting the latest managed
  Android version available to the test runner. No physical device is used.
- The `arm64-v8a` wheel is built twice and checked for ABI, hashes and 16 KB ELF
  alignment on a native macOS ARM runner. Its managed emulator import test is
  currently unavailable on GitHub-hosted macOS: the first CI attempt could not
  start the emulator, and cibuildwheel lists GitHub Actions Linux x86_64 as the
  hosted environment known to support Android emulator virtualization. Full
  ARM64 import smoke coverage remains a runner capability gap, not a passed
  check.
- Wheel-tag/Python/API/ABI validation, wheel RECORD and SHA-256 checks, safe ZIP
  path validation, ELF-machine validation and 16 KB `PT_LOAD` alignment checks.
- Build-recipe and repository-manifest JSON schemas; recipe fields include
  patches, their hashes, SPDX license, source-tree hash, runtime dependencies
  and SBOM. The proof recipe currently has no patches or runtime dependencies.
- Ed25519 sign/verify tooling. The signed manifest covers the Simple index,
  wheels, recipe, patches and SBOM hashes.
- PEP 592 yank policy with reason, plus tests that build the corresponding
  `data-yanked` index entries.
- Immutable snapshot staging and signed promotion/rollback into a fresh
  deployment directory; overwrite attempts are rejected.
- CI artifacts remain unsigned, unpublished review snapshots. The build
  workflow has read-only permissions and no signing secret.
- Play-profile policy is encoded in each manifest record as
  `playRuntimeInstallAllowed: false`.

## Parity check against the reference app

The decompiled reference `PipActivity` uses a separate package-install screen,
runs pip through the app's Python process, and optionally adds a local
loopback `--index-url` backed by a prebuilt-library repository. It filters
packages that are built into the runtime or unsupported on Android and gates
some heavy packages; it is not simply a public PyPI mirror. This Phase 16 repo
therefore supplies the reproducible, curated-wheel build and static index
foundation only. It does not claim to reproduce that app UI, repository
service/plugin, package catalog, or arbitrary-source fallback. Any later app
integration must decide how package delivery is Play-compliant before enabling
installation; unsigned or arbitrary native wheels must not be executed by the
Play-distributed app.

## Security boundary / user setup still required for production publishing

The GitHub Actions workflow does not publish executable wheels to Pages or
create a release. Production trust requires an Ed25519 public key pinned by the
consuming app and its matching private key held in a GitHub protected
environment. No signing key was generated, committed, or uploaded as part of
this phase. Do not promote an unsigned Actions artifact.

Inspect the two ABI artifacts and the `simple-repository-snapshot` artifact.
The x86_64 import smoke test is automated; the ARM64 wheel has build,
repeatability and static ABI/ELF verification only until an emulator-capable
ARM64 test runner is available. A release workflow and Pages settings should
be enabled only after the production key has been created and the public key's
app-side trust/policy has been reviewed.

## Verification

Local Windows verification runs the repository unit tests. The authoritative
Android wheel builds and 16 KB checks run in GitHub Actions on the
corresponding native-architecture runner. The x86_64 import check runs in the
managed emulator; the GitHub-hosted macOS ARM emulator attempt failed, so ARM64
import coverage is explicitly not claimed. Phase 17 is not started by this
file.
