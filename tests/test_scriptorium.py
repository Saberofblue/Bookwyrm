"""The Scriptorium end to end: gestures in the window become saved changes in the file."""

import pymupdf
import pytest
from PySide6.QtCore import QPointF, Qt
from PySide6.QtWidgets import QLineEdit

from bookwyrm.db.repo import Library
from bookwyrm.ui.keys import hint
from bookwyrm.ui.main_window import MainWindow
from conftest import make_pdf

LINE = [(50, 66, 280, 84)]


@pytest.fixture
def library(tmp_path):
    lib = Library(tmp_path / "lib")
    yield lib
    lib.close()


@pytest.fixture
def window(qtbot, library, monkeypatch):
    for target in ("bookwyrm.ui.main_window.QSettings", "bookwyrm.ui.reader.reader_pane.QSettings"):
        monkeypatch.setattr(f"{target}.value", lambda self, key, default=None, **_kw: default)
        monkeypatch.setattr(f"{target}.setValue", lambda self, key, value: None)
    window = MainWindow(library)
    monkeypatch.setattr(window, "_ask_unsaved", lambda title: "discard")
    qtbot.addWidget(window)
    window.resize(1300, 820)
    window.show()
    yield window
    window.close()


def add_tome(library, n=1, maker=make_pdf):
    book = library.add_book(title=f"Tome {n}", author="", filename=f"t{n}.pdf",
                            sha256=f"hash{n}", page_count=6, file_size=1000)
    library.mark_ready(book.id)
    library.book_dir(book.id).mkdir(parents=True)
    maker(library.book_file(book))
    return library.book(book.id)


def open_tome(qtbot, window, book):
    pane = window.open_book(book.id)
    qtbot.waitUntil(pane.view.is_idle, timeout=15000)
    pane.view.set_zoom(1.0)
    qtbot.waitUntil(pane.view.is_idle, timeout=15000)
    return pane


def settled(qtbot, pane):
    qtbot.waitUntil(lambda: not pane.scriptorium._requests, timeout=15000)
    qtbot.waitUntil(pane.view.is_idle, timeout=15000)


def annotations(path, page=0):
    doc = pymupdf.open(path)
    kinds = [a.type[1] for a in doc[page].annots() or ()]
    doc.close()
    return kinds


def test_highlight_undo_redo_save(qtbot, library, window):
    book = add_tome(library)
    path = library.book_file(book)
    original = path.read_bytes()
    pane = open_tome(qtbot, window, book)
    pane.toggle_scriptorium()
    assert not pane.scriptorium.toolbar.isHidden()
    assert not pane.scriptorium.save_action.isEnabled()

    before = pane.view.page_revs.get(0, 0)
    pane.scriptorium._on_markup("highlight", {0: LINE})
    settled(qtbot, pane)
    assert pane.session.dirty and pane.view.page_revs[0] == before + 1
    assert window.left.tabText(1).startswith("• ")
    assert pane.scriptorium.undo_action.toolTip() == f"Undo Mark Text ({hint('Ctrl+Z')})"
    assert annotations(path) == []  # nothing is written until Save

    pane.scriptorium.undo()
    settled(qtbot, pane)
    assert not pane.session.dirty and not window.left.tabText(1).startswith("• ")
    pane.scriptorium.redo()
    settled(qtbot, pane)
    assert pane.session.dirty

    assert pane.save()
    qtbot.waitUntil(lambda: not pane.session.dirty, timeout=15000)
    assert annotations(path) == ["Highlight"]
    assert library.archive_file(book.id).read_bytes() == original
    saved = library.book(book.id)
    assert saved.edited and saved.file_size == path.stat().st_size
    assert not pane.scriptorium.undo_action.isEnabled()  # history starts afresh
    qtbot.waitUntil(lambda: library.book(book.id).indexed, timeout=20000)  # re-read for search
    qtbot.waitUntil(lambda: library.cover_file(book.id).exists(), timeout=15000)
    assert library.find_by_hash("hash1").id == book.id  # still known by its import hash
    assert not pane.save()  # nothing left to save


