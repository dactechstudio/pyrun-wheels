# Android compiler-pack release

`build-toolchain.yml` is the Phase 25 release path. It resolves the pinned
Termux main repository for both supported Android ABIs, extracts the public
Clang/LLVM/LLD/Flang, Make, CMake, Ninja and pkg-config packages, and records
the exact package URLs and SHA-256 values inside each pack manifest.

The builder creates a private-prefix pack for PyRun rather than copying the
reference app's proprietary archive. The pack includes three proof programs:

- C compiled by `clang`;
- C++ compiled by `clang++`;
- Fortran compiled by LLVM `flang`.

The workflow's proof job boots an Android emulator for each ABI, pushes the
pack, compiles all three programs and executes the resulting binaries. The
signing job is gated on both proof jobs and signs each exact `manifest.json`
with the protected `wheel-release/PYRUN_ED25519_PRIVATE_KEY` secret.

`publish-toolchain-pages.yml` is a separate, manual publication step. It
downloads only the successful signed run, verifies both signatures and every
tool digest, then adds immutable entries to `catalog/toolchains.json` and the
`gh-pages` Pages history. The app installer verifies the outer archive digest
and inner Ed25519 signature before atomically activating a pack.

The workflow is intentionally not considered a completed Phase 25 proof until
one successful run has produced both ABI artifacts and the Android proof job
has reported `pyrun-c-ok`, `pyrun-cpp-ok` and `pyrun-fortran-ok` for each ABI.
