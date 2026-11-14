"""SQLite connection and schema migrations."""

from __future__ import annotations

import sqlite3
from importlib import resources
from pathlib import Path

SCHEMA_VERSION = 1


def connect(path: Path | str) -> sqlite3.Connection:
    con = sqlite3.connect(str(path))
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA journal_mode = WAL")
    con.execute("PRAGMA synchronous = NORMAL")
    _migrate(con)
    return con


def _migrate(con: sqlite3.Connection) -> None:
    version = con.execute("PRAGMA user_version").fetchone()[0]
    if version > SCHEMA_VERSION:
        raise RuntimeError(
            f"This library was made by a newer Bookwyrm (schema {version}); "
            f"this version understands up to {SCHEMA_VERSION}."
        )
    if version == 0:
        schema = resources.files("bookwyrm.db").joinpath("schema.sql").read_text("utf-8")
        con.executescript(schema)
        con.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        con.commit()
