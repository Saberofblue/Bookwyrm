"""The Scriptorium: the reader pane's editing tools.

A `Scriptorium` owns the editing toolbar of one ReaderPane and turns the page
view's gestures (a dragged box, a clicked spot, a moved object) into undoable
edits sent to the document host.
"""

from __future__ import annotations

from PySide6.QtCore import QObject, QRectF, Qt, Signal
from PySide6.QtGui import QAction, QActionGroup, QColor, QIcon, QKeySequence, QPixmap
from PySide6.QtWidgets import QFileDialog, QInputDialog, QMenu, QToolBar, QToolButton

from bookwyrm.db.repo import LibraryError
from bookwyrm.ui.editor.dialogs import (
    ContentsEditor,
    PageOrganizer,
    PropertiesDialog,
    extract_destination,
)
from bookwyrm.ui.keys import hint
from bookwyrm.ui.library import dialogs as library_dialogs
from bookwyrm.ui.reader.page_view import PageView
from bookwyrm.ui.theme import icons

# tool id, label, tooltip
TOOLS = (
    ("select", "Read", "Select and copy text; no editing"),
    ("pointer", "Pointer", "Pick up, move, edit or delete notes and shapes"),
    None,
    ("highlight", "Highlight", "Drag over text to highlight it"),
    ("underline", "Underline", "Drag over text to underline it"),
    ("strikeout", "Strike", "Drag over text to strike it through"),
    ("note", "Note", "Click a spot to leave a sticky note"),
    ("textbox", "Text Box", "Drag a box, then type into it"),
    ("ink", "Draw", "Draw freehand"),
    ("rect", "Box", "Drag to draw a rectangle"),
    ("ellipse", "Oval", "Drag to draw an oval"),
    ("line", "Line", "Drag to draw a line"),
    ("arrow", "Arrow", "Drag to draw an arrow"),
    None,
    ("form", "Fill Form", "Click a field to fill it in (Tab moves to the next)"),
    None,
    ("edit_text", "Edit Text", "Click a line of text to change its wording"),
    ("add_text", "Add Text", "Click where new text should start, then type"),
    ("picture", "Pictures", "Pick up, move, resize, replace or delete pictures"),
    ("add_image", "Add Picture", "Drag a box, then choose a picture to place in it"),
    ("erase", "Erase", "Drag a box over what should go"),
)
PICTURE_FILES = "Pictures (*.png *.jpg *.jpeg *.gif *.bmp *.tif *.tiff *.webp)"
MARKUP_TOOLS = ("highlight", "underline", "strikeout")
COLORS = (  # name, pen colour, highlight colour
    ("Crimson", "#c8202a", "#ff8f8f"),
    ("Amber", "#d98a12", "#ffd84a"),
    ("Moss", "#2f8a3c", "#9be08a"),
    ("Frost", "#2a7fb8", "#8fd3f4"),
    ("Violet", "#7a3fb0", "#d3a6f5"),
    ("Ink", "#1a1a1a", "#c9c9c9"),
)


def _rgb(hex_color: str) -> tuple[float, float, float]:
    color = QColor(hex_color)
    return (color.redF(), color.greenF(), color.blueF())


def _swatch(hex_color: str) -> QIcon:
    pixmap = QPixmap(28, 28)
    pixmap.fill(QColor(hex_color))
    pixmap.setDevicePixelRatio(2.0)
    return QIcon(pixmap)


def _box(rect: QRectF) -> tuple[float, float, float, float]:
    return (rect.left(), rect.top(), rect.right(), rect.bottom())


