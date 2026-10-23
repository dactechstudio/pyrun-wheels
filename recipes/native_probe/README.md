# Native probe recipe

This package is intentionally small and has no runtime dependencies. It compiles
one CPython extension so the Android wheel pipeline can prove ABI tagging,
native import, package indexing, checksums, and ELF alignment before larger
native libraries are considered.

It is not intended as a general arithmetic package. Its `add()` and
`native_abi()` functions are smoke-test hooks only.
