"""Loads fonts and applies the stylesheet."""

from __future__ import annotations

import re
from pathlib import Path

from PySide6.QtGui import QColor, QFont, QFontDatabase, QPalette

from bookwyrm.ui.theme import palette

HERE = Path(__file__).parent
ASSETS = HERE / "assets"

# Filled in by apply(); sensible fallbacks keep widgets usable if fonts are missing.
DISPLAY_FONT = "Georgia"
BODY_FONT = "Georgia"


def _load_fonts() -> None:
    global DISPLAY_FONT, BODY_FONT
    families: set[str] = set()
    for path in sorted((HERE / "fonts").glob("*.ttf")):
        font_id = QFontDatabase.addApplicationFont(str(path))
        if font_id >= 0:
            families.update(QFontDatabase.applicationFontFamilies(font_id))
    if "Cinzel" in families:
        DISPLAY_FONT = "Cinzel"
    if "EB Garamond" in families:
        BODY_FONT = "EB Garamond"


def display_font(size: int, weight: int = QFont.Weight.DemiBold, spacing: float = 1.0) -> QFont:
    font = QFont(DISPLAY_FONT)
    font.setPixelSize(size)
    font.setWeight(QFont.Weight(weight))
    font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, spacing)
    return font


def body_font(size: int, italic: bool = False, weight: int = QFont.Weight.Normal) -> QFont:
    font = QFont(BODY_FONT)
    font.setPixelSize(size)
    font.setItalic(italic)
    font.setWeight(QFont.Weight(weight))
    return font


def stylesheet() -> str:
    text = (HERE / "theme.qss").read_text("utf-8")
    tokens = dict(palette.TOKENS, display=DISPLAY_FONT, body=BODY_FONT,
                  assets=ASSETS.as_posix())
    return re.sub(r"@([a-z_0-9]+)", lambda m: tokens.get(m.group(1), m.group(0)), text)


def apply(app) -> None:
    _load_fonts()
    app.setStyle("Fusion")  # a neutral base the stylesheet can fully dress
    colors = QPalette()
    for role, value in (
        (QPalette.ColorRole.Window, palette.STONE_900),
        (QPalette.ColorRole.Base, palette.STONE_800),
        (QPalette.ColorRole.AlternateBase, palette.STONE_700),
        (QPalette.ColorRole.Button, palette.STONE_700),
        (QPalette.ColorRole.WindowText, palette.SILVER_300),
        (QPalette.ColorRole.Text, palette.SILVER_300),
        (QPalette.ColorRole.ButtonText, palette.SILVER_300),
        (QPalette.ColorRole.PlaceholderText, palette.SILVER_700),
        (QPalette.ColorRole.Highlight, palette.FROST_700),
        (QPalette.ColorRole.HighlightedText, palette.SILVER_100),
        (QPalette.ColorRole.ToolTipBase, palette.STONE_700),
        (QPalette.ColorRole.ToolTipText, palette.SILVER_100),
        (QPalette.ColorRole.Link, palette.FROST_500),
    ):
        colors.setColor(role, QColor(value))
    app.setPalette(colors)
    app.setFont(body_font(15))
    app.setStyleSheet(stylesheet())
