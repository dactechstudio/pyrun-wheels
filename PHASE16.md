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
- Safe Ed25519 keypair generator: private output is required outside the repo,
  public output is restricted to `keys/`, and existing files are never replaced.
- Separate, manual signed-snapshot workflow. It accepts only a successful build
  run on `main`, uses the protected `wheel-release` environment, reads the
  private PEM from a GitHub secret without writing it to disk, verifies the
  signature, promotes into a fresh directory, and uploads a signed review
  artifact. It has read-only GitHub permissions and never runs for PRs/pushes.
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

## Security boundary / remaining release setup

The build workflow does not publish executable wheels to Pages or create a
GitHub Release. The separate signing workflow produces a signed review artifact
only. Before it can run, the owner must generate an Ed25519 keypair, commit the
public key at `keys/pyrun-ed25519-public.pem`, and add the private PEM as the
`PYRUN_ED25519_PRIVATE_KEY` secret in a protected GitHub environment named
`wheel-release`. Configure required reviewers and restrict deployment branches
to `main`. No production key was generated or stored by this repository work.

The consuming app must independently pin and verify the public key before
trusting a repository snapshot. Public Pages/GitHub Release deployment remains
unimplemented until the app-side trust policy and repository hosting procedure
are reviewed. Never promote or consume an unsigned Actions artifact as trusted.

The x86_64 import smoke test is automated; the ARM64 wheel has build,
repeatability and static ABI/ELF verification only until an emulator-capable
ARM64 test runner is available. The unsigned `simple-repository-snapshot` is a
temporary build artifact for review; the signed workflow output is also a
temporary review artifact, not a public package index.

## Verification

Local Windows verification runs the repository unit tests, including keypair
path protections, secret-environment signing, and release-workflow policy. The
authoritative Android wheel builds and 16 KB checks run in GitHub Actions on the
corresponding native-architecture runner. The x86_64 import check runs in the
managed emulator; the GitHub-hosted macOS ARM emulator attempt failed, so ARM64
import coverage is explicitly not claimed. Public deployment and live signing
remain pending owner-provided key setup. Phase 17 is not started by this file.