class Scriptorium(QObject):
    notice = Signal(str)
    stateChanged = Signal()  # undo / redo / dirty changed
    extracted = Signal(str)  # path of a new PDF holding pages copied out of this tome
    retitled = Signal(str)  # the tome was given a new library title

    def __init__(self, pane, session, view: PageView):
        super().__init__(pane)
        self.pane = pane
        self.session = session
        self.view = view
        self._requests: set[int] = set()  # edits this pane asked for
        self._object_requests: dict[int, int] = {}  # req_id -> page (annotation lists)
        self._field_requests: dict[int, int] = {}  # req_id -> page (form field lists)
        self._annots: dict[int, list] = {}
        self._fields: dict[int, list] = {}
        self._pen, self._marker = COLORS[0][1], COLORS[1][2]
        self._focus_field: tuple[int, int] | None = None  # (page, xref) to edit once reloaded
        self._extract_requests: dict[int, tuple[str, int]] = {}  # req_id -> (path, pages)
        self._tool = "select"  # the Scriptorium's tool; several share one page-view tool
        self._line_requests: dict[int, int] = {}  # req_id -> page (text lines)
        self._image_requests: dict[int, int] = {}  # req_id -> page (pictures)
        self._use_requests: dict[int, tuple] = {}  # req_id -> the picture edit awaiting it
        self._lines: dict[int, list] = {}
        self._images: dict[int, list] = {}
        self._pending_click: tuple | None = None  # an Edit Text click awaiting its lines
        self._font_notes: set[int] = set()  # replace_text requests to report on
        self._organizer: PageOrganizer | None = None

        self.toolbar = QToolBar(pane)
        self.toolbar.setObjectName("scriptorium")
        self.toolbar.setMovable(False)
        self.toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self._undo_keys = hint(QKeySequence.StandardKey.Undo)
        self._redo_keys = hint(QKeySequence.StandardKey.Redo)
        self.undo_action = self._action("Undo", "Nothing to undo", self.undo)
        self.redo_action = self._action("Redo", "Nothing to redo", self.redo)
        self.save_action = self._action(
            "Save", f"Save changes to this tome ({hint(QKeySequence.StandardKey.Save)}). "
            "Saving clears the undo history; the original stays in the archive.",
            lambda: pane.save())
        self.toolbar.addSeparator()
        self.tool_group = QActionGroup(self)
        self.tool_actions: dict[str, QAction] = {}
        for entry in TOOLS:
            if entry is None:
                self.toolbar.addSeparator()
                continue
            tool, label, tip = entry
            action = QAction(label, self)
            action.setToolTip(tip)
            action.setCheckable(True)
            action.triggered.connect(lambda _checked=False, t=tool: self.choose_tool(t))
            self.tool_group.addAction(action)
            self.toolbar.addAction(action)
            self.tool_actions[tool] = action
        self.tool_actions["select"].setChecked(True)
        self.toolbar.addSeparator()
        self.color_button = QToolButton(pane)
        self.color_button.setToolTip("Colour for notes, drawing and highlights")
        self.color_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(self.color_button)
        for name, pen, marker in COLORS:
            menu.addAction(_swatch(pen), name, lambda p=pen, m=marker: self.set_color(p, m))
        self.color_button.setMenu(menu)
        self.toolbar.addWidget(self.color_button)
        self.set_color(self._pen, COLORS[1][2])
        self.toolbar.addSeparator()
        self._action("Pages…", "Reorder, rotate, delete, insert or extract pages",
                     self.open_organizer)
        self._action("Contents…", "Edit the table of contents", self.edit_contents)
        self._action("Properties…", "Edit the title, author and keywords stored in the PDF",
                     self.edit_properties)
        self._action("Restore…", "Bring this tome back exactly as it was first added",
                     self.restore_original)
        self.toolbar.hide()

        view.markupDrawn.connect(self._on_markup)
        view.boxDrawn.connect(self._on_box)
        view.lineDrawn.connect(self._on_line)
        view.inkDrawn.connect(self._on_ink)
        view.pageClicked.connect(self._on_click)
        view.objectMoved.connect(self._on_object_moved)
        view.objectActivated.connect(self._on_object_activated)
        view.objectDeleteRequested.connect(self._on_object_delete)
        view.inlineTabbed.connect(self._next_field)
        view.currentPageChanged.connect(lambda _page: self._load_overlays())
        view.loaded.connect(self._load_overlays)
        session.edited.connect(self._on_edited)
        session.saved.connect(lambda *_: self._sync())
        session.failed.connect(self._on_failed)
        session.reply.connect(self._on_reply)
        self._sync()

    def _action(self, label: str, tip: str, slot) -> QAction:
        action = QAction(label, self)
        action.setToolTip(tip)
        action.triggered.connect(lambda _checked=False: slot())
        self.toolbar.addAction(action)
        return action

    # -- state ------------------------------------------------------------------

    def _sync(self) -> None:
        state = self.session.edit_state
        self.undo_action.setEnabled(bool(state.get("can_undo")))
        self.redo_action.setEnabled(bool(state.get("can_redo")))
        self.save_action.setEnabled(bool(state.get("dirty")))
        undo, redo = state.get("undo_name") or "", state.get("redo_name") or ""
        self.undo_action.setToolTip(
            f"Undo {undo} ({self._undo_keys})" if undo else "Nothing to undo")
        self.redo_action.setToolTip(
            f"Redo {redo} ({self._redo_keys})" if redo else "Nothing to redo")
        self.stateChanged.emit()

    def set_color(self, pen: str, marker: str) -> None:
        self._pen, self._marker = pen, marker
        self.color_button.setIcon(_swatch(pen))
        self.view.pen_color = QColor(pen)

    def choose_tool(self, tool: str) -> None:
        """Switch tool. A markup tool chosen with text already selected marks it at once."""
        if tool in MARKUP_TOOLS and self.view.tool == "select" and self.view.has_selection():
            self._on_markup(tool, self.view.selection_boxes())
            self.view.clear_selection()
            tool = "select"
        self.tool_actions[tool].setChecked(True)
        if tool != self._tool and "picture" in (tool, self._tool):
            self.view.object_boxes = {}  # the pointer now picks a different kind of thing
        self._tool = tool
        self.view.resizable = tool == "picture"  # pictures get corner handles
        self.view.set_tool("pointer" if tool == "picture" else tool)
        if tool != "form":
            self.view.clear_field_boxes()
        self._load_overlays()

    def leave(self) -> None:
        """Back to plain reading (the toolbar was hidden)."""
        self.choose_tool("select")

    # -- sending edits ------------------------------------------------------------

    def edit(self, name: str, **args) -> int:
        request = self.session.edit(name, **args)
        self._requests.add(request)
        return request

    def undo(self) -> None:
        if self.session.edit_state.get("can_undo"):
            self._requests.add(self.session.undo())

    def redo(self) -> None:
        if self.session.edit_state.get("can_redo"):
            self._requests.add(self.session.redo())

    def _on_edited(self, request: int, state: dict, result) -> None:
        self._requests.discard(request)
        if request in self._font_notes:
            self._font_notes.discard(request)
            if isinstance(result, dict) and result.get("original_font") is False:
                self.notice.emit("The tome's own font lacks some of those letters, so the "
                                 "closest standard font was used for that line.")
        self.view.apply_edit(state)
        changed = state.get("changed")
        for cache in (self._annots, self._fields, self._lines, self._images):
            if changed is None or state.get("structural"):
                cache.clear()
            else:
                for page in changed:
                    cache.pop(page, None)
        self._sync()
        self._load_overlays()

    def _on_failed(self, request: int, message: str) -> None:
        if request in self._requests:
            self._requests.discard(request)
            self.notice.emit(f"That change could not be made: {message}")
            self._sync()

    # -- gestures from the page view -------------------------------------------------

    def _on_markup(self, tool: str, boxes: dict) -> None:
        color = _rgb(self._marker if tool == "highlight" else self._pen)
        for page, rects in boxes.items():
            self.edit("markup", page=page, kind=tool, rects=rects, color=color)

    def _on_box(self, tool: str, page: int, box: QRectF, _where) -> None:
        if tool in ("rect", "ellipse"):
            self.edit("shape", page=page, kind=tool, points=_box(box), color=_rgb(self._pen))
            self.view.clear_marquee()
        elif tool == "textbox":
            def done(text: str) -> None:
                self.view.clear_marquee()
                if text.strip():
                    self.edit("textbox", page=page, rect=_box(box), text=text,
                              color=_rgb(self._pen))
            self.view.edit_inline(page, box, "", done, multiline=True, font_points=11)
        elif tool == "add_image":
            path, _ = QFileDialog.getOpenFileName(self.pane, "Choose a picture", "",
                                                  PICTURE_FILES)
            self.view.clear_marquee()
            if path:
                self.edit("insert_image", page=page, rect=_box(box), path=path)
        elif tool == "erase":
            self._erase_menu(page, box, _where)

    def _erase_menu(self, page: int, box: QRectF, where) -> None:
        menu = QMenu(self.pane)
        for label, mode in (("Erase Text and Line Art", "text"),
                            ("Erase Everything, Pictures Too", "all"),
                            ("Cover with White", "cover")):
            menu.addAction(label, lambda m=mode: self.edit("erase", page=page, rect=_box(box),
                                                           mode=m))
        menu.exec(where)
        self.view.clear_marquee()

    def _on_line(self, tool: str, page: int, a, b) -> None:
        self.edit("shape", page=page, kind=tool, points=(a.x(), a.y(), b.x(), b.y()),
                  color=_rgb(self._pen))

    def _on_ink(self, page: int, points: list) -> None:
        self.edit("ink", page=page, strokes=[points], color=_rgb(self._pen))

    def _on_click(self, tool: str, page: int, point, _where) -> None:
        if tool == "note":
            text, ok = QInputDialog.getMultiLineText(self.pane, "Note", "What should it say?")
            if ok and text.strip():
                self.edit("note", page=page, point=(point.x(), point.y()), text=text.strip(),
                          color=_rgb(self._pen))
        elif tool == "form":
            self._click_field(page, point)
        elif tool == "edit_text":
            self._click_text(page, point)
        elif tool == "add_text":
            def done(text: str) -> None:
                if text.strip():
                    self.edit("add_text", page=page, point=(point.x(), point.y()), text=text,
                              size=11, color=_rgb(self._pen))
            self.view.edit_inline(page, QRectF(point.x(), point.y() - 13, 280, 17), "", done,
                                  font_points=11)

    # -- text already on the page ---------------------------------------------------------

    def _click_text(self, page: int, point) -> None:
        lines = self._lines.get(page)
        if lines is None:  # still on its way from the host: finish when it arrives
            self._pending_click = (page, point)
            self._load_overlays()
            return
        line = next((ln for ln in lines
                     if ln["rect"][0] - 2 <= point.x() <= ln["rect"][2] + 2
                     and ln["rect"][1] - 1 <= point.y() <= ln["rect"][3] + 1), None)
        if line is None:
            self.notice.emit("Click directly on a line of text. (Words that are part of a "
                             "picture cannot be edited, only covered.)")
            return
        if not line["upright"]:
            self.notice.emit("Rotated text cannot be edited in place.")
            return
        x0, y0, x1, y1 = line["rect"]

        def done(text: str) -> None:
            if text != line["text"]:
                request = self.edit(
                    "replace_text", page=page, rect=line["rect"], text=text,
                    font=line["font"], size=line["size"], flags=line["flags"],
                    color=line["color"], origin=line["origin"])
                self._font_notes.add(request)
        self.view.edit_inline(page, QRectF(x0, y0, max(x1 - x0 + 60, 140), y1 - y0),
                              line["text"], done, font_points=line["size"])

    # -- pictures ----------------------------------------------------------------------------

    def _picture(self, page: int, index: int) -> dict | None:
        images = self._images.get(page, ())
        return images[index] if 0 <= index < len(images) else None

    def _picture_edit(self, name: str, page: int, xref: int, **args) -> None:
        """Run a picture edit after checking whether other pages show the same picture."""
        request = self.session.query("image_uses", xref)
        self._use_requests[request] = (name, page, xref, args)

    def _picture_edit_checked(self, uses: int, name: str, page: int, xref: int,
                              args: dict) -> None:
        if uses > 1 and not library_dialogs.confirm(
                self.pane, "Picture Used on Several Pages",
                f"This picture is stored once and shown on {uses} pages. Changing it here "
                "changes it on all of them.", "Change It Everywhere"):
            return
        self.edit(name, page=page, xref=xref, **args)

    def _picture_menu(self, page: int, picture: dict) -> None:
        from PySide6.QtGui import QCursor
        menu = QMenu(self.pane)
        menu.addAction("Replace Picture…", lambda: self._replace_picture(page, picture))
        menu.addAction("Resize…", lambda: self._resize_picture(page, picture))
        menu.addSeparator()
        menu.addAction("Delete Picture",
                       lambda: self._picture_edit("delete_image", page, picture["xref"]))
        menu.exec(QCursor.pos())

    def _replace_picture(self, page: int, picture: dict) -> None:
        path, _ = QFileDialog.getOpenFileName(self.pane, "Choose the new picture", "",
                                              PICTURE_FILES)
        if path:
            self._picture_edit("replace_image", page, picture["xref"], path=path)

    def _resize_picture(self, page: int, picture: dict) -> None:
        percent, ok = QInputDialog.getInt(self.pane, "Resize Picture", "Size (percent):",
                                          100, 10, 500, 5)
        if not ok or percent == 100:
            return
        x0, y0, x1, y1 = picture["rect"]
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        half_w, half_h = (x1 - x0) * percent / 200, (y1 - y0) * percent / 200
        self._picture_edit("move_image", page, picture["xref"],
                           rect=(cx - half_w, cy - half_h, cx + half_w, cy + half_h))

    # -- pointer: notes and shapes as objects -----------------------------------------

    def _load_overlays(self) -> None:
        """Fetch what the current tool needs to see on the visible pages."""
        tool = self._tool
        if tool == "pointer":
            for page in self.view.visible_pages():
                if page not in self._annots and page not in self._object_requests.values():
                    self._object_requests[self.session.annots(page)] = page
        elif tool == "picture":
            for page in self.view.visible_pages():
                if page not in self._images and page not in self._image_requests.values():
                    self._image_requests[self.session.query("images", page)] = page
        elif tool == "edit_text":
            for page in self.view.visible_pages():
                if page not in self._lines and page not in self._line_requests.values():
                    self._line_requests[self.session.query("text_lines", page)] = page
        elif tool == "form":
            for page in self.view.visible_pages():
                if page not in self._fields and page not in self._field_requests.values():
                    self._field_requests[self.session.widgets(page)] = page

    def _on_reply(self, request: int, payload) -> None:
        if request in self._object_requests:
            page = self._object_requests.pop(request)
            self._annots[page] = payload
            if self._tool == "pointer":
                self.view.set_object_boxes(
                    page, [(a["xref"], a["rect"], a["movable"]) for a in payload])
        elif request in self._image_requests:
            page = self._image_requests.pop(request)
            self._images[page] = payload
            if self._tool == "picture":  # pictures are picked by their position in the list
                self.view.set_object_boxes(
                    page, [(index, p["rect"], True) for index, p in enumerate(payload)])
        elif request in self._line_requests:
            page = self._line_requests.pop(request)
            self._lines[page] = payload
            if self._pending_click and self._pending_click[0] == page:
                click, self._pending_click = self._pending_click, None
                if self._tool == "edit_text":
                    self._click_text(*click)
        elif request in self._use_requests:
            name, page, xref, args = self._use_requests.pop(request)
            self._picture_edit_checked(int(payload), name, page, xref, args)
        elif request in self._extract_requests:
            path, count = self._extract_requests.pop(request)
            self.extracted.emit(path)
            noun = "page" if count == 1 else "pages"
            self.notice.emit(f"Copied {count} {noun} into a new tome on the Returns Cart.")
        elif request in self._field_requests:
            page = self._field_requests.pop(request)
            self._fields[page] = payload
            if self._tool == "form":
                self.view.set_field_boxes(
                    page, [f["rect"] for f in payload if not f["read_only"]])
            if self._focus_field and self._focus_field[0] == page:
                target, self._focus_field = self._focus_field, None
                field = next((f for f in payload if f["xref"] == target[1]), None)
                if field is not None:
                    self._edit_field(page, field)

    def _annot(self, page: int, xref: int) -> dict | None:
        return next((a for a in self._annots.get(page, ()) if a["xref"] == xref), None)

    def _on_object_moved(self, page: int, xref: int, box: QRectF) -> None:
        if self._tool == "picture":
            picture = self._picture(page, xref)
            if picture is not None:
                self._picture_edit("move_image", page, picture["xref"], rect=_box(box))
            return
        self.edit("move_annot", page=page, xref=xref, rect=_box(box))

    def _on_object_delete(self, page: int, xref: int) -> None:
        self.view.select_object(None)
        if self._tool == "picture":
            picture = self._picture(page, xref)
            if picture is not None:
                self._picture_edit("delete_image", page, picture["xref"])
            return
        self.edit("delete_annot", page=page, xref=xref)

    def _on_object_activated(self, page: int, xref: int) -> None:
        if self._tool == "picture":
            picture = self._picture(page, xref)
            if picture is not None:
                self._picture_menu(page, picture)
            return
        annot = self._annot(page, xref)
        if annot is None or annot["kind"] not in ("Text", "FreeText"):
            return
        text, ok = QInputDialog.getMultiLineText(self.pane, "Edit Note", "Text:", annot["text"])
        if ok:
            self.edit("edit_annot", page=page, xref=xref, text=text)

    # -- pages, contents, properties, restore ---------------------------------------------

    def open_organizer(self) -> None:
        if self._organizer is None or not self._organizer.isVisible():
            self._organizer = PageOrganizer(self.pane, self.pane.tome_title)
            self._organizer.extractRequested.connect(self.extract_pages)
        self._organizer.show()
        self._organizer.raise_()
        self._organizer.activateWindow()

    def extract_pages(self, pages: list) -> None:
        """Copy pages into a new PDF; the main window then adds it to the library."""
        if not pages:
            return
        path = extract_destination(self.pane.tome_title, pages)
        self._extract_requests[self.session.extract(pages, path)] = (path, len(pages))

    def edit_contents(self) -> None:
        info = self.session.info or {}
        dialog = ContentsEditor(info.get("toc", []), info.get("page_count", 0),
                                self.view.current_page(), self.pane)
        if dialog.exec():
            self.edit("set_toc", toc=dialog.toc())

    def edit_properties(self) -> None:
        info = self.session.info or {}
        dialog = PropertiesDialog(info.get("metadata", {}), self.pane)
        if not dialog.exec():
            return
        values = dialog.values()
        self.edit("set_metadata", fields=values)
        library, book_id = self.pane.library, self.pane.book_id
        if dialog.rename.isChecked() and library is not None and values["title"]:
            try:
                library.update_book(book_id, title=values["title"], author=values["author"])
                self.retitled.emit(values["title"])
            except LibraryError as problem:
                self.notice.emit(str(problem))

    def restore_original(self) -> None:
        library, book_id = self.pane.library, self.pane.book_id
        if library is None:
            return
        if not library.archive_file(book_id).exists():
            self.notice.emit("This tome has not been changed since it was added; "
                             "there is nothing to restore.")
            return
        if library_dialogs.confirm(
                self.pane, "Restore Original",
                f"Restore “{self.pane.tome_title}” to exactly how it was when you first added "
                "it?\n\nEvery saved and unsaved change is discarded. Ribbons and pins keep "
                "their page numbers, which may point at different pages if you had "
                "rearranged the tome.", "Restore Original"):
            self.pane.restore_original()

    # -- forms ---------------------------------------------------------------------------

    def _click_field(self, page: int, point) -> None:
        for field in self._fields.get(page, ()):
            x0, y0, x1, y1 = field["rect"]
            if x0 - 1 <= point.x() <= x1 + 1 and y0 - 1 <= point.y() <= y1 + 1:
                self._edit_field(page, field)
                return

    def _edit_field(self, page: int, field: dict) -> None:
        if field["read_only"]:
            self.notice.emit("That field is read-only.")
            return
        kind, xref, value = field["kind"], field["xref"], field["value"]
        if kind == "CheckBox":
            ticked = value not in (False, None, "", "Off")
            self.edit("fill_field", page=page, xref=xref, value=not ticked)
        elif kind == "RadioButton":
            self.edit("fill_field", page=page, xref=xref, value=True)
        elif kind in ("ComboBox", "ListBox") and field["choices"]:
            options = [c[1] if isinstance(c, list | tuple) else str(c) for c in field["choices"]]
            current = options.index(value) if value in options else 0
            choice, ok = QInputDialog.getItem(self.pane, field["name"] or "Choose",
                                              "Value:", options, current, False)
            if ok:
                self.edit("fill_field", page=page, xref=xref, value=choice)
        elif kind == "Text":
            x0, y0, x1, y1 = field["rect"]

            def done(text: str) -> None:
                if text != (value or ""):
                    self.edit("fill_field", page=page, xref=xref, value=text)
            self.view.edit_inline(page, QRectF(x0, y0, x1 - x0, y1 - y0), str(value or ""),
                                  done, multiline=field["multiline"])
            self._editing_field = (page, xref)
            if field["calculated"]:
                self.notice.emit("This field is meant to be calculated by the form. Bookwyrm "
                                 "cannot run form scripts, so type the value yourself.")

    def _next_field(self, backwards: bool) -> None:
        """Tab out of a text field: open the next (or previous) one on the page."""
        current = getattr(self, "_editing_field", None)
        if self._tool != "form" or current is None:
            return
        page, xref = current
        fields = [f for f in self._fields.get(page, ())
                  if f["kind"] == "Text" and not f["read_only"]]
        index = next((i for i, f in enumerate(fields) if f["xref"] == xref), None)
        if index is None or not fields:
            return
        target = fields[(index + (-1 if backwards else 1)) % len(fields)]
        # The commit just queued an edit whose reply reloads this page's fields;
        # open the next editor once they are back so it shows current values.
        if self._requests:
            self._focus_field = (page, target["xref"])
        else:
            self._edit_field(page, target)


def scriptorium_icon() -> QIcon:
    return icons.icon("quill-ink")
