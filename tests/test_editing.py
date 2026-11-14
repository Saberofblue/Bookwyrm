"""The Scriptorium's engine, exercised directly (no Qt, no helper process)."""

import os

import pymupdf
import pytest

from bookwyrm.pdf import editing
from bookwyrm.pdf.editing import EditError, Editor
from conftest import make_pdf


@pytest.fixture
def editor(tmp_path):
    path = make_pdf(tmp_path / "tome.pdf")
    ed = Editor(pymupdf.open(path), str(path))
    yield ed
    ed.doc.close()


def kinds(editor, page=0):
    return [a["kind"] for a in editing.list_annots(editor.doc, page)]


LINE = [(50, 66, 280, 84)]  # the first text line of every test page


def test_every_annotation_kind_can_be_added_undone_and_redone(editor):
    steps = [
        ("markup", dict(page=0, kind="highlight", rects=LINE), "Highlight"),
        ("markup", dict(page=0, kind="underline", rects=LINE), "Underline"),
        ("markup", dict(page=0, kind="strikeout", rects=LINE), "StrikeOut"),
        ("note", dict(page=0, point=(300, 100), text="Check this"), "Text"),
        ("textbox", dict(page=0, rect=(60, 340, 260, 400), text="A margin note"), "FreeText"),
        ("ink", dict(page=0, strokes=[[(60, 420), (90, 450), (140, 430)]]), "Ink"),
        ("shape", dict(page=0, kind="rect", points=(60, 460, 160, 520)), "Square"),
        ("shape", dict(page=0, kind="ellipse", points=(180, 460, 280, 520)), "Circle"),
        ("shape", dict(page=0, kind="line", points=(60, 540, 200, 560)), "Line"),
        ("shape", dict(page=0, kind="arrow", points=(60, 570, 200, 590)), "Line"),
    ]
    for name, args, _kind in steps:
        entry = editor.apply(name, args)
        assert entry["pages"] == [0] and isinstance(entry["result"], int)
    expected = [kind for *_rest, kind in steps]
    assert kinds(editor) == expected
    for remaining in range(len(steps) - 1, -1, -1):
        editor.undo()
        assert kinds(editor) == expected[:remaining]
    assert not editor.can_undo() and not editor.dirty
    for count in range(1, len(steps) + 1):
        editor.redo()
        assert kinds(editor) == expected[:count]
    assert not editor.can_redo() and editor.dirty


def test_annotations_can_be_edited_moved_and_deleted(editor):
    xref = editor.apply("note", dict(page=1, point=(100, 100), text="first"))["result"]
    editor.apply("edit_annot", dict(page=1, xref=xref, text="second"))
    assert editing.list_annots(editor.doc, 1)[0]["text"] == "second"
    editor.apply("move_annot", dict(page=1, xref=xref, rect=(200, 300, 220, 320)))
    assert editing.list_annots(editor.doc, 1)[0]["rect"][0] == pytest.approx(200, abs=1)
    editor.apply("delete_annot", dict(page=1, xref=xref))
    assert kinds(editor, 1) == []
    editor.undo()
    assert editing.list_annots(editor.doc, 1)[0]["text"] == "second"
    editor.undo()
    editor.undo()
    assert editing.list_annots(editor.doc, 1)[0]["text"] == "first"


def test_a_failing_edit_changes_nothing_and_leaves_the_editor_usable(editor):
    editor.apply("markup", dict(page=0, kind="highlight", rects=LINE))
    with pytest.raises(EditError):
        editor.apply("markup", dict(page=0, kind="highlight", rects=[]))
    with pytest.raises(EditError):
        editor.apply("delete_annot", dict(page=0, xref=999999))
    with pytest.raises(EditError):
        editor.apply("note", dict(page=99, point=(1, 1), text="x"))
    with pytest.raises(EditError):
        editor.apply("no_such_edit", {})
    assert kinds(editor) == ["Highlight"]
    assert editor.summary()["undo_name"] == "Mark Text"
    editor.apply("note", dict(page=0, point=(10, 10), text="still works"))
    editor.undo()
    editor.undo()
    assert kinds(editor) == [] and not editor.dirty


