#!/usr/bin/env python3
"""Apply PyRun's reviewed 16 KiB linker policy to Qt for Python 6.12.0.

The script intentionally fails when Qt's pinned template changes. This avoids
silently building a release with an unreviewed toolchain after an upstream
update.
"""

from __future__ import annotations

import argparse
from pathlib import Path


NEEDLE = (
    'set(QT_LINKER_FLAGS "-Wl,-O1 -Wl,--hash-style=gnu '
    "-Wl,-rpath='$ORIGIN' -Wl,-rpath='$ORIGIN/Qt/lib' \\\n"
    '                     -Wl,--as-needed -L{{ qt_install_path }}/android_{{ qt_plat_name }}/lib \\\n'
    '                     -L{{ qt_install_path }}/android_{{ qt_plat_name }}/plugins/platforms")'
)
REPLACEMENT = (
    'set(QT_LINKER_FLAGS "-Wl,-O1 -Wl,--hash-style=gnu '
    "-Wl,-rpath='$ORIGIN' -Wl,-rpath='$ORIGIN/Qt/lib' \\\n"
    '                     -Wl,--as-needed -Wl,-z,max-page-size=16384 '
    '-Wl,-z,common-page-size=16384 \\\n'
    '                     -L{{ qt_install_path }}/android_{{ qt_plat_name }}/lib \\\n'
    '                     -L{{ qt_install_path }}/android_{{ qt_plat_name }}/plugins/platforms")'
)


def patch(template: Path) -> None:
    original = template.read_text(encoding="utf-8")
    if original.count(NEEDLE) != 1:
        if original.count(REPLACEMENT) == 1:
            return
        raise ValueError("Qt for Python toolchain template differs from the pinned 6.12.0 source")
    template.write_text(original.replace(NEEDLE, REPLACEMENT), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, type=Path)
    args = parser.parse_args()
    template = (
        args.source.resolve()
        / "tools"
        / "cross_compile_android"
        / "templates"
        / "toolchain_default.tmpl.cmake"
    )
    patch(template)
    print(f"Applied the 16 KiB Android linker policy to {template}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
