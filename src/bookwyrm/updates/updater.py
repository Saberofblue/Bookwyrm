"""The steps of an update, in order.

    check()     read the release notice: is there a newer version?
    prepare()   download it, check it against the checksum the release gives, unpack it,
                and make the new version prove itself (right version, passes its self-test)
    install()   hand over to the swap step; the caller then quits the app

Nothing is changed on this computer's copy of Bookwyrm until install(), and by then the
new version has already run successfully here.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, QStandardPaths, QTimer, Signal

from bookwyrm import __version__
from bookwyrm.updates import mac, notes, release, windows
from bookwyrm.updates.fetch import CANCELLED, Fetcher
from bookwyrm.updates.release import Release, UpdateError

ARCHIVE, UNPACKED = "download.zip", "new"


def backend_for(system: str | None = None):
    """The module that knows how Bookwyrm is installed and replaced on a system, or None
    where Bookwyrm cannot replace itself."""
    system = sys.platform if system is None else system
    return mac if system == "darwin" else windows if system == "win32" else None


def default_staging() -> Path:
    """Where a download is kept and unpacked: Bookwyrm's cache folder, never the library."""
    base = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.CacheLocation)
    return Path(base or Path.home() / ".bookwyrm-cache") / "updates"


class Updater(QObject):
    checked = Signal(object)  # a Release when a newer version exists, else None
    failed = Signal(str)  # what went wrong, for the user; nothing was changed
    progressed = Signal(str, int, int)  # what is being done; bytes done and expected (0 = n/a)
    ready = Signal()  # the new version is here, tested and waiting for install()

    def __init__(self, current: str | None = None, staging=None, bundle=..., parent=None,
                 backend=...):
        super().__init__(parent)
        self.current = current or __version__
        self.source = release.source()
        self.staging = Path(staging) if staging else default_staging()
        self.backend = backend_for() if backend is ... else backend
        # The app to replace: Bookwyrm.app on a Mac, the Bookwyrm folder on Windows. Tests
        # pass one; normally it is the one this process runs from.
        if bundle is ...:
            bundle = self.backend.installed_path() if self.backend else None
        self.bundle: Path | None = bundle
        self.latest: Release | None = None  # the newest release, once check() has heard
        self.fetcher = Fetcher(self)
        # Methods, not closures over `self`, for the reason given in fetch.Fetcher.
        self.fetcher.notice.connect(self._heard)
        self.fetcher.progressed.connect(self._download_progressed)
        self.fetcher.downloaded.connect(self._downloaded)
        self._process: QProcess | None = None
        self._watchdog = QTimer(self, singleShot=True)
        self._watchdog.timeout.connect(self._took_too_long)
        self._new_bundle: Path | None = None
        self._swapper = None
        self._stage = ""  # '', 'checking', 'downloading', 'unpacking', 'version', 'selftest'
        self.is_ready = False

    # -- is there a newer version? ----------------------------------------------------

    def check(self) -> None:
        if self._stage:
            return
        self._stage = "checking"
        self.fetcher.read_notice(self.source.url)

    def _heard(self, data, problem: str) -> None:
        self._stage = ""
        if data is None:
            self.failed.emit(problem)
            return
        try:
            self.latest = release.parse_release(data, self.source.rehearsal)
        except UpdateError as trouble:
            self.failed.emit(str(trouble))
            return
        newer = release.is_newer(self.latest.version, self.current)
        self.checked.emit(self.latest if newer else None)

    def blocked(self) -> str | None:
        """Why this copy of Bookwyrm cannot fetch and install the newest release by
        itself, or None if it can. (The release can still be downloaded by hand.)"""
        if self.latest is None:
            return "Check for updates first."
        if self.backend is None:
            return ("On this system Bookwyrm cannot install an update by itself. "
                    "Download the new version from its page.")
        if self._asset() is None:
            return "This release has no download for this kind of computer."
        return self.backend.why_not(self.bundle)

    def _asset(self):
        return release.asset_for(self.latest, self.backend.SYSTEM)

    # -- fetching and proving the new version -------------------------------------------

    def prepare(self) -> None:
        if self._stage or self.is_ready:
            return
        reason = self.blocked()
        if reason:
            self.failed.emit(reason)
            return
        asset = self._asset()
        try:
            shutil.rmtree(self.staging, ignore_errors=True)
            self.staging.mkdir(parents=True, exist_ok=True)
            free = shutil.disk_usage(self.staging).free
        except OSError as problem:
            self.failed.emit(f"There is nowhere to keep the download: {problem}")
            return
        if free < asset.size * 4 + (64 << 20):
            self.failed.emit(
                f"There is not enough free disk space to update: about "
                f"{release.megabytes(asset.size * 4)} is needed for a short while.")
            return
        self._stage = "downloading"
        self.progressed.emit("Downloading", 0, asset.size)
        self.fetcher.download(asset.url, str(self.staging / ARCHIVE), asset.size)

    def _download_progressed(self, done: int, total: int) -> None:
        self.progressed.emit("Downloading", done, total)

    def _downloaded(self, problem: str) -> None:
        if self._stage != "downloading":
            return
        if problem:
            self._stop("" if problem == CANCELLED else problem)
            return
        asset = self._asset()
        archive = self.staging / ARCHIVE
        self.progressed.emit("Checking the download", 0, 0)
        try:
            intact = (archive.stat().st_size == asset.size
                      and release.file_sha256(archive) == asset.sha256)
        except OSError:
            intact = False
        if not intact:
            self._stop("The download did not match the checksum the release gives for it, "
                       "so it was thrown away. Try again; if it keeps happening, the "
                       "release itself may be at fault.")
            return
        self._stage = "unpacking"
        self.progressed.emit("Unpacking", 0, 0)
        target = self.staging / UNPACKED
        shutil.rmtree(target, ignore_errors=True)
        target.mkdir(parents=True)
        command = self.backend.unpack_command(archive, target)
        self._run(command[0], command[1:], 300)

    def _run(self, program: str, arguments: list[str], seconds: int) -> None:
        process = QProcess(self)
        environment = QProcessEnvironment.systemEnvironment()
        environment.remove(release.FEED_VARIABLE)  # the new version is not part of a rehearsal
        process.setProcessEnvironment(environment)
        process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        process.finished.connect(self._step_done)
        process.errorOccurred.connect(self._step_broke)
        self._process = process
        self._watchdog.start(seconds * 1000)
        process.start(program, arguments)

    def _took_too_long(self) -> None:
        self._stop("A step of the update took far too long and was stopped.")

    def _step_broke(self, error) -> None:
        if self.sender() is self._process and error == QProcess.ProcessError.FailedToStart:
            self._stop("A step of the update could not be started.")

    def _step_done(self, code: int, _status=None) -> None:
        process = self.sender()
        if process is None or process is not self._process:
            return  # a step that was stopped, reporting late
        self._process = None
        self._watchdog.stop()
        process.deleteLater()
        stage = self._stage
        try:
            if stage == "unpacking":
                if code != 0:
                    raise UpdateError("The download could not be unpacked.")
                (self.staging / ARCHIVE).unlink(missing_ok=True)
                self._new_bundle = self.backend.find_app(self.staging / UNPACKED)
                self._stage = "version"
                self.progressed.emit("Trying the new version", 0, 0)
                self._run(str(self.backend.executable_of(self._new_bundle)),
                          ["--version", str(self.staging / "version.txt")], 90)
            elif stage == "version":
                told = self._read("version.txt")
                if code != 0 or told != self.latest.version:
                    raise UpdateError(
                        "The downloaded program did not start properly on this computer"
                        + (f" (it calls itself version {told})." if told else "."))
                self._stage = "selftest"
                self._run(str(self.backend.executable_of(self._new_bundle)),
                          ["--self-test", str(self.staging / "selftest.txt")], 300)
            elif stage == "selftest":
                if code != 0 or "RESULT: all" not in self._read("selftest.txt"):
                    raise UpdateError(
                        "The new version did not pass its own checks on this computer, so it "
                        "was not installed. You are still on version "
                        f"{self.current}.")
                self._stage = ""
                self.is_ready = True
                self.ready.emit()
        except UpdateError as trouble:
            self._stop(str(trouble))

    def _read(self, name: str) -> str:
        try:
            return (self.staging / name).read_text("utf-8", errors="replace").strip()
        except OSError:
            return ""

    def _stop(self, problem: str) -> None:
        """Abandon the update under way and clear up. '' means the user cancelled."""
        self._stage = ""
        self.is_ready = False
        self._watchdog.stop()
        process, self._process = self._process, None
        if process is not None:
            process.kill()
            process.deleteLater()
        self._new_bundle = None
        shutil.rmtree(self.staging, ignore_errors=True)
        if problem:
            self.failed.emit(problem)

    def cancel(self) -> None:
        if self._stage == "checking":
            self._stage = ""
            self.fetcher.cancel()
        elif self._stage == "downloading":
            self.fetcher.cancel()  # _downloaded() then clears up
        elif self._stage:
            self._stop("")

    def busy(self) -> bool:
        return bool(self._stage)

    # -- handing over ---------------------------------------------------------------------

    def install(self, relaunch: bool = True, pid: int | None = None) -> None:
        """Start the swap. It waits for this process (or `pid`) to end, so the caller must
        now quit. Raises UpdateError if the swap could not be started (nothing has
        changed then)."""
        if not self.is_ready or self._new_bundle is None or self.bundle is None:
            raise UpdateError("The new version is not ready to install.")
        self._swapper = self.backend.start_swap(self.bundle, self._new_bundle, self.staging,
                                                relaunch, pid)

    def call_off(self) -> None:
        """Stop a swap that is waiting for this process to end (the user chose not to quit)."""
        swapper, self._swapper = self._swapper, None
        if swapper is not None and swapper.poll() is None:
            swapper.terminate()