def test_a_new_edit_discards_the_redo_tail(editor):
    editor.apply("note", dict(page=0, point=(10, 10), text="a"))
    editor.apply("note", dict(page=0, point=(40, 10), text="b"))
    editor.undo()
    assert editor.summary()["redo_name"] == "Add Note"
    editor.apply("shape", dict(page=0, kind="rect", points=(1, 1, 50, 50)))
    assert not editor.can_redo()
    assert kinds(editor) == ["Text", "Square"]


def test_save_writes_atomically_keeps_an_archive_copy_and_starts_history_afresh(
        editor, tmp_path):
    original = open(editor.path, "rb").read()
    archive = tmp_path / "keep" / "original.pdf"
    editor.apply("markup", dict(page=2, kind="highlight", rects=LINE))
    report = editor.save(str(archive))
    assert archive.read_bytes() == original  # pristine copy, made once
    assert report["mapping"] is None
    assert report["sha256"] == editing.sha256_of(editor.path)
    assert not editor.dirty and not editor.can_undo() and not editor.can_redo()
    assert kinds(editor, 2) == ["Highlight"]  # the reopened tome has the change
    assert not os.path.exists(editor.path + ".saving")

    editor.apply("note", dict(page=2, point=(10, 10), text="later"))
    assert editor.dirty
    editor.undo()  # edits made after a save can be undone back to that save
    assert not editor.dirty and kinds(editor, 2) == ["Highlight"]
    editor.redo()
    editor.save(str(archive))
    assert archive.read_bytes() == original  # never overwritten by later saves
    reopened = pymupdf.open(editor.path)
    assert [a.type[1] for a in reopened[2].annots()] == ["Highlight", "Text"]
    reopened.close()


def test_undo_returns_to_a_clean_state(editor):
    editor.apply("note", dict(page=0, point=(10, 10), text="a"))
    editor.apply("note", dict(page=0, point=(40, 10), text="b"))
    assert editor.dirty
    editor.undo()
    editor.undo()
    assert not editor.dirty and kinds(editor) == []
    assert editor.undo() is None


@pytest.fixture
def form(tmp_path):
    path = tmp_path / "sheet.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    for name, kind, rect, value in (
        ("name", pymupdf.PDF_WIDGET_TYPE_TEXT, (72, 72, 300, 92), ""),
        ("inspired", pymupdf.PDF_WIDGET_TYPE_CHECKBOX, (72, 110, 86, 124), False),
    ):
        widget = pymupdf.Widget()
        widget.field_name, widget.field_type = name, kind
        widget.rect = pymupdf.Rect(*rect)
        widget.field_value = value
        page.add_widget(widget)
    doc.save(path)
    doc.close()
    ed = Editor(pymupdf.open(path), str(path))
    yield ed
    ed.doc.close()


def test_form_fields_are_listed_filled_and_undone(form):
    fields = {f["name"]: f for f in editing.list_widgets(form.doc, 0)}
    assert fields["name"]["kind"] == "Text" and fields["inspired"]["kind"] == "CheckBox"
    form.apply("fill_field", dict(page=0, xref=fields["name"]["xref"], value="Hailey-May"))
    form.apply("fill_field", dict(page=0, xref=fields["inspired"]["xref"], value=True))
    values = {f["name"]: f["value"] for f in editing.list_widgets(form.doc, 0)}
    assert values["name"] == "Hailey-May" and values["inspired"] not in (False, "Off", "")
    form.undo()
    form.undo()
    values = {f["name"]: f["value"] for f in editing.list_widgets(form.doc, 0)}
    assert values["name"] == "" and values["inspired"] in (False, "Off", "")
    form.redo()
    form.redo()
    form.save()
    saved = pymupdf.open(form.path)
    assert {w.field_name: w.field_value for w in saved[0].widgets()}["name"] == "Hailey-May"
    saved.close()


# ---- pages, contents, properties ------------------------------------------------------

def page_titles(editor):
    return [editor.doc[i].get_text().split(" of ")[0] for i in range(editor.doc.page_count)]


