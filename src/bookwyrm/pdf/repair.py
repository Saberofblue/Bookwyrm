"""Mending pictures that a PDF describes wrongly. Qt-free.

A picture in a PDF comes in two parts: the picture's own data (very often a JPEG)
and a short description beside it saying how to read that data. Some programs
write a description that contradicts the data, most often a greyscale JPEG
described as full colour. Acrobat, Preview and browsers believe the JPEG and show
the picture properly. MuPDF believes the description: it expects three bytes per
pixel, receives one, draws the picture squashed into its top third and fills the
rest with black.

The cure is to correct the description, in memory, before the picture is first
drawn. Nothing is written to disk by this module; a corrected description reaches
the file only if the tome is saved for some other reason, which leaves the file
more correct than it was.

The description cannot be changed while the editor's undo history is running
(the engine refuses changes made outside an undoable step), so the callers mend
a page before it is drawn while there is no history, and mend the whole tome
just before history begins. See Editor.mend_page() and Editor.apply().
"""

from __future__ import annotations

import re
import struct

# How many numbers per pixel each simple colour space uses.
_COMPONENTS = {
    "/DeviceGray": 1, "/G": 1, "/CalGray": 1,
    "/DeviceRGB": 3, "/RGB": 3, "/CalRGB": 3, "/Lab": 3,
    "/DeviceCMYK": 4, "/CMYK": 4,
}
_REPLACEMENT = {1: "/DeviceGray", 3: "/DeviceRGB"}
_HEAD = 1 << 16  # a JPEG's frame header sits well inside its first 64 KB


def jpeg_components(data: bytes) -> int | None:
    """How many colour components a JPEG holds (1 grey, 3 colour, 4 CMYK), or None."""
    if data[:2] != b"\xff\xd8":
        return None
    at, end = 2, len(data)
    while at + 4 <= end:
        if data[at] != 0xFF:
            return None
        marker = data[at + 1]
        if marker == 0xFF:  # padding between segments
            at += 1
            continue
        if marker == 0x01 or 0xD0 <= marker <= 0xD8:  # markers with no length
            at += 2
            continue
        if marker in (0xD9, 0xDA):  # end of image, or picture data before any frame header
            return None
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):  # a frame header
            return data[at + 9] if at + 9 < end else None
        at += 2 + struct.unpack(">H", data[at + 2:at + 4])[0]
    return None


def declared_components(doc, xref: int) -> int | None:
    """How many components the PDF says a picture has. None when that is not a simple
    count (indexed and spot colours, masks), in which case the picture is left alone."""
    kind, value = doc.xref_get_key(xref, "ColorSpace")
    if kind == "xref":
        value = doc.xref_object(int(value.split()[0]), compressed=True)
    value = value.strip()
    if not value.startswith("["):
        return _COMPONENTS.get(value)
    family = re.match(r"\[\s*(/\w+)", value)
    if family is None:
        return None
    if family.group(1) == "/ICCBased":
        profile = re.search(r"(\d+)\s+\d+\s+R", value)
        if profile is None:
            return None
        kind, count = doc.xref_get_key(int(profile.group(1)), "N")
        return int(count) if kind == "int" else None
    return _COMPONENTS.get(family.group(1))


def _is_plain_jpeg(doc, xref: int) -> bool:
    kind, value = doc.xref_get_key(xref, "Filter")
    return value.replace(" ", "") in ("/DCTDecode", "[/DCTDecode]", "/DCT", "[/DCT]")


def mend_image(doc, xref: int) -> bool:
    """Correct one picture's description if its JPEG data contradicts it. True if changed."""
    if not _is_plain_jpeg(doc, xref):
        return False
    declared = declared_components(doc, xref)
    if declared is None:
        return False
    actual = jpeg_components(doc.xref_stream_raw(xref)[:_HEAD])
    if actual == declared or actual not in _REPLACEMENT:
        return False
    doc.xref_set_key(xref, "ColorSpace", _REPLACEMENT[actual])
    # A /Decode list holds a pair of numbers per component, so it must change length too.
    kind, value = doc.xref_get_key(xref, "Decode")
    if kind == "array":
        numbers = re.findall(r"[-+]?\d*\.?\d+", value)
        pairs = {tuple(numbers[i:i + 2]) for i in range(0, len(numbers), 2)}
        if len(numbers) != 2 * actual:
            if len(pairs) == 1 and len(numbers) % 2 == 0:  # the same for every component
                pair = " ".join(next(iter(pairs)))
                doc.xref_set_key(xref, "Decode", "[" + " ".join([pair] * actual) + "]")
            else:
                doc.xref_set_key(xref, "Decode", "null")
    return True


def mend_page(doc, page: int, checked: set[int] | None = None) -> int:
    """Mend the pictures one page shows. `checked` remembers pictures already looked
    at, so a picture shared by many pages is examined once. Returns how many changed.

    Never raises: a page whose pictures cannot be examined is simply drawn as it is.
    """
    mended = 0
    try:
        if not doc.is_pdf:
            return 0
        pictures = doc.get_page_images(page, full=True)
    except Exception:
        return 0
    for picture in pictures:
        xref = picture[0]
        if xref <= 0 or (checked is not None and xref in checked):
            continue
        if checked is not None:
            checked.add(xref)
        try:
            mended += mend_image(doc, xref)
        except Exception:
            continue
    return mended


def mend_document(doc, checked: set[int] | None = None) -> int:
    """Mend every page's pictures. Returns how many pictures changed."""
    checked = set() if checked is None else checked
    try:
        count = doc.page_count if doc.is_pdf else 0
    except Exception:
        return 0
    return sum(mend_page(doc, page, checked) for page in range(count))