def take_outcome(staging=None) -> tuple[bool, str] | None:
    """After a restart: what the update left to say, once. Also tells a waiting swap that
    this version came up."""
    staging = Path(staging) if staging else default_staging()
    outcome = notes.take_outcome(staging)
    notes.mark_started(staging)
    if outcome is None:  # not just updated: tidy anything an earlier update left behind
        windows.clear_leftovers(windows.installed_path())
    for leftover in (ARCHIVE, UNPACKED):  # a download that was fetched but never installed
        path = staging / leftover
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)
    return outcome


def run_headless(install: bool, relaunch: bool, report_path: str | None = None) -> int:
    """``--check-updates`` and ``--update``: the same steps with no window. Returns 0 when
    all went well (including "nothing newer"), 1 otherwise."""
    from PySide6.QtCore import QCoreApplication

    app = QCoreApplication.instance() or QCoreApplication(sys.argv[:1])
    app.setOrganizationName("Bookwyrm")
    app.setApplicationName("Bookwyrm")
    lines: list[str] = []
    result = {"code": 1}

    def say(text: str) -> None:
        lines.append(text)
        print(text, flush=True)

    def finish(code: int) -> None:
        result["code"] = code
        app.quit()

    updater = Updater()
    last = {"doing": ""}

    def progressed(doing: str, _done: int, _total: int) -> None:
        if doing != last["doing"]:
            last["doing"] = doing
            say(f"{doing}…")

    def checked(found) -> None:
        if found is None:
            say(f"Bookwyrm {updater.current} is the newest version.")
            finish(0)
            return
        say(f"Bookwyrm {found.version} is available (this is {updater.current}).")
        if not install:
            finish(0)
            return
        reason = updater.blocked()
        if reason:
            say(reason)
            finish(1)
            return
        updater.prepare()

    def ready() -> None:
        try:
            updater.install(relaunch)
        except UpdateError as trouble:
            say(str(trouble))
            finish(1)
            return
        say(f"Bookwyrm {updater.latest.version} is being put in place.")
        finish(0)

    def failed(problem: str) -> None:
        say(problem)
        finish(1)

    updater.checked.connect(checked)
    updater.progressed.connect(progressed)
    updater.ready.connect(ready)
    updater.failed.connect(failed)
    say(f"Bookwyrm {updater.current}: checking for updates"
        + (" (rehearsal feed)" if updater.source.rehearsal else ""))
    QTimer.singleShot(0, updater.check)
    app.exec()
    if report_path:
        try:
            Path(report_path).write_text("\n".join(lines) + "\n", "utf-8")
        except OSError:
            pass
    os.environ.pop(release.FEED_VARIABLE, None)
    return result["code"]
