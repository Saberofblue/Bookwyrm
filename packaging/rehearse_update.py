"""Rehearse "Update and Restart" with the packaged app, publishing nothing. Mac only so far.

Needs a finished build with its download:  python packaging/build.py --zip

    python packaging/rehearse_update.py              the automatic rehearsal, below
    python packaging/rehearse_update.py --kit DIR    make a kit for trying the real window

The automatic rehearsal
  1. builds a second, throwaway Bookwyrm that calls itself version 0.0.1 and "installs"
     it in a temporary Applications folder;
  2. writes a release notice that names the real build's zip as the newest release;
  3. has the throwaway copy refuse a notice whose checksum is wrong;
  4. has it update itself from the true notice (the same steps the Update and Restart
     button runs, with no window, and without opening the new version);
  5. checks that the folder now holds the real version, intact and able to run.

The app being replaced is always the throwaway copy in the temporary folder. Nothing
in /Applications, and no library, is touched.

The kit (--kit DIR) is for a person to watch the real thing: it builds a copy of the
current code that calls itself one step newer (0.4.0 becomes 0.4.0.1), puts its
download and a release notice in DIR, and prints the command that starts the installed
Bookwyrm reading that notice. Check for Updates then offers the "newer" version, and
Update and Restart installs it for real.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
import build  # noqa: E402  (packaging/build.py: for the download's name)

OLD = "0.0.1"
VERSION_FILE = ROOT / "src" / "bookwyrm" / "__init__.py"
FEED_VARIABLE = "BOOKWYRM_UPDATE_FEED"


def say(text: str) -> None:
    print(text, flush=True)


def program(app: Path) -> str:
    return str(app / "Contents" / "MacOS" / "Bookwyrm")


def version_of(app: Path) -> str:
    done = subprocess.run([program(app), "--version"], capture_output=True, text=True,
                          timeout=120)
    return done.stdout.strip()


def build_as(version: str, into: Path, zipped: bool = False) -> Path:
    """Build Bookwyrm calling itself `version`. The version lives in the source, so it is
    changed for the length of the build and put back afterwards, whatever happens."""
    original = VERSION_FILE.read_text("utf-8")
    changed = re.sub(r'__version__ = "[^"]+"', f'__version__ = "{version}"', original)
    if changed == original:
        raise SystemExit("Could not find the version to change.")
    command = [sys.executable, str(ROOT / "packaging" / "build.py"), "--dist", str(into)]
    try:
        VERSION_FILE.write_text(changed, "utf-8")
        done = subprocess.run(command + (["--zip"] if zipped else []), capture_output=True,
                              text=True)
    finally:
        VERSION_FILE.write_text(original, "utf-8")
    if done.returncode != 0:
        say(done.stdout[-3000:] + done.stderr[-3000:])
        raise SystemExit("The build did not work.")
    return into / "Bookwyrm.app"


def current_version() -> str:
    return re.search(r'__version__ = "([^"]+)"', VERSION_FILE.read_text("utf-8")).group(1)


def make_kit(folder: Path) -> int:
    """Everything needed to try Update and Restart in the real window, in `folder`."""
    folder = folder.expanduser().resolve()
    folder.mkdir(parents=True, exist_ok=True)
    newer = current_version() + ".1"
    say(f"Building a copy of Bookwyrm that calls itself {newer} (a minute or two)…")
    work = Path(tempfile.mkdtemp(prefix="bookwyrm-update-kit-"))
    try:
        build_as(newer, work / "dist", zipped=True)
        archive = folder / build.archive_name()
        shutil.move(str(work / "dist" / build.archive_name()), str(archive))
    finally:
        shutil.rmtree(work, ignore_errors=True)
    notice = folder / "notice.json"
    write_notice(notice, newer, archive, hashlib.sha256(archive.read_bytes()).hexdigest())
    say("")
    say(f"The kit is in {folder}")
    say("With a packaged Bookwyrm installed (version "
        f"{current_version()} or later), quit it, then start it with:")
    say("")
    say(f'  {FEED_VARIABLE}="{notice}" /Applications/Bookwyrm.app/Contents/MacOS/Bookwyrm')
    say("")
    say(f"Settings > Check for Updates will offer {newer}; Update and Restart installs it.")
    return 0


def write_notice(path: Path, version: str, archive: Path, checksum: str) -> None:
    path.write_text(json.dumps({
        "tag_name": f"v{version}", "name": f"Bookwyrm {version} (rehearsal)",
        "body": "A rehearsal of the update, from a file on this computer.",
        "html_url": archive.parent.as_uri(), "draft": False, "prerelease": False,
        "assets": [{"name": archive.name, "size": archive.stat().st_size,
                    "digest": f"sha256:{checksum}", "browser_download_url": archive.as_uri()}],
    }), "utf-8")


def update(app: Path, notice: Path, report: Path) -> tuple[int, str]:
    """Tell the installed copy to update itself from `notice`, without opening the result."""
    environment = dict(os.environ, **{FEED_VARIABLE: str(notice)})
    done = subprocess.run([program(app), "--update", "--no-relaunch", str(report)],
                          env=environment, capture_output=True, text=True, timeout=900)
    said = report.read_text("utf-8") if report.exists() else done.stdout
    return done.returncode, said


def main() -> int:
    if sys.platform != "darwin":
        say("The update rehearsal is written for the Mac so far.")
        return 0
    if "--kit" in sys.argv:
        return make_kit(Path(sys.argv[sys.argv.index("--kit") + 1]))
    archive = ROOT / "dist" / build.archive_name()
    if not archive.exists():
        raise SystemExit(f"Build first: {archive} is missing (packaging/build.py --zip).")
    new_version = current_version()
    checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
    work = Path(tempfile.mkdtemp(prefix="bookwyrm-update-rehearsal-"))
    failures: list[str] = []

    def check(condition: bool, what: str) -> None:
        say(f"  [{' OK ' if condition else 'FAIL'}] {what}")
        if not condition:
            failures.append(what)

    try:
        say(f"Rehearsing an update from {OLD} to {new_version} in {work}")
        say("Building the throwaway old version (a minute or two)…")
        built = build_as(OLD, work / "old-build")
        applications = work / "Applications"
        applications.mkdir()
        installed = applications / "Bookwyrm.app"
        shutil.move(str(built), str(installed))
        check(version_of(installed) == OLD, f"the installed copy calls itself {OLD}")

        say("A release whose download does not match its checksum:")
        notice = work / "notice.json"
        write_notice(notice, new_version, archive, "0" * 64)
        code, said = update(installed, notice, work / "refused.txt")
        check(code != 0 and "checksum" in said, "it is refused, and says why")
        check(version_of(installed) == OLD, "the installed copy is unchanged")

        say("The true release:")
        write_notice(notice, new_version, archive, checksum)
        code, said = update(installed, notice, work / "updated.txt")
        for line in said.strip().splitlines():
            say(f"      {line}")
        check(code == 0, "the update steps finished without complaint")
        deadline = time.monotonic() + 180  # the swap happens just after the old copy exits
        while time.monotonic() < deadline and version_of(installed) != new_version:
            time.sleep(0.5)
        check(version_of(installed) == new_version,
              f"the installed copy now calls itself {new_version}")
        tested = subprocess.run([program(installed), "--self-test", str(work / "selftest.txt")],
                                capture_output=True, text=True, timeout=600)
        result = (work / "selftest.txt").read_text("utf-8") if tested.returncode == 0 else ""
        check("RESULT: all" in result, "the swapped-in app passes its own self-test")
        signed = subprocess.run(["/usr/bin/codesign", "--verify", "--deep", str(installed)],
                                capture_output=True, text=True)
        check(signed.returncode == 0, "its signature is intact (nothing was lost in the move)")
        time.sleep(1.0)
        left = sorted(entry.name for entry in applications.iterdir())
        check(left == ["Bookwyrm.app"], f"nothing else is left beside it ({', '.join(left)})")
    finally:
        _forget_outcome()
        shutil.rmtree(work, ignore_errors=True)
    say("")
    say("RESULT: the update rehearsal passed" if not failures
        else f"RESULT: the update rehearsal FAILED ({len(failures)} of its checks)")
    return 1 if failures else 0


def _forget_outcome() -> None:
    """The rehearsed swap left a note ("installed") for the next start of Bookwyrm to
    announce. Nothing was really updated on this computer, so remove it."""
    try:
        from PySide6.QtCore import QCoreApplication, QStandardPaths

        app = QCoreApplication.instance() or QCoreApplication([])
        app.setOrganizationName("Bookwyrm")
        app.setApplicationName("Bookwyrm")
        cache = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.CacheLocation)
        shutil.rmtree(Path(cache) / "updates", ignore_errors=True)
    except Exception:
        pass


if __name__ == "__main__":
    sys.exit(main())
