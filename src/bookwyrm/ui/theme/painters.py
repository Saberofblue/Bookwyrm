"""Hand-painted library furniture: planks, covers, bookends and brass plaques."""

from __future__ import annotations

import zlib
from functools import cache

from PySide6.QtCore import QPointF, QRect, QRectF, Qt
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFontMetrics,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QTransform,
)

from bookwyrm.ui.theme import icons, palette, theme

# Leather colors for tomes without a cover image, chosen by title so each stays the same.
_LEATHERS = ("#5a2a2a", "#2f4a3a", "#2c3e5e", "#5b4426", "#4a2f52", "#374650", "#6a3d24")


@cache
def _texture(name: str) -> QPixmap:
    pixmap = QPixmap(str(theme.ASSETS / "textures" / f"{name}.jpg"))
    if pixmap.isNull():  # keep painting even if an asset is missing
        pixmap = QPixmap(64, 64)
        pixmap.fill(QColor(palette.WALNUT_500))
    return pixmap


def _wood_brush(offset: QPointF, scale: float = 0.5) -> QBrush:
    brush = QBrush(_texture("dark_wood"))
    brush.setTransform(QTransform.fromScale(scale, scale).translate(offset.x(), offset.y()))
    return brush


def draw_back_panel(painter: QPainter, rect: QRect, scroll: int = 0) -> None:
    """The shadowed back of the bookcase, behind the tomes."""
    painter.fillRect(rect, _wood_brush(QPointF(120, -scroll * 2 + 300), 0.9))
    painter.fillRect(rect, QColor(8, 5, 3, 205))


def draw_post(painter: QPainter, rect: QRect) -> None:
    """A vertical side post of the bookcase."""
    painter.save()
    painter.translate(rect.left(), rect.top())
    painter.rotate(90)
    painter.translate(0, -rect.width())
    local = QRect(0, 0, rect.height(), rect.width())
    painter.fillRect(local, _wood_brush(QPointF(40, 90), 0.55))
    painter.restore()
    shade = QLinearGradient(rect.left(), 0, rect.right(), 0)
    shade.setColorAt(0.0, QColor(0, 0, 0, 120))
    shade.setColorAt(0.45, QColor(255, 220, 180, 22))
    shade.setColorAt(1.0, QColor(0, 0, 0, 150))
    painter.fillRect(rect, shade)


def draw_plank(painter: QPainter, rect: QRect, seed: int = 0) -> None:
    """A shelf board seen from the front, lit from above."""
    painter.fillRect(rect, _wood_brush(QPointF(seed * 173 % 900, seed * 97 % 700)))
    shade = QLinearGradient(0, rect.top(), 0, rect.bottom())
    shade.setColorAt(0.0, QColor(255, 225, 190, 70))
    shade.setColorAt(0.12, QColor(255, 225, 190, 18))
    shade.setColorAt(0.75, QColor(0, 0, 0, 40))
    shade.setColorAt(1.0, QColor(0, 0, 0, 150))
    painter.fillRect(rect, shade)
    painter.setPen(QPen(QColor(255, 235, 205, 90), 1))
    painter.drawLine(rect.topLeft(), rect.topRight())
    # the shadow the board casts on the tomes below
    under = QRect(rect.left(), rect.bottom() + 1, rect.width(), 14)
    cast = QLinearGradient(0, under.top(), 0, under.bottom())
    cast.setColorAt(0.0, QColor(0, 0, 0, 150))
    cast.setColorAt(1.0, QColor(0, 0, 0, 0))
    painter.fillRect(under, cast)


def draw_rail(painter: QPainter, rect: QRect, seed: int = 0) -> None:
    """The carved rail above a shelf that carries its nameplate."""
    painter.fillRect(rect, _wood_brush(QPointF(seed * 131 % 800 + 200, seed * 61 % 500 + 40)))
    shade = QLinearGradient(0, rect.top(), 0, rect.bottom())
    shade.setColorAt(0.0, QColor(0, 0, 0, 90))
    shade.setColorAt(0.2, QColor(255, 225, 190, 30))
    shade.setColorAt(0.85, QColor(0, 0, 0, 70))
    shade.setColorAt(1.0, QColor(0, 0, 0, 170))
    painter.fillRect(rect, shade)
    painter.setPen(QPen(QColor(0, 0, 0, 120), 1))
    painter.drawLine(rect.bottomLeft(), rect.bottomRight())


