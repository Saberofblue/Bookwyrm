"""The Settings window, and the dialog that shows a library being moved."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QEventLoop, Qt, QThread, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QSlider,
    QVBoxLayout,
)

from bookwyrm import paths
from bookwyrm.db.repo import Library
from bookwyrm.library import relocate
from bookwyrm.ui import prefs, system
from bookwyrm.ui.library import dialogs
from bookwyrm.ui.library.bookcase_view import MAX_COVER_H, MIN_COVER_H

ZOOMS = (("fit_width", "Fit Width"), ("fit_page", "Fit Page"), ("actual", "Actual Size"))
LIGHTS = (("day", "Daylight"), ("candle", "Candlelight"), ("night", "Night"))


def _section(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("sectionLabel")
    label.setContentsMargins(0, 10, 0, 0)
    return label


class SettingsDialog(QDialog):
    libraryChangeRequested = Signal(str, str)  # folder, "move" | "switch"
    coverHeightChanged = Signal(int)
    readingChanged = Signal()  # reading light, side panel or spread preference changed

    def __init__(self, library: Library, parent=None):
        super().__init__(parent)
        self.library = library
        self.setWindowTitle("Settings")
        self.setMinimumWidth(600)
        heading = QLabel("Settings")
        heading.setObjectName("heading")

        # ---- library location ----
        self.location = QLabel(str(library.root))
        self.location.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.location.setWordWrap(True)
        self.change = QPushButton("Change…")
        self.change.setToolTip("Keep the library in a different folder")
        self.change.clicked.connect(self.change_location)
        show = QPushButton(f"Show in {system.FILE_MANAGER}")
        show.clicked.connect(lambda: system.reveal(library.root))
        for button in (self.change, show):  # Return closes the window; it must not press these
            button.setAutoDefault(False)
        location_buttons = QHBoxLayout()
        location_buttons.addWidget(self.change)
        location_buttons.addWidget(show)
        location_buttons.addStretch(1)
        self.location_note = QLabel(
            "Bookwyrm keeps its copies of your PDFs, their covers and its catalogue here.")
        self.location_note.setObjectName("subheading")
        self.location_note.setWordWrap(True)
        if paths.env_override():
            self.change.setEnabled(False)
            self.location_note.setText(
                "This location is set by the BOOKWYRM_LIBRARY environment variable, so it "
                "cannot be changed here.")

        # ---- startup ----
        self.reopen = QCheckBox("Reopen the tomes that were open last time")
        self.reopen.setChecked(prefs.get("startup/reopen"))
        self.reopen.toggled.connect(lambda on: prefs.put("startup/reopen", on))

        # ---- reading ----
        self.zoom = QComboBox()
        for key, label in ZOOMS:
            self.zoom.addItem(label, key)
        self.zoom.setCurrentIndex(max(0, self.zoom.findData(prefs.get("reader/zoom"))))
        self.zoom.activated.connect(lambda _i: prefs.put("reader/zoom", self.zoom.currentData()))
        self.light = QComboBox()
        for key, label in LIGHTS:
            self.light.addItem(label, key)
        self.light.setCurrentIndex(max(0, self.light.findData(prefs.get("reader/mode"))))
        self.light.activated.connect(lambda _i: self._reading("reader/mode",
                                                              self.light.currentData()))
        self.side_panel = QCheckBox("Show the side panel (contents, pages, ribbons)")
        self.side_panel.setChecked(prefs.get("reader/side_panel"))
        self.side_panel.toggled.connect(lambda on: self._reading("reader/side_panel", on))
        self.spread = QCheckBox("Show two pages side by side, like an open book")
        self.spread.setChecked(prefs.get("reader/spread"))
        self.spread.toggled.connect(lambda on: self._reading("reader/spread", on))
        reading = QFormLayout()
        reading.setSpacing(8)
        reading.addRow("Open a tome for the first time at", self.zoom)
        reading.addRow("Reading light", self.light)

        # ---- shelves ----
        self.cover = QSlider(Qt.Orientation.Horizontal)
        self.cover.setRange(MIN_COVER_H, MAX_COVER_H)
        self.cover.setValue(prefs.get("shelves/cover_height"))
        self.cover.valueChanged.connect(self.coverHeightChanged)
        shelves = QFormLayout()
        shelves.addRow("Cover size on the shelves", self.cover)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 18)
        layout.setSpacing(8)
        layout.addWidget(heading)
        layout.addWidget(_section("WHERE YOUR LIBRARY LIVES"))
        layout.addWidget(self.location)
        layout.addLayout(location_buttons)
        layout.addWidget(self.location_note)
        layout.addWidget(_section("WHEN BOOKWYRM STARTS"))
        layout.addWidget(self.reopen)
        layout.addWidget(_section("READING"))
        layout.addLayout(reading)
        layout.addWidget(self.side_panel)
        layout.addWidget(self.spread)
        layout.addWidget(_section("SHELVES"))
        layout.addLayout(shelves)
        layout.addSpacing(8)
        layout.addWidget(buttons)

    def _reading(self, key: str, value) -> None:
        prefs.put(key, value)
        self.readingChanged.emit()

    # -- moving the library -------------------------------------------------------

    def change_location(self) -> None:
        root = self.library.root
        chosen = self._pick_folder(str(root.parent))
        if not chosen:
            return
        target = relocate.resolve_target(Path(chosen))
        if target.resolve() == root.resolve():
            dialogs.complain(self, "That is where your library already is.")
            return
        if system.looks_cloud_synced(target) and not self._confirm(
                "A Synced Folder",
                f"{target}\n\nlooks like it is inside a folder that a cloud service (Dropbox, "
                "OneDrive, iCloud and the like) keeps in sync. If the catalogue is copied "
                "while Bookwyrm is writing to it, it can be damaged.\n\nA folder that is not "
                "synced is safer.", "Use It Anyway"):
            return
        if relocate.is_library(target):
            if self._confirm(
                    "Use That Library",
                    f"There is already a Bookwyrm library at\n{target}\n\nSwitch to it? Your "
                    f"current library stays where it is, at\n{root}", "Switch to It"):
                self.libraryChangeRequested.emit(str(target), "switch")
                self.accept()
            return
        try:
            relocate.check_move(root, target)
        except relocate.MoveError as problem:
            dialogs.complain(self, str(problem))
            return
        choice = self._ask_move_or_empty(target)
        if choice is not None:
            self.libraryChangeRequested.emit(str(target), choice)
            self.accept()

    # The three questions are separate methods so tests can answer them.

    def _pick_folder(self, start: str) -> str:
        return QFileDialog.getExistingDirectory(self, "Choose a folder for your library", start)

    def _confirm(self, title: str, message: str, action: str) -> bool:
        return dialogs.confirm(self, title, message, action)

    def _ask_move_or_empty(self, target: Path) -> str | None:
        """'move', 'switch' (start an empty library there) or None."""
        files, size = relocate.measure(self.library.root)
        tomes = len(self.library.all_books())
        box = QMessageBox(QMessageBox.Icon.Question, "Change Library Folder",
                          f"Keep your library in\n{target}?", parent=self)
        box.setInformativeText(
            f"Your library holds {tomes} tome{'s' if tomes != 1 else ''} "
            f"({relocate.describe_size(size)}).\n\n"
            "Move My Library takes everything there. Start Empty leaves the current "
            "library where it is and begins a new, empty one in the new folder.")
        move = box.addButton("Move My Library", QMessageBox.ButtonRole.AcceptRole)
        empty = box.addButton("Start Empty", QMessageBox.ButtonRole.ActionRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(move)
        box.exec()
        clicked = box.clickedButton()
        return "move" if clicked is move else "switch" if clicked is empty else None


class _Mover(QThread):
    progressed = Signal(int, str)  # thousandths done, file name
    finished_with = Signal(str)  # '' = moved, 'cancelled', or an error for the user

    def __init__(self, old: Path, new: Path, parent=None):
        super().__init__(parent)
        self._old, self._new = old, new
        self.stop = False

    def run(self) -> None:
        def progress(done: int, total: int, name: str) -> None:
            self.progressed.emit(int(done * 1000 / max(1, total)), name)
        try:
            relocate.move_library(self._old, self._new, progress, lambda: self.stop)
            self.finished_with.emit("")
        except relocate.Cancelled:
            self.finished_with.emit("cancelled")
        except relocate.MoveError as problem:
            self.finished_with.emit(str(problem))
        except Exception as problem:  # never leave the window waiting forever
            self.finished_with.emit(f"{type(problem).__name__}: {problem}")


def move_with_progress(old: Path, new: Path, parent=None) -> str:
    """Move a closed library, showing progress. Returns '' on success, 'cancelled',
    or a message explaining why the library stayed where it was."""
    dialog = QProgressDialog("Moving your library…", "Cancel", 0, 1000, parent)
    dialog.setWindowTitle("Moving Library")
    dialog.setWindowModality(Qt.WindowModality.ApplicationModal)
    dialog.setMinimumDuration(400)  # a same-disk move is instant: show nothing
    dialog.setAutoClose(False)
    dialog.setAutoReset(False)
    mover = _Mover(old, new)
    outcome: list[str] = []
    loop = QEventLoop()

    def progressed(thousandths: int, name: str) -> None:
        dialog.setValue(thousandths)
        dialog.setLabelText(f"Moving your library…\n{name}")

    def finished(result: str) -> None:
        outcome.append(result)
        loop.quit()

    def cancel() -> None:
        mover.stop = True
        dialog.setLabelText("Stopping and putting everything back…")

    mover.progressed.connect(progressed)
    mover.finished_with.connect(finished)
    dialog.canceled.connect(cancel)
    mover.start()
    loop.exec()
    mover.wait()
    dialog.close()
    return outcome[0] if outcome else "The move stopped unexpectedly."
