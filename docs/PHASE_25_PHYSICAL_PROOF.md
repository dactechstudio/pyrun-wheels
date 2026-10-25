# Phase 25 physical-device proof

Release candidate: `termux-2026.10.25-r3`

| Field | Value |
| --- | --- |
| Device | Samsung SM-A515F |
| ABI | `arm64-v8a` |
| Proof source | `proof/hello.c`, `proof/hello.cpp`, `proof/hello.f90` |
| Toolchain commit | `b226d66` |
| Result | PASS |

The exact `compiler-pack-arm64_v8a` artifact from workflow run `40176088252`
was unpacked on the connected device and executed from its private temporary
prefix. The proof command compiled and ran all three programs:

```text
pyrun-c-ok
pyrun-cpp-ok
pyrun-fortran-ok
```

This receipt covers the connected ARM64 device only. The `x86_64` pack still
requires an emulator or x86_64 Android device proof before claiming complete
two-ABI compiler support.
