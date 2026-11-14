"""Pictures whose description in the PDF contradicts the picture (see pdf/repair.py).

The tomes here imitate one found in the wild: a scanned book whose page
backgrounds are greyscale JPEGs described as full colour, which the engine
draws as a squashed strip above a black page.
"""

import hashlib

import pymupdf
import pytest

from bookwyrm.library import worker
from bookwyrm.pdf import editing, repair
from bookwyrm.pdf.editing import Editor
from bookwyrm.pdf.session_host import Host
from conftest import make_pdf
from test_session import ScriptedConn

NOTE = dict(page=0, point=(100, 100), text="A note")


def jpeg(colourspace=pymupdf.csGRAY, shade=250, width=120, height=160) -> bytes:
    pix = pymupdf.Pixmap(colourspace, pymupdf.IRect(0, 0, width, height), False)
    pix.clear_with(shade)
    return pix.tobytes("jpeg")


def mislabelled_pdf(path, pages=3):
    """A tome of pale paper-coloured pages, each a greyscale JPEG described as colour."""
    doc = pymupdf.open()
    xrefs = []
    for number in range(pages):
        page = doc.new_page(width=300, height=400)
        xref = page.insert_image(page.rect, stream=jpeg(shade=250 - number))  # no two alike
        doc.xref_set_key(xref, "ColorSpace", "/DeviceRGB")
        xrefs.append(xref)
    assert len(set(xrefs)) == pages
    assert doc.xref_get_key(xrefs[0], "Filter") == ("name", "/DCTDecode")
    doc.save(path)
    doc.close()
    return str(path), xrefs


def described_as(doc, xref) -> str:
    return doc.xref_get_key(xref, "ColorSpace")[1]


def foot_brightness(pix) -> float:
    """Average brightness of the bottom quarter of a rendered page (0 black, 255 white)."""
    start = (pix.height * 3 // 4) * pix.stride
    foot = bytes(pix.samples)[start:]
    return sum(foot) / len(foot)


def foot_of(doc, page=0) -> float:
    return foot_brightness(doc[page].get_pixmap(matrix=pymupdf.Matrix(0.5, 0.5), alpha=False))


@pytest.fixture
def bad(tmp_path):
    return mislabelled_pdf(tmp_path / "mislabelled.pdf")


# -- reading the picture and its description ---------------------------------------


def test_jpeg_components_come_from_the_pictures_own_header():
    assert repair.jpeg_components(jpeg(pymupdf.csGRAY)) == 1
    assert repair.jpeg_components(jpeg(pymupdf.csRGB)) == 3
    for junk in (b"", b"not a jpeg", b"\xff\xd8", b"\xff\xd8\xff", b"\xff\xd8\xff\xe0\x00"):
        assert repair.jpeg_components(junk) is None
    assert repair.jpeg_components(jpeg()[:12]) is None  # cut off before the frame header


def test_declared_components_understands_the_usual_colour_spaces(tmp_path):
    doc = pymupdf.open()
    page = doc.new_page()
    xref = page.insert_image(page.rect, stream=jpeg(pymupdf.csRGB))
    assert repair.declared_components(doc, xref) == 3
    profile = doc.get_new_xref()
    doc.update_object(profile, "<</N 3>>")
    doc.update_stream(profile, b"a stand-in colour profile")
    cases = {
        "/DeviceGray": 1, "/DeviceCMYK": 4, f"[/ICCBased {profile} 0 R]": 3,
        "[/CalRGB << /WhitePoint [0.95 1 1.09] >>]": 3,
        "[/Indexed /DeviceRGB 1 <000000ffffff>]": None,  # not a plain count: leave alone
        "[/Separation /Gold /DeviceCMYK 12 0 R]": None,
    }
    for written, expected in cases.items():
        doc.xref_set_key(xref, "ColorSpace", written)
        assert repair.declared_components(doc, xref) == expected, written
    indirect = doc.get_new_xref()  # the colour space kept as a separate object
    doc.update_object(indirect, f"[/ICCBased {profile} 0 R]")
    doc.xref_set_key(xref, "ColorSpace", f"{indirect} 0 R")
    assert repair.declared_components(doc, xref) == 3


# -- mending -------------------------------------------------------------------------


def test_a_greyscale_jpeg_described_as_colour_is_mended(bad):
    path, xrefs = bad
    doc = pymupdf.open(path)
    checked: set[int] = set()
    assert repair.mend_page(doc, 0, checked) == 1
    assert described_as(doc, xrefs[0]) == "/DeviceGray"
    assert described_as(doc, xrefs[1]) == "/DeviceRGB"  # other pages wait their turn
    assert foot_of(doc, 0) > 235  # pale paper to the bottom edge, not black
    assert repair.mend_page(doc, 0, checked) == 0
    assert repair.mend_document(doc, checked) == 2
    assert all(foot_of(doc, page) > 235 for page in range(3))
    assert repair.mend_document(doc) == 0  # nothing left to mend


def test_a_page_drawn_before_it_was_mended_comes_right_afterwards(bad):
    path, _xrefs = bad
    doc = pymupdf.open(path)
    doc[0].get_pixmap()  # the engine has now read the picture the wrong way
    repair.mend_page(doc, 0)
    assert foot_of(doc, 0) > 235


def test_sound_pictures_are_left_exactly_as_they_are(tmp_path):
    doc = pymupdf.open()
    page = doc.new_page()
    grey = page.insert_image(pymupdf.Rect(0, 0, 100, 100), stream=jpeg(pymupdf.csGRAY))
    colour = page.insert_image(pymupdf.Rect(0, 100, 100, 200), stream=jpeg(pymupdf.csRGB))
    drawn = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 20, 20), False)
    drawn.clear_with(90)
    png = page.insert_image(pymupdf.Rect(0, 200, 100, 300), stream=drawn.tobytes("png"))
    indexed = page.insert_image(pymupdf.Rect(0, 300, 100, 400), stream=jpeg(shade=249))
    doc.xref_set_key(indexed, "ColorSpace", "[/Indexed /DeviceRGB 1 <000000ffffff>]")
    before = {xref: doc.xref_object(xref) for xref in (grey, colour, png, indexed)}
    assert repair.mend_document(doc) == 0
    assert {xref: doc.xref_object(xref) for xref in before} == before


