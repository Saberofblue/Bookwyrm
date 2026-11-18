"""Putting a new version of Bookwyrm in place of the running one, on Windows.

On Windows, Bookwyrm is a folder: Bookwyrm.exe and what it needs. A running program
cannot replace its own folder, so the *new* version, already unpacked somewhere else and
proven to run, is started with ``--finish-update`` and the old one quits. The new version
waits for that, moves the old folder aside, copies itself into its place, and starts the
copy. If the copy does not come up, the old folder is put back.

Because the folder keeps its place, shortcuts to Bookwyrm keep working.

The steps are ordinary file operations, written so they can be tested on any system. Only
waiting for another process to end, and starting one that outlives this one, are done the
Windows way.

``--finish-update`` is answered by a *newer* Bookwyrm than the one that calls it, so its
arguments must keep their meaning from version to version:

    --finish-update <process id to wait for> <folder to replace> <notes folder> <relaunch|stay>
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

from bookwyrm.updates import notes
from bookwyrm.updates.release import UpdateError

SYSTEM = "win32"  # the kind of download this module installs (see release.asset_name)
EXE = "Bookwyrm.exe"
PATIENCE = 60  # half-seconds to wait for the new version to come up before doubting it
_FROM_A_ZIP = re.compile(r"(?i)^(temp\d*_.*\.zip|7zo.*|rar\$.*)$")  # where zip tools unpack to


def installed_path(executable: str | None = None, frozen: bool | None = None) -> Path | None:
    """The folder this Bookwyrm runs from, or None when it is not a packaged Windows program."""
    if frozen is None:
        frozen = bool(getattr(sys, "frozen", False)) and sys.platform == "win32"
    return Path(executable or sys.executable).parent if frozen else None


def _can_write(folder: Path) -> bool:
    try:
        with tempfile.NamedTemporaryFile(dir=folder, prefix=".bookwyrm-check-"):
            return True
    except OSError:
        return False


def why_not(folder: Path | None) -> str | None:
    """Why Bookwyrm cannot replace itself here, or None if it can."""
    if folder is None:
        return ("This copy of Bookwyrm is running from its source code, so it cannot "
                "replace itself. Update it the way you got it.")
    if any(_FROM_A_ZIP.match(part) for part in folder.parts):
        return ("Bookwyrm seems to be running from inside the zip it came in, where it "
                "cannot be replaced. Extract the Bookwyrm folder somewhere it can stay, "
                "open it from there, and check for updates again.")
    if not _can_write(folder) or not _can_write(folder.parent):
        return (f"Bookwyrm is not allowed to change the folder it is kept in ({folder}), "
                "so it cannot replace itself there. Move the Bookwyrm folder somewhere of "
                "your own, such as Documents, and check for updates again.")
    return None


def _own_command() -> list[str]:
    """How to start this same Bookwyrm again: the program itself, or Python running it."""
    if getattr(sys, "frozen", False):
        return [sys.executable]
    return [sys.executable, "-m", "bookwyrm"]


def unpack_command(archive: Path, into: Path) -> list[str]:
    """Unpacking takes a few seconds, so it is done by a second copy of this program."""
    return [*_own_command(), "--unpack", str(archive), str(into)]


def unpack(archive: str, into: str) -> int:
    """``--unpack``: extract a release zip. Entries cannot land outside `into` (zipfile
    strips drive letters and '..'). Where the zip records that a file may be run, that
    is kept."""
    with zipfile.ZipFile(archive) as bundle:
        for entry in bundle.infolist():
            placed = bundle.extract(entry, into)
            mode = (entry.external_attr >> 16) & 0o777
            if mode and not entry.is_dir():
                os.chmod(placed, mode)
    return 0


def find_app(folder: Path) -> Path:
    """The one program folder inside an unpacked download, checked to be Bookwyrm."""
    found = [entry for entry in sorted(Path(folder).iterdir())
             if entry.is_dir() and (entry / EXE).is_file()]
    if len(found) != 1:
        raise UpdateError("The download did not contain Bookwyrm.")
    return found[0]


def executable_of(app: Path) -> Path:
    return Path(app) / EXE


def _detached() -> dict:
    """Arguments for starting a program that carries on after this one has gone."""
    if sys.platform == "win32":
        return {"creationflags": subprocess.DETACHED_PROCESS
                | subprocess.CREATE_NEW_PROCESS_GROUP, "close_fds": True}
    return {"start_new_session": True, "close_fds": True}


def start_swap(folder: Path, new_folder: Path, staging: Path, relaunch: bool = True,
               pid: int | None = None) -> subprocess.Popen:
    """Start the new version's finishing step, which swaps `new_folder` in for `folder`
    once process `pid` (this one, by default) has gone. Returns that step's process."""
    (Path(staging) / notes.OUTCOME).unlink(missing_ok=True)
    command = [str(executable_of(new_folder)), "--finish-update",
               str(os.getpid() if pid is None else pid), str(folder), str(staging),
               "relaunch" if relaunch else "stay"]
    try:
        # Not started inside the folder to be replaced: Windows will not move a folder
        # that is some program's current folder.
        return subprocess.Popen(command, cwd=str(staging), stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                **_detached())
    except OSError as problem:
        raise UpdateError(f"The update could not be started: {problem}") from problem