def test_rearranging_pages_tracks_where_each_page_came_from(editor):
    assert page_titles(editor)[:3] == ["Page 1", "Page 2", "Page 3"]
    entry = editor.apply("reorder_pages", dict(order=[2, 0, 1, 5]))  # also drops pages 4 and 5
    assert entry["structural"]
    assert page_titles(editor) == ["Page 3", "Page 1", "Page 2", "Page 6"]
    assert editor.origins == [2, 0, 1, 5]
    editor.undo()
    assert page_titles(editor) == [f"Page {n}" for n in range(1, 7)]
    assert editor.origins == list(range(6))
    editor.redo()
    report = editor.save()
    assert report["mapping"] == {2: 0, 0: 1, 1: 2, 5: 3} and report["page_count"] == 4
    assert editor.origins == [0, 1, 2, 3]
    reopened = pymupdf.open(editor.path)
    assert reopened.page_count == 4 and "Page 3" in reopened[0].get_text()
    reopened.close()


def test_delete_rotate_insert_and_crop_are_undoable(editor, tmp_path):
    editor.apply("delete_pages", dict(pages=[1, 3]))
    assert page_titles(editor) == ["Page 1", "Page 3", "Page 5", "Page 6"]
    editor.apply("rotate_pages", dict(pages=[0], angle=90))
    assert editor.doc[0].rotation == 90
    assert (editor.doc[0].rect.width, editor.doc[0].rect.height) == (600, 400)
    editor.apply("insert_blank", dict(at=1))
    assert editor.doc.page_count == 5 and editor.doc[1].get_text().strip() == ""
    assert editor.origins == [0, None, 2, 4, 5]
    other = make_pdf(tmp_path / "other.pdf", pages=3, title="Other")
    added = editor.apply("insert_pdf", dict(path=str(other), at=5, first=1))["result"]
    assert added == 2 and editor.doc.page_count == 7
    assert editor.origins == [0, None, 2, 4, 5, None, None]
    editor.apply("crop_page", dict(page=2, rect=(50, 50, 350, 330)))
    assert editor.doc[2].rect.width == pytest.approx(300)
    for _ in range(5):
        editor.undo()
    assert not editor.dirty
    assert page_titles(editor) == [f"Page {n}" for n in range(1, 7)]
    assert editor.doc[0].rotation == 0 and editor.doc[2].rect.width == pytest.approx(400)
    assert editor.origins == list(range(6))


def test_page_edits_refuse_nonsense(editor):
    for name, args in (
        ("delete_pages", dict(pages=[0, 1, 2, 3, 4, 5])),  # a tome needs a page
        ("delete_pages", dict(pages=[])),
        ("reorder_pages", dict(order=[0, 0, 1])),
        ("reorder_pages", dict(order=[0, 1, 2, 3, 4, 5])),
        ("rotate_pages", dict(pages=[0], angle=45)),
        ("insert_pdf", dict(path="/no/such/file.pdf", at=0)),
        ("insert_pdf", dict(path=editor.path, at=0)),
        ("crop_page", dict(page=0, rect=(0, 0, 5, 5))),
    ):
        with pytest.raises(EditError):
            editor.apply(name, args)
    assert not editor.dirty and editor.doc.page_count == 6


def test_contents_and_properties_can_be_edited_and_undone(editor):
    before = editor.doc.get_toc()
    editor.apply("set_toc", dict(toc=[[1, "Opening", 1], [2, "Details", 2], [1, "Closing", 6]]))
    assert [e[1] for e in editor.doc.get_toc()] == ["Opening", "Details", "Closing"]
    editor.apply("set_metadata", dict(fields={"title": "Renamed", "author": "B. Wyrm"}))
    assert editor.doc.metadata["title"] == "Renamed"
    editor.undo()
    assert editor.doc.metadata["title"] == "Test Tome"
    editor.undo()
    assert editor.doc.get_toc() == before
    for bad in ([[2, "Too deep", 1]], [[1, "", 1]], [[1, "Nowhere", 99]],
                [[1, "A", 1], [3, "Skips a level", 2]]):
        with pytest.raises(EditError):
            editor.apply("set_toc", dict(toc=bad))


