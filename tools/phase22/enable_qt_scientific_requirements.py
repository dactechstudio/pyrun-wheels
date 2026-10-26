#!/usr/bin/env python3
"""Pin scientific p4a recipes in Qt's generated Buildozer configuration.

Qt for Python's Android deployer hardcodes its recipe list. Patch only the
reviewed deployer source revision, and fail if its contract changes.
"""

from __future__ import annotations

import argparse
from pathlib import Path


P4A_COMMIT = '58d21141f17c889bf8585f5665921d72028f8831'
ORIGINAL = 'self.set_value("app", "requirements", "python3,shiboken6,PySide6")'
SCIENTIFIC = 'self.set_value("app", "requirements", "python3,shiboken6,PySide6,numpy,matplotlib")'


def patch_deployer(path: Path) -> None:
    source = path.read_text(encoding="utf-8")
    if source.count(f'P4A_COMMIT = "{P4A_COMMIT}"') != 1:
        raise ValueError("Qt deployer p4a revision changed")
    if source.count(ORIGINAL) != 1 or SCIENTIFIC in source:
        raise ValueError("Qt deployer requirements contract changed")
    path.write_text(source.replace(ORIGINAL, SCIENTIFIC), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--deployer", required=True, type=Path)
    args = parser.parse_args()
    patch_deployer(args.deployer)
    print("Enabled pinned Qt scientific recipes: numpy, matplotlib")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