def test_drawing_tools_use_the_mouse(qtbot, library, window):
    book = add_tome(library)
    pane = open_tome(qtbot, window, book)
    pane.toggle_scriptorium()
    view = pane.view
    item = view._items[0]
    def at(x, y):
        return view.mapFromScene(item.pos() + QPointF(x, y))
    def drag(points):
        qtbot.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=at(*points[0]))
        for point in points[1:]:
            qtbot.mouseMove(view.viewport(), pos=at(*point))
        qtbot.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=at(*points[-1]))

    pane.scriptorium.choose_tool("rect")
    drag([(60, 340), (100, 360), (200, 420)])
    settled(qtbot, pane)
    pane.scriptorium.choose_tool("arrow")
    drag([(60, 440), (100, 450), (220, 470)])
    settled(qtbot, pane)
    pane.scriptorium.choose_tool("ink")
    drag([(60, 500), (80, 520), (120, 505), (160, 530)])
    settled(qtbot, pane)
    pane.scriptorium.choose_tool("highlight")  # dragging over text marks it
    drag([(52, 74), (120, 76), (270, 76)])
    qtbot.waitUntil(lambda: pane.session.edit_state["undo_name"] == "Mark Text", timeout=15000)
    settled(qtbot, pane)
    assert view.tool == "highlight" and not view.has_selection()

    pane.save()
    qtbot.waitUntil(lambda: not pane.session.dirty, timeout=15000)
    assert annotations(library.book_file(book)) == ["Square", "Line", "Ink", "Highlight"]


def test_highlight_button_marks_an_existing_selection(qtbot, library, window):
    book = add_tome(library)
    pane = open_tome(qtbot, window, book)
    pane.toggle_scriptorium()
    pane.view.select_all_on_page(0)
    qtbot.waitUntil(pane.view.has_selection, timeout=5000)
    pane.scriptorium.choose_tool("underline")
    settled(qtbot, pane)
    assert pane.view.tool == "select"  # one-shot: stays in reading mode
    assert pane.scriptorium.tool_actions["select"].isChecked()
    assert pane.session.edit_state["undo_name"] == "Mark Text"


def test_pointer_selects_moves_and_deletes_a_note(qtbot, library, window):
    book = add_tome(library)
    pane = open_tome(qtbot, window, book)
    pane.toggle_scriptorium()
    view, tools = pane.view, pane.scriptorium
    tools.edit("note", page=0, point=(300, 200), text="Ask the players")
    settled(qtbot, pane)
    tools.choose_tool("pointer")
    qtbot.waitUntil(lambda: bool(view.object_boxes.get(0)), timeout=15000)
    xref, box, movable = view.object_boxes[0][0]
    assert movable

    centre = view.mapFromScene(view._items[0].pos() + box.center())
    qtbot.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=centre)
    assert view.selected_object == (0, xref)

    tools._on_object_moved(0, xref, box.translated(40, 60))
    settled(qtbot, pane)
    qtbot.waitUntil(lambda: bool(view.object_boxes.get(0)), timeout=15000)
    assert view.object_boxes[0][0][1].left() == pytest.approx(box.left() + 40, abs=2)

    view.select_object((0, xref))
    qtbot.keyClick(view, Qt.Key.Key_Delete)
    settled(qtbot, pane)
    qtbot.waitUntil(lambda: view.object_boxes.get(0) == [], timeout=15000)
    assert pane.session.edit_state["undo_name"] == "Delete Annotation"


def make_form(path):
    doc = pymupdf.open()
    page = doc.new_page(width=400, height=600)
    for name, kind, rect in (("name", pymupdf.PDF_WIDGET_TYPE_TEXT, (60, 80, 300, 104)),
                             ("class", pymupdf.PDF_WIDGET_TYPE_TEXT, (60, 120, 300, 144)),
                             ("inspired", pymupdf.PDF_WIDGET_TYPE_CHECKBOX, (60, 160, 76, 176))):
        widget = pymupdf.Widget()
        widget.field_name, widget.field_type = name, kind
        widget.rect = pymupdf.Rect(*rect)
        widget.field_value = False if kind == pymupdf.PDF_WIDGET_TYPE_CHECKBOX else ""
        page.add_widget(widget)
    doc.save(path)
    doc.close()