def test_extract_pages_writes_a_new_pdf_and_leaves_the_tome_alone(editor, tmp_path):
    out = tmp_path / "extract.pdf"
    assert editing.extract_pages(editor.doc, [4, 0], str(out)) == 2
    extracted = pymupdf.open(out)
    assert [extracted[i].get_text().split(" of ")[0] for i in range(2)] == ["Page 5", "Page 1"]
    extracted.close()
    assert editor.doc.page_count == 6 and not editor.dirty


def test_restore_puts_the_archive_copy_back(editor, tmp_path):
    archive = tmp_path / "original.pdf"
    editor.apply("delete_pages", dict(pages=[0, 1]))
    editor.save(str(archive))
    assert editor.doc.page_count == 4
    report = editor.restore(str(archive))
    assert report["restored"] and report["page_count"] == 6
    assert editor.doc.page_count == 6 and not editor.dirty
    assert open(editor.path, "rb").read() == archive.read_bytes()
    with pytest.raises(EditError):
        editor.restore(str(tmp_path / "missing.pdf"))


# ---- page content: text, pictures, erasing ---------------------------------------------

def text(editor, page=0):
    return editor.doc[page].get_text()


def first_line(editor, page=0):
    return editing.list_text_lines(editor.doc, page)[0]


def test_text_lines_report_what_is_needed_to_reset_them(editor):
    line = first_line(editor)
    assert line["text"] == "Page 1 of the test tome"
    assert line["size"] == pytest.approx(18) and line["upright"]
    assert line["color"] == (0, 0, 0) and len(line["origin"]) == 2
    assert line["rect"][0] == pytest.approx(50, abs=1)


def test_replacing_a_line_keeps_its_place_and_its_neighbours(editor):
    line = first_line(editor)
    below = editing.list_text_lines(editor.doc, 0)[1]["text"]
    entry = editor.apply("replace_text", dict(
        page=0, rect=line["rect"], text="Chapter One: The Wyrm", font=line["font"],
        size=line["size"], flags=line["flags"], color=line["color"], origin=line["origin"]))
    assert entry["pages"] == [0] and "original_font" in entry["result"]
    lines = {ln["text"]: ln for ln in editing.list_text_lines(editor.doc, 0)}
    new = lines["Chapter One: The Wyrm"]
    assert new["origin"] == pytest.approx(line["origin"], abs=0.5)
    assert new["size"] == pytest.approx(18)
    assert below in lines  # the line underneath was not caught
    assert "Page 1 of" not in text(editor)
    assert len(editor.doc[0].get_drawings()) == 1  # the rectangle on the page survived
    editor.undo()
    assert first_line(editor)["text"] == "Page 1 of the test tome"
    assert "Chapter One" not in text(editor)


def test_emptying_a_line_removes_it(editor):
    line = first_line(editor)
    editor.apply("replace_text", dict(page=0, rect=line["rect"], text="  "))
    assert "Page 1 of" not in text(editor) and "keyword-1" in text(editor)


def test_the_original_font_is_reused_only_when_it_has_the_letters(tmp_path):
    from bookwyrm.ui.theme.theme import HERE
    garamond = str(HERE / "fonts" / "EBGaramond[wght].ttf")
    path = tmp_path / "fancy.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 100), "silver dragon", fontsize=14, fontname="garamond",
                     fontfile=garamond)
    doc.save(path, garbage=3, deflate=True)  # as a typical PDF would be: font subsetted
    doc.subset_fonts() if hasattr(doc, "subset_fonts") else None
    doc.close()
    ed = Editor(pymupdf.open(path), str(path))
    line = editing.list_text_lines(ed.doc, 0)[0]
    args = dict(page=0, rect=line["rect"], font=line["font"], size=line["size"],
                flags=line["flags"], color=line["color"], origin=line["origin"])
    result = ed.apply("replace_text", dict(args, text="silver wyrm"))["result"]
    assert [ln["text"] for ln in editing.list_text_lines(ed.doc, 0)] == ["silver wyrm"]
    assert isinstance(result["original_font"], bool)
    ed.doc.close()


