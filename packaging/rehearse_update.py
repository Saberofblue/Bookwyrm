"""Rehearse "Update and Restart" with the packaged program, publishing nothing.

Needs a finished build with its download:  python packaging/build.py --zip

    python packaging/rehearse_update.py                   the automatic rehearsal, below
    python packaging/rehearse_update.py --with-relaunch   ...and let the new version open
    python packaging/rehearse_update.py --kit DIR         make a kit for trying the window

The automatic rehearsal
  1. builds a second, throwaway Bookwyrm that calls itself version 0.0.1 and "installs"
     it in a temporary folder;
  2. writes a release notice that names the real build's zip as the newest release;
  3. has the throwaway copy refuse a notice whose checksum is wrong;
  4. has it update itself from the true notice (the same steps the Update and Restart
     button runs, with no window, and without opening the new version);
  5. checks that the folder now holds the real version, intact and able to run.

The copy being replaced is always the throwaway one in the temporary folder. No installed
Bookwyrm, and no library, is touched.

--with-relaunch repeats the update with a second throwaway copy and lets the new version
open its window afterwards, as it does for a person. That starts the real program against
this computer's own library and settings, so it is refused outside an automated build
(where the computer is thrown away afterwards).

The kit (--kit DIR) is for a person to watch the real thing: it builds a copy of the
current code that calls itself one step newer (0.4.0 becomes 0.4.0.1), puts its download
and a release notice in DIR, and prints the command that starts the installed Bookwyrm
reading that notice. Check for Updates then offers the "newer" version, and Update and
Restart installs it for real.
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

MAC, WINDOWS = sys.platform == "darwin", sys.platform == "win32"
OLD = "0.0.1"
VERSION_FILE = ROOT / "src" / "bookwyrm" / "__init__.py"
FEED_VARIABLE = "BOOKWYRM_UPDATE_FEED"
APP = "Bookwyrm.app" if MAC else "Bookwyrm"  # what a build produces, and what is installed
HOME = "Applications" if MAC else "Programs"  # the stand-in for where it is installed


def say(text: str) -> None:
    print(text, flush=True)


def program(app: Path) -> str:
    if MAC:
        return str(app / "Contents" / "MacOS" / "Bookwyrm")
    return str(app / "Bookwyrm.exe")


def version_of(app: Path) -> str:
    """What a packaged copy calls itself. Asked for in a file: on Windows the program has
    no console to print to."""
    answer = app.parent / "version-answer.txt"
    answer.unlink(missing_ok=True)
    try:
        subprocess.run([program(app), "--version", str(answer)], capture_output=True,
                       timeout=180)
        return answer.read_text("utf-8").strip()
    except (OSError, subprocess.SubprocessError):
        return ""
    finally:
        answer.unlink(missing_ok=True)


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
    return into / APP


def current_version() -> str:
    return re.search(r'__version__ = "([^"]+)"', VERSION_FILE.read_text("utf-8")).group(1)


def write_notice(path: Path, version: str, archive: Path, checksum: str) -> None:
    path.write_text(json.dumps({
        "tag_name": f"v{version}", "name": f"Bookwyrm {version} (rehearsal)",
        "body": "A rehearsal of the update, from a file on this computer.",
        "html_url": archive.parent.as_uri(), "draft": False, "prerelease": False,
        "assets": [{"name": archive.name, "size": archive.stat().st_size,
                    "digest": f"sha256:{checksum}", "browser_download_url": archive.as_uri()}],
    }), "utf-8")


def update(app: Path, notice: Path, report: Path, relaunch: bool = False) -> tuple[int, str]:
    """Tell an installed copy to update itself from `notice`."""
    environment = dict(os.environ, **{FEED_VARIABLE: str(notice)})
    command = [program(app), "--update", str(report)]
    if not relaunch:
        command.insert(2, "--no-relaunch")
    done = subprocess.run(command, env=environment, capture_output=True, text=True,
                          timeout=900)
    said = report.read_text("utf-8") if report.exists() else done.stdout
    return done.returncode, said


def staging_folder() -> Path:
    """Where the packaged program keeps a download and its notes (its cache folder)."""
    from PySide6.QtCore import QCoreApplication, QStandardPaths

    app = QCoreApplication.instance() or QCoreApplication([])
    app.setOrganizationName("Bookwyrm")
    app.setApplicationName("Bookwyrm")
    cache = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.CacheLocation)
    return Path(cache) / "updates"


def running_from(app: Path) -> bool:
    """Is a program running from inside this copy of Bookwyrm?"""
    if WINDOWS:
        listed = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-Process Bookwyrm -ErrorAction SilentlyContinue).Path"],
            capture_output=True, text=True).stdout
        return any(Path(line.strip()) == Path(program(app))
                   for line in listed.splitlines() if line.strip())
    listed = subprocess.run(["ps", "-axo", "command="], capture_output=True, text=True).stdout
    return any(line.startswith(program(app)) for line in listed.splitlines())


def stop_running(app: Path) -> None:
    if WINDOWS:
        subprocess.run(["taskkill", "/F", "/T", "/IM", "Bookwyrm.exe"], capture_output=True)
    else:
        subprocess.run(["pkill", "-f", program(app)], capture_output=True)


def wait_until(condition, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.5)
    return bool(condition())


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
    if WINDOWS:
        say(f'  set "{FEED_VARIABLE}={notice}" && "<the Bookwyrm folder>\\Bookwyrm.exe"')
    else:
        say(f'  {FEED_VARIABLE}="{notice}" /Applications/Bookwyrm.app/Contents/MacOS/Bookwyrm')
    say("")
    say(f"Settings > Check for Updates will offer {newer}; Update and Restart installs it.")
    return 0


def main() -> int:
    if not (MAC or WINDOWS):
        say("Bookwyrm updates itself on a Mac and on Windows; there is nothing to rehearse here.")
        return 0
    if "--kit" in sys.argv:
        return make_kit(Path(sys.argv[sys.argv.index("--kit") + 1]))
    with_relaunch = "--with-relaunch" in sys.argv
    if with_relaunch and not os.environ.get("CI"):
        raise SystemExit("--with-relaunch opens the real program against this computer's own "
                         "library, so it only runs in an automated build.")
    archive = ROOT / "dist" / build.archive_name()
    if not archive.exists():
        raise SystemExit(f"Build first: {archive} is missing (packaging/build.py --zip).")
    new_version = current_version()
    checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
    # Resolved, so that paths here read as the system reports them for running programs
    # (a Mac's temporary folder is reached through a link).
    work = Path(tempfile.mkdtemp(prefix="bookwyrm-update-rehearsal-")).resolve()
    failures: list[str] = []

    def check(condition: bool, what: str) -> None:
        say(f"  [{' OK ' if condition else 'FAIL'}] {what}")
        if not condition:
            failures.append(what)

    def show(said: str) -> None:
        for line in said.strip().splitlines():
            say(f"      {line}")

    try:
        say(f"Rehearsing an update from {OLD} to {new_version} in {work}")
        say("Building the throwaway old version (a minute or two)…")
        built = build_as(OLD, work / "old-build")
        if with_relaunch:
            shutil.copytree(built, work / "second" / HOME / APP, symlinks=True)
        installed = work / HOME / APP
        installed.parent.mkdir(parents=True)
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
        show(said)
        check(code == 0, "the update steps finished without complaint")
        # The swap happens just after the old copy has exited. Wait for its note rather
        # than trying the program meanwhile: that would get in the way of the swap.
        check(wait_until((staging_folder() / "outcome.txt").exists, 300),
              "the swap step reported back")
        check(version_of(installed) == new_version,
              f"the installed copy now calls itself {new_version}")
        tested = subprocess.run([program(installed), "--self-test", str(work / "selftest.txt")],
                                capture_output=True, text=True, timeout=600)
        result = (work / "selftest.txt").read_text("utf-8") if tested.returncode == 0 else ""
        check("RESULT: all" in result, "the swapped-in program passes its own self-test")
        if MAC:
            signed = subprocess.run(["/usr/bin/codesign", "--verify", "--deep", str(installed)],
                                    capture_output=True, text=True)
            check(signed.returncode == 0,
                  "its signature is intact (nothing was lost in the move)")
        alone = wait_until(
            lambda: sorted(e.name for e in installed.parent.iterdir()) == [APP], 90)
        left = ", ".join(sorted(entry.name for entry in installed.parent.iterdir()))
        check(alone, f"nothing else is left beside it ({left})")
        log = staging_folder() / "swap.log"
        if log.exists():
            show(log.read_text("utf-8", errors="replace"))

        say("Asking GitHub, as the Check for Updates button does:")
        asked = work / "asked.txt"
        subprocess.run([program(installed), "--check-updates", str(asked)],
                       capture_output=True, timeout=180)
        heard = asked.read_text("utf-8") if asked.exists() else ""
        show(heard)
        # A busy network may be told to wait by GitHub; that is still an answer, received
        # over a secure connection, which is what a packaged build has to prove here.
        check(any(words in heard for words in ("is the newest version", "is available",
                                               "not answering requests from this network")),
              "the packaged program reached GitHub over a secure connection")

        if with_relaunch:
            say("Once more, letting the new version open as it does for a person:")
            second = work / "second" / HOME / APP
            started = staging_folder() / "started"
            code, said = update(second, notice, work / "relaunched.txt", relaunch=True)
            check(code == 0, "the update steps finished without complaint")
            check(wait_until(lambda: running_from(second), 300),
                  "a copy of Bookwyrm was opened afterwards and is running")
            check(wait_until(started.exists, 120),
                  "it told the waiting update step that it had come up")
            check(version_of(second) == new_version,
                  f"the second copy now calls itself {new_version}")
            check(wait_until(
                lambda: sorted(e.name for e in second.parent.iterdir()) == [APP], 120),
                "the old copy was then cleared away")
            if log.exists():
                show(log.read_text("utf-8", errors="replace"))
            stop_running(second)
            time.sleep(2.0)
    finally:
        shutil.rmtree(staging_folder(), ignore_errors=True)  # no stray "updated" note
        shutil.rmtree(work, ignore_errors=True)
    say("")
    say("RESULT: the update rehearsal passed" if not failures
        else f"RESULT: the update rehearsal FAILED ({len(failures)} of its checks)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