def test_filling_a_form_with_inline_editors_and_tab(qtbot, library, window):
    book = add_tome(library, maker=make_form)
    pane = open_tome(qtbot, window, book)
    pane.toggle_scriptorium()
    view, tools = pane.view, pane.scriptorium
    tools.choose_tool("form")
    qtbot.waitUntil(lambda: 0 in tools._fields, timeout=15000)
    assert len(view.field_boxes[0]) == 3

    def click(x, y):
        qtbot.mouseClick(view.viewport(), Qt.MouseButton.LeftButton,
                         pos=view.mapFromScene(view._items[0].pos() + QPointF(x, y)))
    click(120, 92)
    assert view.has_inline_editor()
    editor = view.viewport().findChild(QLineEdit)
    qtbot.keyClicks(editor, "Hailey-May")
    qtbot.keyClick(editor, Qt.Key.Key_Tab)  # commits, then opens the next field
    qtbot.waitUntil(lambda: view.has_inline_editor() and not tools._requests, timeout=15000)
    editor = view.viewport().findChild(QLineEdit)
    qtbot.keyClicks(editor, "Ranger")
    qtbot.keyClick(editor, Qt.Key.Key_Return)
    settled(qtbot, pane)
    assert not view.has_inline_editor()
    click(68, 168)  # the checkbox toggles on a click
    settled(qtbot, pane)

    pane.save()
    qtbot.waitUntil(lambda: not pane.session.dirty, timeout=15000)
    doc = pymupdf.open(library.book_file(book))
    values = {w.field_name: w.field_value for w in doc[0].widgets()}
    doc.close()
    assert values["name"] == "Hailey-May" and values["class"] == "Ranger"
    assert values["inspired"] not in (False, "Off", "")


def test_files_outside_the_library_are_never_edited(qtbot, window, tmp_path):
    path = make_pdf(tmp_path / "loose.pdf")
    pane = window.open_path(str(path))
    qtbot.waitUntil(pane.view.is_idle, timeout=15000)
    with qtbot.waitSignal(pane.notice) as said:
        pane.toggle_scriptorium()
    assert "library" in said.args[0]
    assert pane.scriptorium.toolbar.isHidden() and not pane.can_edit()
    assert not pane.save()


def test_closing_with_unsaved_changes_asks_first(qtbot, library, window, monkeypatch):
    book = add_tome(library)
    path = library.book_file(book)
    pane = open_tome(qtbot, window, book)
    pane.scriptorium._on_markup("highlight", {0: LINE})
    settled(qtbot, pane)

    monkeypatch.setattr(window, "_ask_unsaved", lambda title: "cancel")
    window.close_pane(pane)
    assert window.left.count() == 2 and pane.session.dirty  # still open

    monkeypatch.setattr(window, "_ask_unsaved", lambda title: "save")
    window.close_pane(pane)
    assert window.left.count() == 1
    assert annotations(path) == ["Highlight"]

    pane = open_tome(qtbot, window, book)
    pane.scriptorium.edit("note", page=0, point=(100, 100), text="scrap")
    settled(qtbot, pane)
    monkeypatch.setattr(window, "_ask_unsaved", lambda title: "discard")
    window.close_pane(pane)
    assert annotations(path) == ["Highlight"]  # the note was thrown away


def test_an_edit_shows_in_every_view_of_the_tome(qtbot, library, window):
    book = add_tome(library)
    first = open_tome(qtbot, window, book)
    window.open_alongside()
    twin = window.right.widget(0)
    qtbot.waitUntil(twin.view.is_idle, timeout=15000)
    first.scriptorium._on_markup("highlight", {0: LINE})
    settled(qtbot, first)
    assert twin.view.page_revs.get(0) == 1  # the other view repaints the page too
    assert window.right.tabText(0).startswith("• ")
    window.close_pane(first)  # no prompt: the twin still holds the edits
    assert twin.session.dirty


