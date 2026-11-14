"""Find in the open tome."""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import QHBoxLayout, QLabel, QLineEdit, QToolButton, QWidget

from bookwyrm.ui.keys import hint
from bookwyrm.ui.theme import icons


class FindBar(QWidget):
    searchRequested = Signal(str)  # debounced; '' clears
    nextRequested = Signal()
    previousRequested = Signal()
    closed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("readerToolbar")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        self.field = QLineEdit(self)
        self.field.setPlaceholderText("Find in this tome…")
        self.field.setClearButtonEnabled(True)
        self.field.addAction(icons.icon("magnifying-glass"),
                             QLineEdit.ActionPosition.LeadingPosition)
        self.field.setMinimumWidth(260)
        self.status = QLabel("", self)
        self.status.setObjectName("subheading")
        self.status.setMinimumWidth(96)
        self._previous = self._button(
            "‹", f"Previous match ({hint(QKeySequence.StandardKey.FindPrevious)})",
            self.previousRequested)
        self._next = self._button(
            "›", f"Next match ({hint(QKeySequence.StandardKey.FindNext)})", self.nextRequested)
        done = self._button("Done", "Close the find bar (Esc)", self.close_bar)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 5, 10, 5)
        layout.setSpacing(6)
        layout.addWidget(self.field)
        layout.addWidget(self._previous)
        layout.addWidget(self._next)
        layout.addWidget(self.status)
        layout.addStretch(1)
        layout.addWidget(done)

        self._timer = QTimer(self, singleShot=True, interval=250)
        self._timer.timeout.connect(lambda: self.searchRequested.emit(self.text()))
        self.field.textChanged.connect(lambda _: self._timer.start(250))
        self.field.returnPressed.connect(self._on_return)
        self.hide()

    def _button(self, text: str, tip: str, slot) -> QToolButton:
        button = QToolButton(self)
        button.setText(text)
        button.setToolTip(tip)
        button.clicked.connect(slot)
        return button

    def text(self) -> str:
        return self.field.text().strip()

    def open_bar(self, text: str | None = None) -> None:
        self.show()
        if text is not None and text != self.field.text():
            self.field.setText(text)
            self._timer.stop()
            self.searchRequested.emit(self.text())
        self.field.setFocus()
        self.field.selectAll()

    def close_bar(self) -> None:
        if self.isVisible():
            self.hide()
            self._timer.stop()
            self.closed.emit()

    def set_status(self, text: str) -> None:
        self.status.setText(text)

    def _on_return(self) -> None:
        if self._timer.isActive():  # typed and pressed Return straight away
            self._timer.stop()
            self.searchRequested.emit(self.text())
        else:
            self.nextRequested.emit()

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.close_bar()
        else:
            super().keyPressEvent(event)
