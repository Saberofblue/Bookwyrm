"""Make dist/Bookwyrm-for-Windows.zip: what a Windows user needs to install Bookwyrm.

The zip holds Bookwyrm's own code, the one-click installer and a read-me. The
recipient unzips it and double-clicks "Install Bookwyrm". It can be made on any
system (it is just files); the installing happens on the Windows PC.

    python packaging/make_windows_package.py
"""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FILES = ("READ ME FIRST.txt", "Install Bookwyrm.bat", "Start Bookwyrm.bat", "README.md", "LICENSE",
         "pyproject.toml", "installer/setup_bookwyrm.py", "packaging/Bookwyrm.ico")
SKIP_NAMES = {"__pycache__", ".DS_Store"}
SKIP_SUFFIXES = {".pyc", ".pyo"}


def package_files() -> list[Path]:
    files = [ROOT / name for name in FILES]
    for path in sorted((ROOT / "src").rglob("*")):
        if path.is_file() and path.suffix not in SKIP_SUFFIXES \
                and not SKIP_NAMES.intersection(path.parts):
            files.append(path)
    missing = [str(p) for p in files if not p.exists()]
    if missing:
        raise SystemExit(f"Cannot build the package; missing: {missing}")
    return files


def main() -> int:
    target = ROOT / "dist" / "Bookwyrm-for-Windows.zip"
    target.parent.mkdir(exist_ok=True)
    files = package_files()
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in files:
            archive.write(path, Path("Bookwyrm") / path.relative_to(ROOT))
    size = target.stat().st_size / 1_000_000
    print(f"Wrote {target} ({len(files)} files, {size:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
