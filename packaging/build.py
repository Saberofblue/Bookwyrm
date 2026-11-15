"""Build the packaged Bookwyrm for the system this is run on.

    macOS    ->  dist/Bookwyrm.app
    Windows  ->  dist/Bookwyrm/Bookwyrm.exe   (a folder; keep its contents together)

Add --zip to also write the download that goes on a release page:
    macOS    ->  dist/Bookwyrm-mac-arm64.zip (or -x86_64, after the Mac it was built on)
    Windows  ->  dist/Bookwyrm-windows.zip
These names matter: Bookwyrm looks for them when it updates itself (updates/release.py).

Add --dist <folder> to build somewhere other than dist/ (used to rehearse an update).

Run it with the project's own Python, after `pip install -e .`:
    macOS:    .venv/bin/python packaging/build.py
    Windows:  .venv\\Scripts\\python packaging\\build.py
A build can only be made on the system it is for: a Mac cannot produce the
Windows program, nor the other way round.
"""

from __future__ import annotations

import platform
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MAC = sys.platform == "darwin"
WINDOWS = sys.platform == "win32"

# PyMuPDF 2.x lists machine-learning helpers as requirements, but Bookwyrm never
# loads them; leaving them out keeps the app a few hundred megabytes smaller.
UNUSED = ("onnxruntime", "numpy", "networkx", "pymupdf4llm", "pymupdf.layout",
          "pymupdf_layout", "pytest", "PIL", "tkinter")


def archive_name() -> str:
    if MAC:
        machine = platform.machine().lower()
        return f"Bookwyrm-mac-{'arm64' if machine in ('arm64', 'aarch64') else 'x86_64'}.zip"
    return f"Bookwyrm-{'windows' if WINDOWS else sys.platform}.zip"


def main() -> int:
    arguments = sys.argv[1:]
    dist = ROOT / "dist"
    if "--dist" in arguments:
        dist = Path(arguments[arguments.index("--dist") + 1]).resolve()
    work = dist.parent / "build"  # PyInstaller's scratch space, removed afterwards
    subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", "pyinstaller>=6.10"])
    shutil.rmtree(work, ignore_errors=True)
    for stale in ("Bookwyrm.app", "Bookwyrm"):  # leave other things in dist/ alone
        shutil.rmtree(dist / stale, ignore_errors=True)

    icon = ROOT / "packaging" / ("Bookwyrm.icns" if MAC else "Bookwyrm.ico")
    command = [
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--windowed", "--name", "Bookwyrm",
        "--icon", str(icon), "--paths", str(ROOT / "src"),
        "--collect-data", "bookwyrm", "--collect-binaries", "pymupdf",
        "--distpath", str(dist), "--workpath", str(work), "--specpath", str(work),
    ]
    if MAC:
        command += ["--osx-bundle-identifier", "org.bookwyrm.Bookwyrm"]
    for module in UNUSED:
        command += ["--exclude-module", module]
    command.append(str(ROOT / "packaging" / "launcher.py"))
    subprocess.check_call(command, cwd=ROOT)
    shutil.rmtree(work, ignore_errors=True)

    built = dist / ("Bookwyrm.app" if MAC else "Bookwyrm")
    if MAC:
        shutil.rmtree(dist / "Bookwyrm", ignore_errors=True)  # the loose folder beside the .app
    if WINDOWS:  # a note for whoever receives the folder, and the license it comes under
        shutil.copyfile(ROOT / "packaging" / "windows-readme.txt", built / "READ ME FIRST.txt")
        shutil.copyfile(ROOT / "LICENSE", built / "LICENSE.txt")
    print(f"Built {built}")
    if "--zip" in arguments:
        archive = dist / archive_name()
        archive.unlink(missing_ok=True)
        if MAC:  # ditto keeps the symbolic links and signature inside an app bundle intact
            subprocess.check_call(["ditto", "-c", "-k", "--keepParent", str(built), str(archive)])
        else:
            shutil.make_archive(str(archive.with_suffix("")), "zip", dist, built.name)
        print(f"Zipped {archive}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
