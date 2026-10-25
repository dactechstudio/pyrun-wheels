# Android compiler-pack release

`build-toolchain.yml` is the Phase 25 release path. It resolves the pinned
Termux main repository for both supported Android ABIs, extracts the public
Clang/LLVM/LLD/Flang, Make, CMake, Ninja and pkg-config packages, and records
the exact package URLs and SHA-256 values inside each pack manifest.

The builder creates a private-prefix pack for PyRun rather than copying the
reference app's proprietary archive. The compact profile keeps Clang/Flang,
Make, m4, pkg-config and the required sysroot/runtime; CMake, Ninja and the
standalone binutils package are intentionally excluded. The pack includes
three proof programs:

- C compiled by `clang`;
- C++ compiled by `clang++`;
- Fortran compiled by LLVM `flang`.

The workflow's proof job boots an Android emulator for each ABI, pushes the
pack, compiles all three programs and executes the resulting binaries. The
signing job is gated on both proof jobs and signs each exact `manifest.json`
with the protected `wheel-release/PYRUN_ED25519_PRIVATE_KEY` secret.

When a hosted emulator cannot boot the target ABI, the same workflow supports
an explicit `physical_device_proof=true` dispatch. Before dispatching that
path, a real device must be connected and the exact build artifact must have
produced `pyrun-c-ok`, `pyrun-cpp-ok` and `pyrun-fortran-ok` locally. The
device model is supplied in `physical_device_model`; the protected
`wheel-release` approval remains mandatory before signing. This is a release
exception, not an emulator-proof substitute for future automated runs.

`publish-toolchain-pages.yml` is a separate, manual publication step. It
downloads only the successful signed run, verifies both signatures and every
tool digest, uploads the large archives as immutable GitHub Release assets,
then adds the catalog, manifests and signatures to `catalog/toolchains.json`
and the `gh-pages` Pages history. This split is required because GitHub rejects
individual Git/Pages files larger than 100 MB. The app installer follows the
HTTPS asset URL from the catalog, verifies the outer archive digest and inner
Ed25519 signature, then atomically activates a pack.

The first published compiler release is
[`termux-2026.10.25-r3`](https://github.com/dactechstudio/pyrun-wheels/releases/tag/termux-2026.10.25-r3),
with its catalog at
[`catalog/toolchains.json`](https://dactechstudio.github.io/pyrun-wheels/catalog/toolchains.json).

The workflow is intentionally not considered a completed Phase 25 proof until
one successful run has produced both ABI artifacts and either the Android
proof job or an approved physical-device proof has reported
`pyrun-c-ok`, `pyrun-cpp-ok` and `pyrun-fortran-ok` for the released ABI. The
other ABI must still have a build artifact and must be covered by a separate
device/emulator proof before claiming full two-ABI support.