# ---- Scriptorium II: pages, contents, properties, restore -------------------------------

def page_titles(path):
    doc = pymupdf.open(path)
    titles = [doc[i].get_text().split(" of ")[0] for i in range(doc.page_count)]
    doc.close()
    return titles


def save(qtbot, pane):
    assert pane.save()
    qtbot.waitUntil(lambda: not pane.session.dirty and pane._save_request is None,
                    timeout=15000)


def test_page_organizer_rearranges_and_ribbons_follow_their_pages(qtbot, library, window):
    book = add_tome(library)
    path = library.book_file(book)
    ribbon = library.add_ribbon(book.id, 4, "Boss")  # on "Page 5"
    table = library.add_table("Session")
    library.add_pin(table.id, book.id, 1, None, "Doomed page")  # on "Page 2"
    pane = open_tome(qtbot, window, book)
    pane.toggle_scriptorium()
    pane.scriptorium.open_organizer()
    organizer = pane.scriptorium._organizer
    qtbot.waitUntil(lambda: organizer.list.count() == 6, timeout=5000)

    organizer.list.reordered.emit([4, 0, 1, 2, 3, 5])  # drag "Page 5" to the front
    settled(qtbot, pane)
    assert pane.view.ribbon_pages == {0}  # the ribbon is shown where its page now is
    organizer.list.item(2).setSelected(True)  # "Page 2"
    organizer._delete()
    settled(qtbot, pane)
    organizer.list.clearSelection()
    organizer.list.item(0).setSelected(True)
    organizer._rotate(90)
    settled(qtbot, pane)
    organizer._insert_blank()  # after the selected first page
    settled(qtbot, pane)
    assert organizer.list.count() == 6 and pane.view.page_count() == 6
    assert pane.side.pages.count() == 6
    assert organizer.undo.isEnabled()

    save(qtbot, pane)
    assert page_titles(path) == ["Page 5", "", "Page 1", "Page 3", "Page 4", "Page 6"]
    assert [(r.id, r.page) for r in library.ribbons(book.id)] == [(ribbon.id, 0)]
    assert library.pins(table.id) == []  # its page was deleted
    assert library.book(book.id).page_count == 6
    assert pane.view.ribbon_pages == {0}
    doc = pymupdf.open(path)
    assert doc[0].rotation == 90
    doc.close()
    organizer.close()


def test_extracting_pages_makes_a_new_tome(qtbot, library, window):
    book = add_tome(library)
    pane = open_tome(qtbot, window, book)
    pane.tome_title = "Heliana's Guide"
    pane.scriptorium.extract_pages([1, 2, 3])
    qtbot.waitUntil(lambda: len(library.all_books()) == 2, timeout=20000)
    qtbot.waitUntil(lambda: not window.importer.busy(), timeout=20000)
    new = next(b for b in library.all_books() if b.id != book.id)
    assert new.title == "Heliana's Guide (pages 2-4)" and new.page_count == 3
    assert [b.id for b in library.unshelved_books()][0] == new.id  # waits on the Returns Cart
    assert page_titles(library.book_file(new)) == ["Page 2", "Page 3", "Page 4"]
    assert not pane.session.dirty  # the source tome is untouched