def test_a_decode_list_is_shortened_to_match(tmp_path):
    doc = pymupdf.open()
    page = doc.new_page()
    same = page.insert_image(pymupdf.Rect(0, 0, 100, 100), stream=jpeg(shade=240))
    mixed = page.insert_image(pymupdf.Rect(0, 100, 100, 200), stream=jpeg(shade=241))
    for xref, decode in ((same, "[1 0 1 0 1 0]"), (mixed, "[0 1 1 0 0 1]")):
        doc.xref_set_key(xref, "ColorSpace", "/DeviceRGB")
        doc.xref_set_key(xref, "Decode", decode)
    assert repair.mend_document(doc) == 2
    assert doc.xref_get_key(same, "Decode")[1].replace(" ", "") == "[10]"  # still inverted
    assert doc.xref_get_key(mixed, "Decode")[0] == "null"  # no sensible grey reading: dropped
    doc[0].get_pixmap()  # and the page still draws


def test_mending_never_raises(tmp_path):
    doc = pymupdf.open()
    doc.new_page()
    assert repair.mend_page(doc, 99) == 0  # no such page
    doc.close()
    assert repair.mend_page(doc, 0) == 0  # a closed document
    assert repair.mend_document(doc) == 0


# -- where the app applies it ----------------------------------------------------------


