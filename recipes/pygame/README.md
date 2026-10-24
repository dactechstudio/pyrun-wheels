# Pygame Android wheel recipe

This recipe builds upstream Pygame 2.6.1 for PyRun's CPython 3.13 ABI on
`arm64-v8a` and `x86_64`. It uses Pygame's upstream `Setup.Android.SDL2.in`
template, pinned SDL2/SDL2_image/SDL2_mixer/SDL2_ttf sources, and statically
linked FreeType. The APK supplies the matching SDL shared libraries and SDL's
Android Activity; the wheel contains only Pygame's Python and extension modules.

Image decoding uses SDL_image's bundled stb backend. Audio includes WAV,
MP3/minimp3, and OGG/stb_vorbis. Optional codecs requiring separate native
packages are disabled. This is an experimental Android package, not a promise
that arbitrary desktop Pygame projects or plugins work unchanged.

Release checks validate the two wheel tags, the full Android Setup extension
set, Pygame and SDL link dependencies, ELF machine and 16 KB load alignment.
The source build preserves Pygame's LGPL and SDL_gfx license notices; the APK
also includes notices for SDL, FreeType, stb_image, stb_vorbis and minimp3.
Android import, drawing, audio, touch, IME, pause/resume, rotation and
cancellation tests remain a separate device/emulator acceptance step; this
build workflow intentionally skips emulator tests.

Pygame is LGPL-2.1-or-later. The SDL runtime stack is built into the APK and its
upstream licenses are copied to the app's license assets. Preserve the LGPL
license and provide the corresponding source/build recipe when distributing
the app or wheel.