def test_added_text_and_erasing(editor):
    editor.apply("add_text", dict(page=0, point=(60, 400), text="First line\nSecond line",
                                  size=12, color=(0.5, 0, 0)))
    assert "First line" in text(editor) and "Second line" in text(editor)
    lines = {ln["text"]: ln for ln in editing.list_text_lines(editor.doc, 0)}
    assert lines["Second line"]["origin"][1] == pytest.approx(415, abs=0.5)

    editor.apply("erase", dict(page=0, rect=(40, 390, 300, 425), mode="text"))
    assert "First line" not in text(editor) and "keyword-1" in text(editor)
    drawings = len(editor.doc[0].get_drawings())
    editor.apply("erase", dict(page=0, rect=(40, 130, 380, 330), mode="text"))  # the rectangle
    assert len(editor.doc[0].get_drawings()) == drawings - 1
    editor.apply("erase", dict(page=0, rect=(40, 60, 300, 90), mode="cover"))
    assert "Page 1 of" in text(editor)  # covered, not removed
    with pytest.raises(EditError):
        editor.apply("erase", dict(page=0, rect=(0, 0, 10, 10), mode="scorch"))
    with pytest.raises(EditError):
        editor.apply("add_text", dict(page=0, point=(1, 1), text="   "))
    for _ in range(4):
        editor.undo()
    assert not editor.dirty and "First line" not in text(editor)
    assert len(editor.doc[0].get_drawings()) == 1


@pytest.fixture
def pictures(tmp_path):
    """A two-page tome whose pages both show the same stored picture, plus a spare PNG."""
    def png(name, color):
        pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 60, 40))
        pix.set_rect(pix.irect, color)
        pix.save(tmp_path / name)
        return str(tmp_path / name)
    red, blue = png("red.png", (200, 30, 30)), png("blue.png", (30, 30, 200))
    path = tmp_path / "pictures.pdf"
    doc = pymupdf.open()
    first = doc.new_page(width=400, height=600)
    xref = first.insert_image(pymupdf.Rect(50, 50, 170, 130), filename=red)
    first.insert_text((50, 200), "Caption under the picture", fontsize=12)
    second = doc.new_page(width=400, height=600)
    second.insert_image(pymupdf.Rect(100, 300, 220, 380), xref=xref)  # same stored image
    doc.save(path)
    doc.close()
    ed = Editor(pymupdf.open(path), str(path))
    yield ed, blue
    ed.doc.close()


def colour_at(editor, page, x, y):
    pix = editor.doc[page].get_pixmap(clip=pymupdf.Rect(x, y, x + 2, y + 2))
    return pix.pixel(0, 0)


def test_pictures_are_listed_moved_replaced_deleted_and_added(pictures):
    editor, blue = pictures
    shown = editing.list_images(editor.doc, 0)
    assert len(shown) == 1 and shown[0]["rect"] == pytest.approx((50, 50, 170, 130), abs=0.5)
    xref = shown[0]["xref"]
    assert editing.image_uses(editor.doc, xref) == 2  # the same stored picture on both pages

    assert colour_at(editor, 0, 100, 90) == (200, 30, 30)
    editor.apply("move_image", dict(page=0, xref=xref, rect=(200, 400, 380, 520)))
    assert colour_at(editor, 0, 300, 460) == (200, 30, 30)  # now here, and larger
    assert colour_at(editor, 0, 100, 90) == (255, 255, 255)  # no longer there
    assert "Caption" in text(editor)  # text nearby is untouched
    editor.undo()
    assert colour_at(editor, 0, 100, 90) == (200, 30, 30)
    assert colour_at(editor, 1, 150, 340) == (200, 30, 30)

    editor.apply("replace_image", dict(page=0, xref=xref, path=blue))
    assert colour_at(editor, 0, 100, 90) == (30, 30, 200)
    assert colour_at(editor, 1, 150, 340) == (30, 30, 200)  # every page showing it changes
    editor.undo()
    assert colour_at(editor, 0, 100, 90) == (200, 30, 30)

    editor.apply("delete_image", dict(page=0, xref=xref))
    assert colour_at(editor, 0, 100, 90) == (255, 255, 255)
    editor.undo()
    assert colour_at(editor, 0, 100, 90) == (200, 30, 30)

    editor.apply("insert_image", dict(page=1, rect=(20, 20, 140, 100), path=blue))
    assert colour_at(editor, 1, 80, 60) == (30, 30, 200)
    assert len(editing.list_images(editor.doc, 1)) == 2
    with pytest.raises(EditError):
        editor.apply("insert_image", dict(page=1, rect=(0, 0, 50, 50), path="/no/file.png"))
    with pytest.raises(EditError):
        editor.apply("replace_image", dict(page=0, xref=xref, path=editor.path + ".nope"))