def test_contents_editor_validates_and_applies(qtbot, library, window):
    from bookwyrm.ui.editor.dialogs import ContentsEditor
    dialog = ContentsEditor([[1, "First", 1], [2, "Inner", 2]], 6, 3)
    qtbot.addWidget(dialog)
    dialog.table.setCurrentCell(1, 1)
    dialog._add()  # for the page being read (page 4), at the level of the selected row
    assert dialog.toc() == [[1, "First", 1], [2, "Inner", 2], [2, "New entry", 4]]
    dialog._shift(1)
    assert dialog.toc()[2][0] == 3
    dialog._shift(1)
    with pytest.raises(ValueError, match="indented too far"):
        dialog.toc()
    dialog._shift(-3)
    dialog.table.item(2, 2).setText("99")
    with pytest.raises(ValueError, match="pages 1 to 6"):
        dialog.toc()
    dialog.table.item(2, 2).setText("6")
    dialog._move(-1)
    assert [e[1] for e in dialog.toc()] == ["First", "New entry", "Inner"]

    book = add_tome(library)
    pane = open_tome(qtbot, window, book)
    pane.scriptorium.edit("set_toc", toc=[[1, "Only entry", 5]])
    settled(qtbot, pane)
    assert pane.side.outline.topLevelItemCount() == 1
    assert pane.side.outline.topLevelItem(0).text(0) == "Only entry"
    pane.scriptorium.edit("set_toc", toc=[])  # a tome may have no contents at all
    settled(qtbot, pane)
    assert pane.side.widget(0) is pane.side.no_outline
    pane.scriptorium.undo()
    settled(qtbot, pane)
    assert pane.side.widget(0) is pane.side.outline


def test_properties_edit_renames_the_tome(qtbot, library, window, monkeypatch):
    from bookwyrm.ui.editor import scriptorium as module
    book = add_tome(library)
    pane = open_tome(qtbot, window, book)

    class Accepting(module.PropertiesDialog):
        def exec(self):
            self.fields["title"].setText("Heliana's Guide")
            self.fields["author"].setText("Loot Tavern")
            return True
    monkeypatch.setattr(module, "PropertiesDialog", Accepting)
    pane.scriptorium.edit_properties()
    settled(qtbot, pane)
    assert library.book(book.id).title == "Heliana's Guide"
    assert window.left.tabToolTip(1) == "Heliana's Guide"
    assert window.left.tabText(1) == "• Heliana's Guide"
    save(qtbot, pane)
    doc = pymupdf.open(library.book_file(book))
    assert doc.metadata["title"] == "Heliana's Guide" and doc.metadata["author"] == "Loot Tavern"
    doc.close()


def test_restore_original_brings_the_first_version_back(qtbot, library, window, monkeypatch):
    book = add_tome(library)
    path = library.book_file(book)
    original = path.read_bytes()
    pane = open_tome(qtbot, window, book)
    with qtbot.waitSignal(pane.scriptorium.notice) as said:
        pane.scriptorium.restore_original()  # nothing has been changed yet
    assert "nothing to restore" in said.args[0]

    pane.scriptorium.edit("delete_pages", pages=[0, 1, 2])
    settled(qtbot, pane)
    save(qtbot, pane)
    assert pane.view.page_count() == 3 and path.read_bytes() != original
    pane.scriptorium.edit("note", page=0, point=(50, 50), text="unsaved scrap")
    settled(qtbot, pane)

    monkeypatch.setattr("bookwyrm.ui.editor.scriptorium.library_dialogs.confirm",
                        lambda *args: True)
    pane.scriptorium.restore_original()
    qtbot.waitUntil(lambda: pane.view.page_count() == 6 and pane._save_request is None,
                    timeout=15000)
    assert path.read_bytes() == original
    assert not pane.session.dirty and library.book(book.id).page_count == 6
    assert not window.left.tabText(1).startswith("• ")


# ---- Scriptorium III: text, pictures, erasing ---------------------------------------------

def page_text(path, page=0):
    doc = pymupdf.open(path)
    found = doc[page].get_text()
    doc.close()
    return found