def test_the_reader_draws_a_mislabelled_page_properly(bad):
    path, _xrefs = bad
    conn = ScriptedConn([("preview", 1, 0, 150, 0), ("tile", 2, 1, 1.0, (0, 200, 300, 400), 0)])
    host = Host(conn, path)
    with pytest.raises(EOFError):
        host.serve()
    assert [message[0] for message in conn.sent] == ["pixels", "pixels"]
    for message, frame in zip(conn.sent, conn.frames, strict=True):
        _op, _request, _width, height, stride = message
        foot = frame[(height * 3 // 4) * stride:]
        assert sum(foot) / len(foot) > 235
    assert not host.editor.dirty  # looking at a tome does not count as changing it


def test_reading_a_mislabelled_tome_leaves_its_file_untouched(bad):
    path, _xrefs = bad
    digest = hashlib.sha256(open(path, "rb").read()).hexdigest()
    conn = ScriptedConn([("preview", 1, page, 150, 0) for page in range(3)])
    host = Host(conn, path)
    with pytest.raises(EOFError):
        host.serve()
    host.doc.close()
    assert hashlib.sha256(open(path, "rb").read()).hexdigest() == digest


def test_the_first_edit_mends_the_whole_tome_and_undo_leaves_it_mended(bad):
    path, xrefs = bad
    editor = Editor(pymupdf.open(path), path)
    editor.mend_page(0)
    assert [described_as(editor.doc, x) for x in xrefs] == ["/DeviceGray", "/DeviceRGB",
                                                            "/DeviceRGB"]
    assert not editor.dirty
    editor.apply("note", NOTE)  # history starts here; every page must be mended first
    assert [described_as(editor.doc, x) for x in xrefs] == ["/DeviceGray"] * 3
    editor.mend_page(2)  # asked again while history runs: harmless
    editor.undo()
    assert not editor.dirty
    assert [described_as(editor.doc, x) for x in xrefs] == ["/DeviceGray"] * 3
    assert all(foot_of(editor.doc, page) > 235 for page in range(3))
    editor.redo()
    assert len(editing.list_annots(editor.doc, 0)) == 1
    editor.doc.close()


def test_saving_an_edited_tome_keeps_the_repair(bad):
    path, xrefs = bad
    editor = Editor(pymupdf.open(path), path)
    editor.apply("note", NOTE)
    editor.save()
    assert editor.mended and not editor.dirty
    editor.apply("note", dict(NOTE, page=1))  # history can begin again after a save
    editor.doc.close()
    saved = pymupdf.open(path)
    assert [described_as(saved, x) for x in xrefs] == ["/DeviceGray"] * 3
    assert repair.mend_document(saved) == 0
    assert all(foot_of(saved, page) > 235 for page in range(3))


def test_restoring_the_original_brings_back_a_tome_that_is_mended_again_when_shown(
        bad, tmp_path):
    path, xrefs = bad
    archive = str(tmp_path / "archive" / "original.pdf")
    editor = Editor(pymupdf.open(path), path)
    editor.apply("note", NOTE)
    editor.save(archive)
    editor.restore(archive)
    assert described_as(editor.doc, xrefs[0]) == "/DeviceRGB"  # the file as first imported
    editor.mend_page(0)
    assert described_as(editor.doc, xrefs[0]) == "/DeviceGray"
    editor.doc.close()


def test_pages_inserted_from_a_mislabelled_pdf_arrive_mended(bad, tmp_path):
    path, _xrefs = bad
    tome = make_pdf(tmp_path / "tome.pdf")
    editor = Editor(pymupdf.open(tome), str(tome))
    editor.apply("note", NOTE)  # history is already running when the pages come in
    editor.apply("insert_pdf", dict(path=path, at=1))
    assert editor.doc.page_count == 9
    assert all(foot_of(editor.doc, page) > 235 for page in (1, 2, 3))
    editor.doc.close()


def test_extracted_pages_are_written_mended(bad, tmp_path):
    path, _xrefs = bad
    doc = pymupdf.open(path)
    destination = str(tmp_path / "extract.pdf")
    assert editing.extract_pages(doc, [0, 2], destination) == 2
    written = pymupdf.open(destination)
    assert repair.mend_document(written) == 0
    assert all(foot_of(written, page) > 235 for page in range(2))


def test_the_cover_of_a_mislabelled_tome_is_drawn_properly(bad, tmp_path):
    path, _xrefs = bad
    digest = hashlib.sha256(open(path, "rb").read()).hexdigest()
    cover = str(tmp_path / "covers" / "1.jpg")
    assert worker.make_cover(pymupdf, path, cover)
    assert foot_brightness(pymupdf.Pixmap(cover)) > 235
    assert hashlib.sha256(open(path, "rb").read()).hexdigest() == digest
