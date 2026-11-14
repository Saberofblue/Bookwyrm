"""Bookwyrm's colors: a silver dragon's library at night.

Stone and midnight for surfaces, silver for text, frost-blue for anything the
user can act on, candle-amber for brass fittings, walnut for the shelves.
"""

STONE_900 = "#0f131b"  # window background
STONE_800 = "#161c28"  # panels
STONE_700 = "#1e2636"  # raised surfaces
STONE_600 = "#293348"  # borders, hover
STONE_500 = "#3a4763"  # strong borders

SILVER_100 = "#eef2f7"
SILVER_300 = "#c9d1dc"  # primary text
SILVER_500 = "#8d98a8"  # muted text
SILVER_700 = "#5b6677"

FROST_300 = "#a9dcf2"
FROST_500 = "#6fbfe3"  # accent
FROST_700 = "#2f6f93"  # selection

AMBER_300 = "#f0cf8a"
AMBER_400 = "#e3b057"  # brass, candlelight
AMBER_700 = "#8a6428"

PARCHMENT = "#f4efe4"
PARCHMENT_DARK = "#e2d6bd"

WALNUT_900 = "#170e09"
WALNUT_700 = "#33210f"
WALNUT_500 = "#5b3d28"

DANGER = "#d9736a"

TOKENS = {name.lower(): value for name, value in globals().items()
          if name.isupper() and isinstance(value, str) and value.startswith("#")}
