"""SQLite connection and schema migrations."""

from __future__ import annotations

import os
import sqlite3
from importlib import resources
from pathlib import Path

SCHEMA_VERSION = 3
BACKUP_NAME = "bookwyrm-before-upgrade.db"  # the catalogue as it was before the last upgrade

# A page counts as holding text when it has this many characters besides spaces and line
# breaks: a scanned page may still carry a stray page number or stamp.
TEXT_MIN = 20
_SOLID = ("replace(replace(replace(replace(text, ' ', ''), char(10), ''), char(13), ''), "
          "char(9), '')")

# How to bring a catalogue from each older version up by one. Applied in order, each
# inside one transaction, so a failed upgrade leaves the catalogue as it was.
UPGRADES = {
    1: "ALTER TABLE places ADD COLUMN collapsed INTEGER NOT NULL DEFAULT 0",
    # From 2 to 3: how many pages of each tome hold text (so tomes that are only pictures of
    # pages can be pointed out), counted here once for the tomes already read; and a place to
    # keep the numbers printed on pages, which the app fills in by examining each tome.
    2: f"""
        ALTER TABLE books ADD COLUMN text_pages INTEGER;
        CREATE TABLE page_labels (book_id INTEGER PRIMARY KEY
                                  REFERENCES books(id) ON DELETE CASCADE, labels TEXT NOT NULL);
        CREATE TEMP TABLE counted AS
            SELECT book_id, COUNT(*) AS pages FROM pages_fts
            WHERE length({_SOLID}) >= {TEXT_MIN} GROUP BY book_id;
        UPDATE books SET text_pages = COALESCE(
            (SELECT pages FROM counted WHERE counted.book_id = books.id), 0)
            WHERE indexed = 1;
        DROP TABLE counted
    """,
}


def has_text(text: str) -> bool:
    """Does a page's extracted text amount to real text? (The same rule as the upgrade.)"""
    solid = 0
    for character in text:
        if character not in " \n\r\t":
            solid += 1
            if solid >= TEXT_MIN:
                return True
    return False


def connect(path: Path | str) -> sqlite3.Connection:
    con = sqlite3.connect(str(path))
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA journal_mode = WAL")
    con.execute("PRAGMA synchronous = NORMAL")
    _migrate(con, Path(path))
    return con


def _keep_copy(con: sqlite3.Connection, path: Path) -> None:
    """Before upgrading a catalogue, keep a copy of it as it is.

    An upgraded catalogue cannot be opened by an older Bookwyrm. Should someone
    need to go back, putting this copy in place of bookwyrm.db gives the older
    version the catalogue it understands (without what was done since). Only the
    copy from the latest upgrade is kept. A copy that cannot be made (a full disk)
    does not stop the upgrade, which is safe by itself.
    """
    target = path.with_name(BACKUP_NAME)
    partial = path.with_name(BACKUP_NAME + ".partial")
    try:
        partial.unlink(missing_ok=True)
        copy = sqlite3.connect(str(partial))
        try:
            con.backup(copy)
        finally:
            copy.close()
        os.replace(partial, target)
    except (sqlite3.Error, OSError):
        try:
            partial.unlink(missing_ok=True)
        except OSError:
            pass


def _migrate(con: sqlite3.Connection, path: Path) -> None:
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
        return
    if version < SCHEMA_VERSION:
        _keep_copy(con, path)
    while version < SCHEMA_VERSION:  # an older catalogue: upgrade it step by step
        try:
            con.executescript(
                f"BEGIN;\n{UPGRADES[version]};\nPRAGMA user_version = {version + 1};\nCOMMIT;")
        except Exception:
            if con.in_transaction:
                con.rollback()
            raise
        version += 1
