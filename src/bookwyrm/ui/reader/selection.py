"""Text-selection geometry over a page's word boxes. Pure functions, no Qt.

A word is PyMuPDF's tuple ``(x0, y0, x1, y1, text, block, line, number)``; the
list arrives in reading order, so a selection is simply a slice of it.
"""

from __future__ import annotations

Word = tuple


def nearest_word(words: list[Word], x: float, y: float) -> int | None:
    """Index of the word containing (x, y), else the closest one.

    A point level with a line of text belongs to that line even when it is far
    to the side, so dragging past the end of a short line selects to its end.
    """
    best, best_distance = None, float("inf")
    level, level_distance = None, float("inf")
    for index, (x0, y0, x1, y1, *_rest) in enumerate(words):
        dx = max(x0 - x, 0.0, x - x1)
        dy = max(y0 - y, 0.0, y - y1)
        if dy == 0.0:
            if dx == 0.0:
                return index
            if dx < level_distance:
                level, level_distance = index, dx
        distance = dx + dy * 3.0
        if distance < best_distance:
            best, best_distance = index, distance
    return level if level is not None else best


def word_at(words: list[Word], x: float, y: float, slack: float = 2.0) -> int | None:
    """Index of the word under (x, y), or None if the point is not on a word."""
    for index, (x0, y0, x1, y1, *_rest) in enumerate(words):
        if x0 - slack <= x <= x1 + slack and y0 - slack <= y <= y1 + slack:
            return index
    return None


def line_rects(words: list[Word]) -> list[tuple[float, float, float, float]]:
    """One rectangle per run of selected words on the same line."""
    rects: list[list[float]] = []
    previous = None
    for x0, y0, x1, y1, _text, block, line, _number in words:
        key = (block, line)
        if key == previous:
            rect = rects[-1]
            rect[0], rect[1] = min(rect[0], x0), min(rect[1], y0)
            rect[2], rect[3] = max(rect[2], x1), max(rect[3], y1)
        else:
            rects.append([x0, y0, x1, y1])
            previous = key
    return [tuple(r) for r in rects]


def text_of(words: list[Word]) -> str:
    """The words as text: spaces within a line, newlines between lines, a gap between blocks."""
    out: list[str] = []
    previous = None
    for *_box, text, block, line, _number in words:
        if previous is None:
            pass
        elif previous[0] != block:
            out.append("\n\n")
        elif previous[1] != line:
            out.append("\n")
        else:
            out.append(" ")
        out.append(text)
        previous = (block, line)
    return "".join(out)