# -- the finishing step, run by the new version ---------------------------------------------


def wait_gone(pid: int, seconds: float) -> bool:
    """Wait for another process to end. True once it has (or was never there)."""
    if sys.platform == "win32":
        import ctypes

        synchronize = 0x00100000
        kernel = ctypes.windll.kernel32
        handle = kernel.OpenProcess(synchronize, False, int(pid))
        if not handle:
            return True
        try:
            return kernel.WaitForSingleObject(handle, int(seconds * 1000)) == 0
        finally:
            kernel.CloseHandle(handle)
    deadline = time.monotonic() + seconds
    while True:
        try:
            os.kill(int(pid), 0)
        except ProcessLookupError:
            return True
        except OSError:
            pass  # there, but not ours to signal
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.2)


def _persist(action, seconds: float = 30.0) -> OSError | None:
    """Do a file operation, trying again for a while if Windows says the files are in
    use (a virus scanner or the old version's helpers may hold them for a moment).
    Returns None once it worked, else the last error."""
    deadline = time.monotonic() + seconds
    while True:
        try:
            action()
            return None
        except OSError as problem:
            if time.monotonic() >= deadline:
                return problem
            time.sleep(0.5)


def _start_program(exe: Path) -> subprocess.Popen:
    return subprocess.Popen([str(exe)], cwd=str(Path.home()), stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **_detached())


def finish_update(pid: int, folder, staging, relaunch: bool, new_folder=None,
                  start=_start_program, patience: int = PATIENCE, retry_for: float = 30.0,
                  wait_for_exit: float = 600.0) -> int:
    """Replace `folder` with `new_folder` (by default, the folder this program runs from)
    once process `pid` has ended. Returns 0 if the new version is in place.

    `start` opens a program and returns something with poll(); `patience` is how many
    half-seconds the new version gets to come up; both are given only in tests.
    """
    folder, staging = Path(folder), Path(staging)
    new_folder = Path(new_folder) if new_folder else Path(sys.executable).parent
    previous = folder.with_name(folder.name + ".previous")
    exe = executable_of(folder)
    log = open(staging / "swap.log", "a", encoding="utf-8")

    def say(text: str) -> None:
        log.write(f"{time.strftime('%H:%M:%S')}  {text}\n")
        log.flush()

    def outcome(text: str) -> None:
        notes.write_outcome(staging, text)
        say(text)

    def reopen() -> None:
        if relaunch:
            try:
                start(exe)
            except OSError as problem:
                say(f"could not start {exe}: {problem}")

    try:
        say(f"waiting for Bookwyrm (process {pid}) to close")
        if not wait_gone(pid, wait_for_exit):
            outcome("failed: Bookwyrm did not close, so it was left as it is.")
            return 1
        (staging / notes.STARTED).unlink(missing_ok=True)
        _persist(lambda: shutil.rmtree(previous) if previous.exists() else None, 5.0)

        problem = _persist(lambda: os.rename(folder, previous), retry_for)
        if problem is not None:
            outcome("failed: the old version could not be moved aside "
                    f"(another program is using its folder: {problem}).")
            reopen()
            return 1
        try:
            shutil.copytree(new_folder, folder)
        except OSError as problem:
            say(f"copying the new version failed: {problem}")
            _persist(lambda: shutil.rmtree(folder) if folder.exists() else None, retry_for)
            _persist(lambda: os.rename(previous, folder), retry_for)
            outcome("failed: the new version could not be put in place, so the old one "
                    "was put back.")
            reopen()
            return 1
        outcome(notes.INSTALLED)
        if not relaunch:
            _persist(lambda: shutil.rmtree(previous), retry_for)
            return 0

        # Start the new version and give it time to show its window. It leaves a note
        # ("started") once it has. Without one, and with the program gone, it has failed.
        try:
            program = start(exe)
        except OSError as trouble:
            say(f"could not start the new version: {trouble}")
            program = None
        waited = 0
        while not (staging / notes.STARTED).exists():
            waited += 1
            alive = program is not None and program.poll() is None
            if (waited > patience and not alive) or waited > patience * 6:
                break
            if program is None:
                break
            time.sleep(0.5)
        came_up = (staging / notes.STARTED).exists() or (
            program is not None and program.poll() is None)
        if came_up:
            say("the new version is running")
            _persist(lambda: shutil.rmtree(previous), retry_for)
            return 0
        say("the new version did not start; putting the old one back")
        _persist(lambda: shutil.rmtree(folder) if folder.exists() else None, retry_for)
        back = _persist(lambda: os.rename(previous, folder), retry_for)
        outcome("failed: the new version did not start, so the old one was put back."
                if back is None else
                "failed: the new version did not start, and the old one could not be put "
                f"back; it is in {previous}.")
        reopen()
        return 1
    finally:
        log.close()


def finish_update_from_arguments(arguments: list[str]) -> int:
    """``--finish-update <pid> <folder> <notes folder> <relaunch|stay>``"""
    if len(arguments) < 4:
        return 2
    return finish_update(int(arguments[0]), arguments[1], arguments[2],
                         arguments[3] == "relaunch")