# ---- inserting again after an undo ------------------------------------------------------
# The PDF engine remembers fonts and images it has embedded and reuses them by object
# number. Undo removes those objects, so a naive second insertion points at nothing.

def ink(editor, rect, page=0):
    """How many dark pixels a region of the rendered page has."""
    pix = editor.doc[page].get_pixmap(clip=pymupdf.Rect(*rect), colorspace=pymupdf.csGRAY)
    return sum(1 for value in pix.samples if value < 128)


@pytest.mark.parametrize("name, args, region", [
    ("textbox", dict(page=0, rect=(60, 340, 300, 380), text="A margin note", fontsize=14),
     (60, 340, 300, 380)),
    ("add_text", dict(page=0, point=(60, 420), text="Written on the page", size=14),
     (60, 400, 300, 430)),
    ("add_text", dict(page=0, point=(60, 420), text="Sans this time", size=14, serif=False),
     (60, 400, 300, 430)),
])
def test_text_inserted_again_after_an_undo_still_renders(editor, name, args, region):
    assert ink(editor, region) == 0
    editor.apply(name, args)
    expected = ink(editor, region)
    assert expected > 100
    for _ in range(3):  # undo, then do the very same thing afresh, several times over
        editor.undo()
        assert ink(editor, region) == 0
        editor.apply(name, args)
        assert ink(editor, region) == expected
    editor.save()
    assert ink(editor, region) == expected  # and it survives a save and reopen


def test_replacing_text_again_after_an_undo_still_renders(editor):
    line = first_line(editor)
    args = dict(page=0, rect=line["rect"], text="Chapter One", font=line["font"],
                size=line["size"], flags=line["flags"], color=line["color"],
                origin=line["origin"])
    region = (40, 60, 380, 90)
    editor.apply("replace_text", args)
    expected = ink(editor, region)
    editor.undo()
    editor.apply("replace_text", args)
    assert ink(editor, region) == expected and "Chapter One" in text(editor)


def test_a_form_field_filled_again_after_an_undo_still_renders(form):
    xref = next(f["xref"] for f in editing.list_widgets(form.doc, 0) if f["name"] == "name")
    region = (72, 72, 300, 92)
    form.apply("fill_field", dict(page=0, xref=xref, value="Hailey-May"))
    expected = ink(form, region)
    assert expected > 50
    form.undo()
    form.apply("fill_field", dict(page=0, xref=xref, value="Hailey-May"))
    assert ink(form, region) == expected


def test_the_same_picture_inserted_again_after_an_undo_still_shows(pictures):
    editor, blue = pictures
    args = dict(page=1, rect=(20, 20, 140, 100), path=blue)
    for _ in range(3):
        editor.apply("insert_image", args)
        assert colour_at(editor, 1, 80, 60) == (30, 30, 200)
        editor.undo()
        assert colour_at(editor, 1, 80, 60) == (255, 255, 255)
    editor.apply("insert_image", args)
    xref = next(i["xref"] for i in editing.list_images(editor.doc, 1)
                if i["rect"][0] < 30)
    editor.apply("delete_image", dict(page=1, xref=xref))
    editor.undo()
    editor.apply("delete_image", dict(page=1, xref=xref))
    assert colour_at(editor, 1, 80, 60) == (255, 255, 255)
    editor.save()
    assert colour_at(editor, 1, 150, 340) == (200, 30, 30)  # the other picture is unharmed