def plaque_width(text: str, size: int = 12) -> int:
    metrics = QFontMetrics(theme.display_font(size, spacing=1.5))
    return metrics.horizontalAdvance(text) + 34


def draw_plaque(painter: QPainter, rect: QRect, text: str, size: int = 12,
                highlighted: bool = False) -> None:
    """An engraved brass nameplate with two tiny screws."""
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    body = QRectF(rect).adjusted(0.5, 0.5, -0.5, -0.5)
    brass = QLinearGradient(0, body.top(), 0, body.bottom())
    brass.setColorAt(0.0, QColor("#f4d999" if not highlighted else "#fff0c4"))
    brass.setColorAt(0.5, QColor(palette.AMBER_400))
    brass.setColorAt(1.0, QColor(palette.AMBER_700))
    painter.setPen(QPen(QColor(40, 26, 8, 220), 1))
    painter.setBrush(brass)
    painter.drawRoundedRect(body, 3, 3)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(70, 46, 14, 200))
    for x in (body.left() + 7, body.right() - 7):
        painter.drawEllipse(QPointF(x, body.center().y()), 1.6, 1.6)
    font = theme.display_font(size, spacing=1.5)
    painter.setFont(font)
    inner = rect.adjusted(14, 0, -14, 0)
    elided = QFontMetrics(font).elidedText(text, Qt.TextElideMode.ElideRight, inner.width())
    painter.setPen(QColor(255, 244, 214, 150))
    painter.drawText(inner.translated(0, 1), Qt.AlignmentFlag.AlignCenter, elided)
    painter.setPen(QColor(54, 34, 8))
    painter.drawText(inner, Qt.AlignmentFlag.AlignCenter, elided)
    painter.restore()


def draw_bookend(painter: QPainter, rect: QRect, facing_right: bool) -> None:
    """A pewter bookend: an upright plate against the tomes, braced from behind.

    `facing_right` is the left-hand bookend of a set (its tomes stand to its right).
    """
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    r = QRectF(rect).adjusted(0.5, 0.5, -0.5, -0.5)
    plate = 4.5
    path = QPainterPath()
    if facing_right:
        path.moveTo(r.right(), r.top())
        path.lineTo(r.right() - plate, r.top())
        path.lineTo(r.left(), r.bottom() - 3)
        path.lineTo(r.left(), r.bottom())
        path.lineTo(r.right(), r.bottom())
    else:
        path.moveTo(r.left(), r.top())
        path.lineTo(r.left() + plate, r.top())
        path.lineTo(r.right(), r.bottom() - 3)
        path.lineTo(r.right(), r.bottom())
        path.lineTo(r.left(), r.bottom())
    path.closeSubpath()
    metal = QLinearGradient(r.left(), r.top(), r.right(), r.bottom())
    metal.setColorAt(0.0, QColor("#e3e9f1"))
    metal.setColorAt(0.45, QColor("#aab4c2"))
    metal.setColorAt(1.0, QColor("#5d6775"))
    painter.setPen(QPen(QColor(18, 22, 28, 220), 1))
    painter.setBrush(metal)
    painter.drawPath(path)
    edge = r.right() - plate / 2 if facing_right else r.left() + plate / 2
    painter.setPen(QPen(QColor(255, 255, 255, 110), 1))
    painter.drawLine(QPointF(edge, r.top() + 2), QPointF(edge, r.bottom() - 2))
    painter.restore()


def leather_for(title: str) -> QColor:
    return QColor(_LEATHERS[zlib.crc32(title.encode("utf-8")) % len(_LEATHERS)])


