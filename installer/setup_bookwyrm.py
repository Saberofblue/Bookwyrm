"""Set Bookwyrm up in this folder. Run by "Install Bookwyrm.bat".

Uses only Python's standard library, because nothing else is installed yet. It
makes a private Python environment inside this folder (nothing is installed
system-wide), downloads the two packages Bookwyrm needs, runs Bookwyrm's
self-test, adds shortcuts and starts the program. Everything it does and every
error is written to "setup-log.txt" next to the installer.

Options (for whoever maintains Bookwyrm):
    --no-launch      do not start Bookwyrm at the end
    --no-shortcuts   do not create Desktop and Start-menu shortcuts
    --fresh          rebuild the private environment from scratch
"""

from __future__ import annotations

import datetime
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENV = ROOT / ".venv"
LOG = ROOT / "setup-log.txt"
WINDOWS = sys.platform == "win32"

# Installed one by one so the log shows which download failed. PyMuPDF is installed
# without its optional companions: Bookwyrm does not use them and they triple the download.
PACKAGES = (
    ("the window toolkit (about 80 MB)", ["PySide6-Essentials==6.12.0"]),
    ("the PDF engine (about 25 MB)", ["--no-deps", "pymupdf==2.1"]),
)


class SetupError(Exception):
    """Something the person installing can understand and pass on."""


_log_lines: list[str] = []


def note(text: str = "", show: bool = True) -> None:
    _log_lines.append(text)
    if show:
        print(text, flush=True)
    try:
        LOG.write_text("\n".join(_log_lines) + "\n", encoding="utf-8")
    except OSError:
        pass


def python_in_venv(windowed: bool = False) -> Path:
    if WINDOWS:
        return VENV / "Scripts" / ("pythonw.exe" if windowed else "python.exe")
    return VENV / "bin" / "python"


def run(command: list, doing: str, timeout: int = 3600, slow: bool = False) -> str:
    """Run a command, keep its output in the log, and fail with a plain message.

    For `slow` steps a dot is printed every few seconds, so a long download does
    not look like a frozen window.
    """
    note(f"    $ {' '.join(str(part) for part in command)}", show=False)
    try:
        with tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as output:
            process = subprocess.Popen([str(part) for part in command], stdout=output,
                                       stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                       cwd=str(ROOT))
            started, dots = time.monotonic(), 0
            while True:
                try:
                    process.wait(timeout=4)
                    break
                except subprocess.TimeoutExpired:
                    if time.monotonic() - started > timeout:
                        process.kill()
                        raise SetupError(
                            f"Gave up on {doing} after {timeout // 60} minutes.") from None
                    if slow:
                        print("." if dots else "    .", end="", flush=True)
                        dots += 1
            if dots:
                print(flush=True)
            output.seek(0)
            text = output.read()
    except OSError as problem:
        raise SetupError(f"Could not finish {doing}: {problem}") from problem
    for line in text.rstrip().splitlines()[-200:]:
        note("    | " + line, show=False)
    if process.returncode != 0:
        raise SetupError(f"Could not finish {doing} (exit code {process.returncode}).")
    return text


def check_python() -> None:
    version = sys.version_info
    note(f"Python {platform.python_version()} at {sys.executable}", show=False)
    if version < (3, 11):
        raise SetupError(
            f"Bookwyrm needs Python 3.11 or newer; this is Python {platform.python_version()}. "
            "Install a newer one from https://www.python.org/downloads/ and run this again.")
    if sys.maxsize <= 2**32:
        raise SetupError(
            "This is a 32-bit Python, and Bookwyrm needs the 64-bit one. Install "
            '"Windows installer (64-bit)" from https://www.python.org/downloads/ and run '
            "this again.")


def make_environment(fresh: bool) -> None:
    if fresh and VENV.exists():
        shutil.rmtree(VENV, ignore_errors=True)
    if python_in_venv().exists():
        note("  A private Python environment is already here; reusing it.")
        return
    if VENV.exists():  # e.g. copied from another kind of computer: not usable here
        shutil.rmtree(VENV, ignore_errors=True)
    note("  Making a private Python environment in this folder...")
    run([sys.executable, "-m", "venv", VENV], "making the Python environment")


