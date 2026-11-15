"""The Check for Updates window: asks only when opened, and updates only when told to."""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
)

from bookwyrm.updates import release
from bookwyrm.updates.updater import Updater


class UpdateDialog(QDialog):
    restartRequested = Signal()  # the new version is ready: Bookwyrm should now close

    def __init__(self, updater: Updater, parent=None):
        super().__init__(parent)
        self.updater = updater
        self.state = ""  # checking | current | available | preparing | ready | failed
        self.setWindowTitle("Check for Updates")
        self.setMinimumWidth(600)
        self.heading = QLabel()
        self.heading.setObjectName("heading")
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.notes = QTextBrowser()  # what the release says about itself
        self.notes.setOpenExternalLinks(True)
        self.notes.setMinimumHeight(260)
        self.bar = QProgressBar()
        self.bar.setTextVisible(False)
        self.primary = QPushButton()
        self.primary.setObjectName("primary")
        self.primary.clicked.connect(self._primary_clicked)
        self.secondary = QPushButton()
        self.secondary.clicked.connect(self._secondary_clicked)
        for button in (self.primary, self.secondary):
            button.setAutoDefault(False)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(self.secondary)
        buttons.addWidget(self.primary)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 18)
        layout.setSpacing(10)
        layout.addWidget(self.heading)
        layout.addWidget(self.status)
        layout.addWidget(self.notes, 1)
        layout.addWidget(self.bar)
        layout.addStretch(0)
        layout.addLayout(buttons)

        updater.checked.connect(self._checked)
        updater.failed.connect(self._failed)
        updater.progressed.connect(self._progressed)
        updater.ready.connect(self._ready)
        if updater.is_ready:  # fetched earlier in this session and still waiting
            self._show_ready()
        else:
            self._show("checking", "Checking for updates…", "Asking GitHub for the newest "
                       "release of Bookwyrm.", secondary="Cancel")
            QTimer.singleShot(0, updater.check)

    # -- what is shown ------------------------------------------------------------------

    def _show(self, state: str, heading: str, status: str, primary: str = "",
              secondary: str = "Close", notes: bool = False, bar: bool = False) -> None:
        self.state = state
        self.heading.setText(heading)
        self.status.setText(status)
        self.primary.setVisible(bool(primary))
        self.primary.setText(primary)
        self.primary.setDefault(bool(primary))
        self.secondary.setText(secondary)
        self.notes.setVisible(notes)
        self.bar.setVisible(bar)
        self.adjustSize()  # the release notes come and go; the window follows

    def _checked(self, found) -> None:
        if self.state != "checking":
            return
        current = self.updater.current
        if found is None:
            self._show("current", "Bookwyrm is up to date",
                       f"You have version {current}, the newest there is.")
            return
        self.notes.setMarkdown(found.notes or "*This release says nothing about itself.*")
        reason = self.updater.blocked()
        self._show(
            "available", f"Bookwyrm {found.version} is available",
            f"You have version {current}." + (f"\n\n{reason}" if reason else ""),
            primary="Open the Download Page" if reason else "Update and Restart",
            secondary="Not Now", notes=True)

    def _progressed(self, doing: str, done: int, total: int) -> None:
        if self.state != "preparing":
            return
        if total > 0:
            self.bar.setRange(0, 1000)
            self.bar.setValue(int(done * 1000 / total))
            doing = (f"{doing}: {release.megabytes(done)} of {release.megabytes(total)}")
        else:
            self.bar.setRange(0, 0)  # busy, with nothing to measure
        self.status.setText(f"{doing}…")

    def _ready(self) -> None:
        if self.state == "preparing":
            self._show_ready()
            self.restartRequested.emit()

    def _show_ready(self) -> None:
        version = self.updater.latest.version if self.updater.latest else "the new version"
        self._show("ready", f"Bookwyrm {version} is ready",
                   "Bookwyrm will close and open again as the new version. If a tome has "
                   "unsaved changes, you will be asked about them first.",
                   primary="Restart Now", secondary="Later")

    def waiting_to_restart(self) -> None:
        """The restart was put off (a tome had unsaved changes and the user stayed)."""
        self._show_ready()

    def _failed(self, problem: str) -> None:
        if self.state in ("checking", "preparing", "ready"):
            was_checking = self.state == "checking"
            self._show("failed", "Could not check for updates" if was_checking
                       else "Bookwyrm was not updated", problem, primary="Try Again")

    def show_failure(self, problem: str) -> None:
        self._show("failed", "Bookwyrm was not updated", problem, primary="Try Again")

    # -- the buttons --------------------------------------------------------------------

    def _primary_clicked(self) -> None:
        if self.state == "available" and self.updater.blocked():
            QDesktopServices.openUrl(QUrl(self.updater.latest.page))
        elif self.state == "available":
            self._show("preparing", f"Getting Bookwyrm {self.updater.latest.version}",
                       "Starting…", secondary="Cancel", bar=True)
            self.bar.setRange(0, 0)
            self.updater.prepare()
        elif self.state == "ready":
            self.restartRequested.emit()
        elif self.state == "failed":
            self._show("checking", "Checking for updates…", "Asking GitHub for the newest "
                       "release of Bookwyrm.", secondary="Cancel")
            self.updater.check()

    def _secondary_clicked(self) -> None:
        if self.state == "preparing":
            self.updater.cancel()
            self._checked_again()
            return
        self.reject()

    def _checked_again(self) -> None:
        self.state = "checking"
        self._checked(self.updater.latest)

    def reject(self) -> None:
        if self.state in ("checking", "preparing"):
            self.updater.cancel()
        super().reject()