def draw_blank_cover(painter: QPainter, rect: QRect, title: str) -> None:
    """A leather-bound cover for a tome that has no cover image."""
    painter.save()
    painter.setClipRect(rect)
    brush = QBrush(_texture("brown_leather"))
    brush.setTransform(QTransform.fromScale(0.35, 0.35))
    painter.fillRect(rect, brush)
    tint = leather_for(title)
    tint.setAlpha(170)
    painter.fillRect(rect, tint)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    gilt = QColor(palette.AMBER_300)
    gilt.setAlpha(190)
    painter.setPen(QPen(gilt, 1))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawRect(QRectF(rect).adjusted(7.5, 7.5, -7.5, -7.5))
    crest = min(rect.width() - 36, 44)
    if crest > 16:
        painter.drawPixmap(rect.center().x() - crest // 2, rect.bottom() - crest - 16,
                           icons.pixmap("spiked-dragon-head", crest, gilt.name()))
    painter.setFont(theme.display_font(11, spacing=0.5))
    painter.setPen(QColor(palette.AMBER_300))
    text_rect = rect.adjusted(13, 16, -13, -crest - 26)
    painter.drawText(text_rect, Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop
                     | Qt.TextFlag.TextWordWrap, title)
    painter.restore()


def draw_no_text_mark(painter: QPainter, rect: QRect) -> None:
    """A small crossed-out magnifying glass in the foot of a cover: this tome's pages are
    pictures, so there is no text in it to search."""
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    size = max(16, min(22, rect.width() // 5))
    badge = QRectF(rect.right() - size - 4, rect.bottom() - size - 4, size, size)
    painter.setPen(QPen(QColor(0, 0, 0, 170), 1))
    painter.setBrush(QColor(15, 19, 27, 215))
    painter.drawEllipse(badge)
    unit = size / 20.0
    centre = badge.center()
    amber = QColor(palette.AMBER_300)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.setPen(QPen(amber, 1.6 * unit, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
    lens = QPointF(centre.x() - 1.5 * unit, centre.y() - 1.5 * unit)
    painter.drawEllipse(lens, 3.6 * unit, 3.6 * unit)
    painter.drawLine(QPointF(lens.x() + 2.7 * unit, lens.y() + 2.7 * unit),
                     QPointF(centre.x() + 5 * unit, centre.y() + 5 * unit))
    painter.setPen(QPen(QColor(palette.DANGER), 1.6 * unit, Qt.PenStyle.SolidLine,
                        Qt.PenCapStyle.RoundCap))
    painter.drawLine(QPointF(centre.x() + 5.5 * unit, centre.y() - 5.5 * unit),
                     QPointF(centre.x() - 5.5 * unit, centre.y() + 5.5 * unit))
    painter.restore()


def draw_cover(painter: QPainter, rect: QRect, cover: QPixmap | None, title: str,
               selected: bool = False, dimmed: bool = False) -> None:
    """A tome standing face-out on the shelf."""
    painter.save()
    # contact shadow on the board and a soft shadow to the right
    shadow = QLinearGradient(rect.right(), 0, rect.right() + 12, 0)
    shadow.setColorAt(0.0, QColor(0, 0, 0, 130))
    shadow.setColorAt(1.0, QColor(0, 0, 0, 0))
    painter.fillRect(QRect(rect.right() + 1, rect.top() + 5, 12, rect.height() - 5), shadow)
    if cover is not None and not cover.isNull():
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.drawPixmap(rect, cover)
    else:
        draw_blank_cover(painter, rect, title)
    # the spine edge catches the light; the hinge is a thin dark groove
    spine = QLinearGradient(rect.left(), 0, rect.left() + 12, 0)
    spine.setColorAt(0.0, QColor(255, 255, 255, 70))
    spine.setColorAt(0.35, QColor(0, 0, 0, 60))
    spine.setColorAt(0.55, QColor(255, 255, 255, 26))
    spine.setColorAt(1.0, QColor(255, 255, 255, 0))
    painter.fillRect(QRect(rect.left(), rect.top(), 12, rect.height()), spine)
    painter.setPen(QPen(QColor(0, 0, 0, 150), 1))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawRect(rect.adjusted(0, 0, -1, -1))
    if dimmed:
        painter.fillRect(rect, QColor(15, 19, 27, 150))
    if selected:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        glow = QColor(palette.FROST_500)
        for width, alpha in ((7, 50), (4, 110), (2, 255)):
            glow.setAlpha(alpha)
            painter.setPen(QPen(glow, width))
            painter.drawRoundedRect(QRectF(rect).adjusted(-2, -2, 2, 2), 3, 3)
    painter.restore()