def install_packages() -> None:
    python = python_in_venv()
    for what, arguments in PACKAGES:
        note(f"  Downloading {what}...")
        run([python, "-m", "pip", "install", "--disable-pip-version-check", "--no-input",
             *arguments], f"downloading {what}; is this PC online?", slow=True)
    # Point the environment at Bookwyrm's own code in this folder.
    site = run([python, "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
               "locating the environment").strip().splitlines()[-1]
    (Path(site) / "bookwyrm.pth").write_text(str(ROOT / "src") + "\n", encoding="utf-8")


def self_test() -> None:
    note("  Checking that everything works on this computer...")
    report = ROOT / "selftest-report.txt"
    try:
        output = run([python_in_venv(), "-m", "bookwyrm", "--self-test", report],
                     "Bookwyrm's self-test", timeout=600)
    except SetupError:
        if report.exists():
            note("", show=False)
            for line in report.read_text("utf-8", errors="replace").splitlines():
                note("    " + line, show=False)
        raise SetupError("Bookwyrm was installed, but its self-test found a problem.") from None
    for line in output.splitlines():
        if line.startswith(("[ OK ]", "[FAIL]", "RESULT")):
            note("    " + line)


def _quoted(value) -> str:
    """A PowerShell single-quoted string."""
    return "'" + str(value).replace("'", "''") + "'"


def make_shortcuts() -> None:
    if not WINDOWS:
        note("  (Shortcuts are only made on Windows.)")
        return
    script = "; ".join([
        "$ErrorActionPreference = 'Stop'",
        "$shell = New-Object -ComObject WScript.Shell",
        "foreach ($place in @('Desktop', 'Programs')) { "
        "$link = $shell.CreateShortcut((Join-Path ([Environment]::GetFolderPath($place)) "
        "'Bookwyrm.lnk')); "
        f"$link.TargetPath = {_quoted(python_in_venv(windowed=True))}; "
        "$link.Arguments = '-m bookwyrm'; "
        f"$link.WorkingDirectory = {_quoted(ROOT)}; "
        f"$link.IconLocation = {_quoted(ROOT / 'packaging' / 'Bookwyrm.ico')}; "
        "$link.Description = 'Bookwyrm: a library, reader and editor for your PDFs'; "
        "$link.Save() }",
    ])
    try:
        run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
            "making the shortcuts", timeout=120)
        note("  Added a Bookwyrm shortcut to the Desktop and the Start menu.")
    except SetupError as problem:  # not fatal: "Start Bookwyrm.bat" still works
        note(f"  Could not make shortcuts ({problem}). "
             'Use "Start Bookwyrm" in this folder to open Bookwyrm instead.')


def launch() -> None:
    note("  Opening Bookwyrm...")
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess,
                                                                 "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen([str(python_in_venv(windowed=True)), "-m", "bookwyrm"], cwd=str(ROOT),
                     creationflags=flags, close_fds=True)


def main() -> int:
    options = set(sys.argv[1:])
    note(f"Bookwyrm setup, {datetime.datetime.now():%Y-%m-%d %H:%M}", show=False)
    note(f"System: {platform.platform()} ({platform.machine()})", show=False)
    note(f"Folder: {ROOT}", show=False)
    note("Setting up Bookwyrm. This downloads about 110 MB; allow 5 to 10 minutes.")
    note()
    try:
        check_python()
        make_environment("--fresh" in options)
        install_packages()
        self_test()
        if "--no-shortcuts" not in options:
            make_shortcuts()
        note()
        note("Bookwyrm is ready.")
        if "--no-launch" not in options:
            launch()
        return 0
    except SetupError as problem:
        note()
        note(f"SETUP DID NOT FINISH: {problem}")
    except Exception as problem:  # anything unforeseen still ends with a useful log
        import traceback
        note()
        note(f"SETUP DID NOT FINISH: an unexpected error ({type(problem).__name__}: {problem}).")
        for line in traceback.format_exc().rstrip().splitlines():
            note("    " + line, show=False)
    note()
    note(f'Please send the file "{LOG.name}" from this folder to whoever gave you Bookwyrm:')
    note(f"  {LOG}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