def test_editing_a_line_of_text_in_place(qtbot, library, window):
    book = add_tome(library)
    pane = open_tome(qtbot, window, book)
    pane.toggle_scriptorium()
    view, tools = pane.view, pane.scriptorium
    tools.choose_tool("edit_text")

    def click(x, y):
        qtbot.mouseClick(view.viewport(), Qt.MouseButton.LeftButton,
                         pos=view.mapFromScene(view._items[0].pos() + QPointF(x, y)))
    click(120, 76)  # on "Page 1 of the test tome"; its lines arrive, then the editor opens
    qtbot.waitUntil(view.has_inline_editor, timeout=15000)
    editor = view.viewport().findChild(QLineEdit)
    assert editor.text() == "Page 1 of the test tome"
    editor.selectAll()
    qtbot.keyClicks(editor, "Chapter One: The Wyrm")
    qtbot.keyClick(editor, Qt.Key.Key_Return)
    settled(qtbot, pane)
    assert pane.session.edit_state["undo_name"] == "Edit Text"

    with qtbot.waitSignal(tools.notice) as said:
        click(200, 500)  # blank paper
    assert "directly on a line" in said.args[0]

    tools.choose_tool("add_text")
    click(60, 420)
    editor = view.viewport().findChild(QLineEdit)
    qtbot.keyClicks(editor, "A new line of my own")
    qtbot.keyClick(editor, Qt.Key.Key_Return)
    settled(qtbot, pane)

    save(qtbot, pane)
    found = page_text(library.book_file(book))
    assert "Chapter One: The Wyrm" in found and "A new line of my own" in found
    assert "Page 1 of" not in found and "keyword-1" in found
    qtbot.waitUntil(lambda: library.book(book.id).indexed, timeout=20000)
    assert [h.page for h in library.search('"Chapter One"')] == [0]  # search sees the edit


def make_picture_tome(path):
    doc = pymupdf.open()
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 60, 40))
    pix.set_rect(pix.irect, (200, 30, 30))
    first = doc.new_page(width=400, height=600)
    xref = first.insert_image(pymupdf.Rect(50, 50, 170, 130), pixmap=pix)
    first.insert_text((50, 200), "Caption under the picture", fontsize=12)
    second = doc.new_page(width=400, height=600)
    second.insert_image(pymupdf.Rect(100, 300, 220, 380), xref=xref)
    doc.save(path)
    doc.close()


def colour(path, page, x, y):
    doc = pymupdf.open(path)
    pixel = doc[page].get_pixmap(clip=pymupdf.Rect(x, y, x + 2, y + 2)).pixel(0, 0)
    doc.close()
    return pixel


def test_pictures_can_be_moved_with_a_warning_when_shared(qtbot, library, window, monkeypatch):
    book = add_tome(library, maker=make_picture_tome)
    path = library.book_file(book)
    pane = open_tome(qtbot, window, book)
    pane.toggle_scriptorium()
    view, tools = pane.view, pane.scriptorium
    tools.choose_tool("picture")
    qtbot.waitUntil(lambda: bool(view.object_boxes.get(0)), timeout=15000)
    index, box, movable = view.object_boxes[0][0]
    assert movable and box.width() == pytest.approx(120, abs=1)

    asked = []
    def answer(parent, title, message, action):
        asked.append(message)
        return len(asked) > 1  # refuse the first time, agree the second
    monkeypatch.setattr("bookwyrm.ui.editor.scriptorium.library_dialogs.confirm", answer)

    tools._on_object_moved(0, index, box.translated(150, 300))
    qtbot.waitUntil(lambda: len(asked) == 1, timeout=15000)
    assert "shown on 2 pages" in asked[0]
    assert not pane.session.dirty  # refused: nothing changed

    tools._on_object_moved(0, index, box.translated(150, 300))
    qtbot.waitUntil(lambda: pane.session.dirty, timeout=15000)
    settled(qtbot, pane)
    assert pane.session.edit_state["undo_name"] == "Move Picture"
    save(qtbot, pane)
    assert colour(path, 0, 260, 390) == (200, 30, 30)  # at its new place
    assert colour(path, 0, 100, 90) == (255, 255, 255)  # gone from the old one
    assert colour(path, 1, 150, 340) == (255, 255, 255)  # and, as warned, from page two
    assert "Caption" in page_text(path)


def test_erasing_a_region(qtbot, library, window):
    book = add_tome(library)
    pane = open_tome(qtbot, window, book)
    pane.toggle_scriptorium()
    pane.scriptorium.edit("erase", page=0, rect=(40, 60, 380, 90), mode="text")
    settled(qtbot, pane)
    save(qtbot, pane)
    found = page_text(library.book_file(book))
    assert "Page 1 of" not in found and "keyword-1" in found
