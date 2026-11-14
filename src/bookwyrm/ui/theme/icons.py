"""Tinted icons from the bundled SVG set (game-icons.net, CC BY 3.0; see CREDITS.md)."""

from __future__ import annotations

from functools import cache, lru_cache

from PySide6.QtCore import QByteArray, QRectF, QSize, Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

from bookwyrm.ui.theme import palette
from bookwyrm.ui.theme.theme import ASSETS

_BACKGROUND = '<path d="M0 0h512v512H0z"/>'


@cache
def _source(name: str) -> str:
    path = ASSETS / "icons" / f"{name}.svg"
    return path.read_text("utf-8").replace(_BACKGROUND, "")


@lru_cache(maxsize=256)
def pixmap(name: str, size: int, color: str = palette.SILVER_300, dpr: float = 2.0) -> QPixmap:
    svg = _source(name).replace('fill="#fff"', f'fill="{color}"')
    renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
    image = QPixmap(QSize(round(size * dpr), round(size * dpr)))
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    renderer.render(painter, QRectF(0, 0, size * dpr, size * dpr))
    painter.end()
    image.setDevicePixelRatio(dpr)
    return image


@lru_cache(maxsize=128)
def icon(name: str, color: str = palette.SILVER_300, active: str = palette.SILVER_100) -> QIcon:
    result = QIcon()
    for size in (16, 20, 24, 32):
        result.addPixmap(pixmap(name, size, color), QIcon.Mode.Normal)
        result.addPixmap(pixmap(name, size, active), QIcon.Mode.Active)
        result.addPixmap(pixmap(name, size, active), QIcon.Mode.Selected)
        result.addPixmap(pixmap(name, size, palette.SILVER_700), QIcon.Mode.Disabled)
    return result