def test_jpeg_and_transparent_pictures_are_embedded_correctly(editor, tmp_path):
    jpeg, clear = tmp_path / "photo.jpg", tmp_path / "clear.png"
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 80, 60))
    pix.set_rect(pix.irect, (20, 160, 60))
    pix.save(jpeg, jpg_quality=95)
    see_through = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 80, 60), 1)
    see_through.set_rect(see_through.irect, (200, 30, 30, 0))  # fully transparent red
    see_through.set_rect(pymupdf.IRect(0, 0, 40, 60), (200, 30, 30, 255))  # left half solid
    see_through.save(clear)
    editor.apply("insert_image", dict(page=0, rect=(60, 340, 220, 460), path=str(jpeg)))
    r, g, b = colour_at(editor, 0, 140, 400)
    assert abs(r - 20) < 12 and abs(g - 160) < 12 and abs(b - 60) < 12
    stored = editing.list_images(editor.doc, 0)[0]["xref"]
    assert editor.doc.xref_get_key(stored, "Filter")[1] == "/DCTDecode"  # not re-encoded

    editor.apply("insert_image", dict(page=1, rect=(60, 340, 220, 460), path=str(clear)))
    assert colour_at(editor, 1, 80, 400) == (200, 30, 30)  # solid half
    assert colour_at(editor, 1, 200, 400) == (255, 255, 255)  # see-through half shows the page


# ---- a library file that cannot be replaced (Windows: another program has it open) -------

def test_a_save_that_cannot_replace_the_file_loses_nothing(editor, monkeypatch):
    path = editor.path
    on_disk = open(path, "rb").read()

    def locked(source, target, **_kw):
        raise PermissionError("the file is in use")
    monkeypatch.setattr(editing, "replace_file", locked)
    editor.apply("reorder_pages", dict(order=[5, 0, 1, 2, 3, 4]))
    editor.apply("note", dict(page=0, point=(40, 40), text="keep me"))
    with pytest.raises(EditError, match="another program"):
        editor.save()
    assert open(path, "rb").read() == on_disk  # the library file is exactly as it was
    assert editor.dirty and editor.stranded and os.path.exists(editor.stranded)
    assert page_titles(editor)[0] == "Page 6" and kinds(editor, 0) == ["Text"]  # still there
    assert editor.origins == [5, 0, 1, 2, 3, 4]  # still measured against the library file
    assert not editor.can_undo()  # but, as after any save, history starts afresh

    with pytest.raises(EditError):  # still locked on a second try: still nothing lost
        editor.save()
    assert editor.dirty and page_titles(editor)[0] == "Page 6"

    monkeypatch.undo()  # the other program lets go
    stranded = editor.stranded
    editor.apply("note", dict(page=1, point=(40, 40), text="and me"))
    report = editor.save()
    assert report["mapping"] == {5: 0, 0: 1, 1: 2, 2: 3, 3: 4, 4: 5}
    assert not editor.dirty and editor.stranded is None
    assert not os.path.exists(stranded) and not os.path.exists(path + ".saving")
    saved = pymupdf.open(path)
    assert "Page 6" in saved[0].get_text() and len(list(saved[1].annots())) == 1
    saved.close()


def test_replace_file_waits_out_a_brief_lock(tmp_path, monkeypatch):
    source, target = tmp_path / "new", tmp_path / "old"
    source.write_text("new")
    target.write_text("old")
    real, calls = os.replace, []

    def flaky(a, b):
        calls.append(1)
        if len(calls) < 3:
            raise PermissionError("busy")
        real(a, b)
    monkeypatch.setattr(editing.os, "replace", flaky)
    editing.replace_file(str(source), str(target), pause=0.001)
    assert target.read_text() == "new" and len(calls) == 3
    calls.clear()
    monkeypatch.setattr(editing.os, "replace",
                        lambda a, b: (_ for _ in ()).throw(PermissionError("stuck")))
    with pytest.raises(PermissionError):
        editing.replace_file(str(source), str(target), attempts=3, pause=0.001)
