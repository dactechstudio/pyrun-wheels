# Kivy 2.3.1 for PyRun Android

This recipe is an experimental CPython 3.13 build for `arm64_v8a` and `x86_64`.
It compiles Kivy's SDL2, SDL2_image, SDL2_mixer, SDL2_ttf, OpenGL ES 2 and
window/input extensions against the separately reviewed PyRun SDL2 libraries.
The wheel does not carry copies of those shared SDL libraries; the matching APK
must package them and must use the PyRun Chaquopy-to-SDL Android bridge.

`prepare_kivy_source.py` fetches the exact Kivy revision and checksum, then
applies the Python 3.13 AST, Android Cython, SDL thread-swap, page-alignment and
license adaptations. `configure_kivy_android.py` validates the shared SDL
headers/libraries and writes the exact include/link search path for the ABI.
`verify_kivy_wheel.py` validates the wheel metadata, required extension modules,
dynamic SDL dependencies, MIT notice, bundled Roboto font, and 16 KB ELF load
alignment.

The signed wheel release is intentionally not approved yet. The wheel has not
been built, imported, or exercised in PyRun, and this project does not run an
Android emulator/device check unless that is explicitly authorized. Do not set
`pyrunKivyRelease` in an app build until the CI artifacts have been reviewed,
the runtime flow has been checked on a device, and the recipe is deliberately
promoted to `approved`.

The Kivy Android runtime APIs which require additional Android permissions,
camera/location services, or PyJNIus-specific Java inheritance are not included
in this first pack.
