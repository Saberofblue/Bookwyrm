"""The library catalogue: tomes, and the bookcases, shelves and sets they stand in.

Qt-free so it can be tested and reused anywhere. Interested parties call
``subscribe`` to hear about changes.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from bookwyrm.db import database

CHILD_KIND = {"bookcase": "shelf", "shelf": "set"}
PARENT_KIND = {"shelf": "bookcase", "set": "shelf"}
KIND_LABEL = {"bookcase": "Bookcase", "shelf": "Shelf", "set": "Set"}

_BOOK_FIELDS = {"title", "author", "notes", "page_count", "file_size", "file_hash", "edited"}


@dataclass(frozen=True)
class Place:
    id: int
    kind: str
    parent_id: int | None
    name: str
    sort_order: int
    color: str | None
    collapsed: bool = False  # a shelf shown minimized: just its name and count


@dataclass(frozen=True)
class Book:
    id: int
    title: str
    author: str
    filename: str
    sha256: str
    page_count: int
    file_size: int
    added_at: str
    last_opened_at: str | None
    last_page: int
    last_offset: float
    last_zoom: float
    last_zoom_mode: str
    notes: str
    ready: bool
    indexed: bool
    edited: bool


@dataclass(frozen=True)
class Ribbon:
    id: int
    book_id: int
    page: int  # 0-based
    label: str


@dataclass(frozen=True)
class StudyTable:
    id: int
    name: str


@dataclass(frozen=True)
class Pin:
    id: int
    table_id: int
    book_id: int
    page: int  # 0-based
    rect: tuple[float, float, float, float] | None  # region in page points, if any
    label: str
    thumb: bytes | None  # small PNG/JPEG of the page or region


@dataclass(frozen=True)
class Excerpt:
    id: int
    book_id: int | None  # None when the source tome has left the library
    page: int | None
    rect: tuple[float, float, float, float] | None
    image_path: str  # file name inside excerpts/
    note: str
    created_at: str


@dataclass(frozen=True)
class Hit:
    book_id: int
    page: int  # 0-based
    snippet: str  # matches wrapped in \x02 ... \x03


class LibraryError(Exception):
    """A request that breaks the library's rules, phrased for the user."""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _place(row) -> Place:
    return Place(row["id"], row["kind"], row["parent_id"], row["name"], row["sort_order"],
                 row["color"], bool(row["collapsed"]))


def _rect(row):
    if row["x0"] is None:
        return None
    return (row["x0"], row["y0"], row["x1"], row["y1"])


def _book(row) -> Book:
    return Book(
        id=row["id"], title=row["title"], author=row["author"], filename=row["filename"],
        sha256=row["sha256"], page_count=row["page_count"], file_size=row["file_size"],
        added_at=row["added_at"], last_opened_at=row["last_opened_at"],
        last_page=row["last_page"], last_offset=row["last_offset"], last_zoom=row["last_zoom"],
        last_zoom_mode=row["last_zoom_mode"], notes=row["notes"], ready=bool(row["ready"]),
        indexed=bool(row["indexed"]), edited=bool(row["edited"]),
    )


def fts_query(text: str) -> str:
    """Turn what a person typed into a safe FTS5 query.

    Quoted phrases stay phrases; every other word must appear, and the last bare
    word matches as a prefix so results appear while typing.
    """
    terms = []
    last_is_bare_word = False
    for phrase, word in re.findall(r'"([^"]+)"|(\S+)', text):
        token = (phrase or word).replace('"', " ").strip()
        last_is_bare_word = False
        if token and re.search(r"\w", token):
            terms.append(f'"{token}"')
            last_is_bare_word = bool(word)
    if terms and last_is_bare_word and not text.endswith(" "):
        terms[-1] += "*"
    return " ".join(terms)


class Library:
    def __init__(self, root: Path | str):
        self.root = Path(root)
        for sub in ("books", "covers", "excerpts"):
            (self.root / sub).mkdir(parents=True, exist_ok=True)
        self.db = database.connect(self.root / "bookwyrm.db")
        self._listeners: list = []

    def close(self) -> None:
        """Close the catalogue, folding its write-ahead log into the one database file
        so the library folder is complete and safe to move or copy."""
        try:
            self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:
            pass
        self.db.close()

    # -- change notification ------------------------------------------------

    def subscribe(self, callback) -> None:
        """callback(what) with what in {'places', 'books', 'ribbons', 'tables', 'excerpts'}."""
        self._listeners.append(callback)

    def unsubscribe(self, callback) -> None:
        if callback in self._listeners:
            self._listeners.remove(callback)

    def _changed(self, what: str) -> None:
        for callback in list(self._listeners):
            callback(what)

    # -- places ---------------------------------------------------------------

    def places(self) -> list[Place]:
        rows = self.db.execute("SELECT * FROM places ORDER BY sort_order, name COLLATE NOCASE")
        return [_place(r) for r in rows]

    def place(self, place_id: int) -> Place | None:
        row = self.db.execute("SELECT * FROM places WHERE id = ?", (place_id,)).fetchone()
        return _place(row) if row else None

    def children(self, parent_id: int | None) -> list[Place]:
        rows = self.db.execute(
            "SELECT * FROM places WHERE parent_id IS ? ORDER BY sort_order, name COLLATE NOCASE",
            (parent_id,),
        )
        return [_place(r) for r in rows]

    def path(self, place_id: int) -> list[Place]:
        """Bookcase first, the place itself last."""
        chain: list[Place] = []
        current = self.place(place_id)
        while current is not None:
            chain.append(current)
            current = self.place(current.parent_id) if current.parent_id else None
        return chain[::-1]

    def descendants(self, place_id: int) -> list[int]:
        rows = self.db.execute(
            """WITH RECURSIVE tree(id) AS (
                   SELECT id FROM places WHERE parent_id = ?
                   UNION ALL
                   SELECT p.id FROM places p JOIN tree t ON p.parent_id = t.id)
               SELECT id FROM tree""",
            (place_id,),
        )
        return [r[0] for r in rows]

    def add_place(self, kind: str, name: str, parent_id: int | None = None) -> Place:
        name = name.strip()
        if not name:
            raise LibraryError(f"A {kind} needs a name.")
        if kind == "bookcase":
            if parent_id is not None:
                raise LibraryError("A bookcase stands on its own; it cannot go inside anything.")
        else:
            parent = self.place(parent_id) if parent_id is not None else None
            if parent is None or parent.kind != PARENT_KIND[kind]:
                raise LibraryError(f"A {kind} belongs in a {PARENT_KIND[kind]}.")
        order = self.db.execute(
            "SELECT COALESCE(MAX(sort_order), -1) + 1 FROM places WHERE parent_id IS ?",
            (parent_id,),
        ).fetchone()[0]
        with self.db:
            cur = self.db.execute(
                "INSERT INTO places (kind, parent_id, name, sort_order) VALUES (?, ?, ?, ?)",
                (kind, parent_id, name, order),
            )
        self._changed("places")
        return self.place(cur.lastrowid)

    def rename_place(self, place_id: int, name: str) -> None:
        name = name.strip()
        if not name:
            raise LibraryError("A name cannot be empty.")
        with self.db:
            self.db.execute("UPDATE places SET name = ? WHERE id = ?", (name, place_id))
        self._changed("places")

    def set_collapsed(self, place_ids, collapsed: bool) -> None:
        """Minimize shelves (or bring them back) in their bookcase."""
        with self.db:
            self.db.executemany("UPDATE places SET collapsed = ? WHERE id = ?",
                                [(int(collapsed), place_id) for place_id in place_ids])
        self._changed("places")

    def set_place_color(self, place_id: int, color: str | None) -> None:
        with self.db:
            self.db.execute("UPDATE places SET color = ? WHERE id = ?", (color, place_id))
        self._changed("places")

    def delete_place(self, place_id: int) -> None:
        """Remove a place and everything inside it. Tomes are never deleted, only unshelved."""
        with self.db:
            self.db.execute("DELETE FROM places WHERE id = ?", (place_id,))
        self._changed("places")
        self._changed("books")

    def move_place(self, place_id: int, parent_id: int | None, index: int | None = None) -> None:
        """Re-parent and/or reorder. `index` is the position among the new siblings."""
        place = self.place(place_id)
        if place is None:
            return
        if place.kind == "bookcase":
            if parent_id is not None:
                raise LibraryError("A bookcase stands on its own; it cannot go inside anything.")
        else:
            parent = self.place(parent_id) if parent_id is not None else None
            if parent is None or parent.kind != PARENT_KIND[place.kind]:
                raise LibraryError(
                    f"A {place.kind} belongs in a {PARENT_KIND[place.kind]}.")
        siblings = [p.id for p in self.children(parent_id) if p.id != place_id]
        if index is None or index > len(siblings):
            index = len(siblings)
        siblings.insert(max(0, index), place_id)
        with self.db:
            self.db.execute("UPDATE places SET parent_id = ? WHERE id = ?", (parent_id, place_id))
            self.db.executemany("UPDATE places SET sort_order = ? WHERE id = ?",
                                [(i, pid) for i, pid in enumerate(siblings)])
        self._changed("places")

    # -- tomes ------------------------------------------------------------------

    def add_book(self, *, title: str, author: str, filename: str, sha256: str,
                 page_count: int, file_size: int) -> Book:
        with self.db:
            cur = self.db.execute(
                """INSERT INTO books (title, author, filename, sha256, page_count, file_size,
                                      added_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (title, author, filename, sha256, page_count, file_size, _now()),
            )
        self._changed("books")
        return self.book(cur.lastrowid)

    def mark_ready(self, book_id: int) -> None:
        with self.db:
            self.db.execute("UPDATE books SET ready = 1 WHERE id = ?", (book_id,))
        self._changed("books")

    def book(self, book_id: int) -> Book | None:
        row = self.db.execute("SELECT * FROM books WHERE id = ?", (book_id,)).fetchone()
        return _book(row) if row else None

    def find_by_hash(self, sha256: str) -> Book | None:
        row = self.db.execute(
            "SELECT * FROM books WHERE sha256 = ? OR file_hash = ?", (sha256, sha256)
        ).fetchone()
        return _book(row) if row else None

    def all_books(self) -> list[Book]:
        rows = self.db.execute("SELECT * FROM books ORDER BY title COLLATE NOCASE")
        return [_book(r) for r in rows]

    def unshelved_books(self) -> list[Book]:
        rows = self.db.execute(
            """SELECT * FROM books
               WHERE id NOT IN (SELECT book_id FROM book_places)
               ORDER BY added_at DESC, id DESC"""
        )
        return [_book(r) for r in rows]

    def recent_books(self, limit: int = 24) -> list[Book]:
        rows = self.db.execute(
            """SELECT * FROM books WHERE last_opened_at IS NOT NULL
               ORDER BY last_opened_at DESC LIMIT ?""",
            (limit,),
        )
        return [_book(r) for r in rows]

    def books_in(self, place_id: int, recursive: bool = False) -> list[Book]:
        ids = [place_id] + (self.descendants(place_id) if recursive else [])
        marks = ",".join("?" * len(ids))
        rows = self.db.execute(
            f"""SELECT DISTINCT b.*, MIN(bp.sort_order) AS spot FROM books b
                JOIN book_places bp ON bp.book_id = b.id
                WHERE bp.place_id IN ({marks})
                GROUP BY b.id ORDER BY spot, b.title COLLATE NOCASE""",
            ids,
        )
        return [_book(r) for r in rows]

    def counts(self) -> dict[int, int]:
        """Tomes standing directly in each place."""
        rows = self.db.execute("SELECT place_id, COUNT(*) FROM book_places GROUP BY place_id")
        return {r[0]: r[1] for r in rows}

    def update_book(self, book_id: int, **fields) -> None:
        unknown = set(fields) - _BOOK_FIELDS
        if unknown:
            raise ValueError(f"cannot update {sorted(unknown)}")
        if "title" in fields and not str(fields["title"]).strip():
            raise LibraryError("A tome needs a title.")
        if not fields:
            return
        assignments = ", ".join(f"{name} = ?" for name in fields)
        with self.db:
            self.db.execute(f"UPDATE books SET {assignments} WHERE id = ?",
                            (*fields.values(), book_id))
        self._changed("books")

    def delete_book(self, book_id: int) -> None:
        """Remove a tome from the library, including Bookwyrm's copy of the file."""
        with self.db:
            self.db.execute("DELETE FROM pages_fts WHERE book_id = ?", (book_id,))
            self.db.execute("DELETE FROM books WHERE id = ?", (book_id,))
        shutil.rmtree(self.book_dir(book_id), ignore_errors=True)
        self.cover_file(book_id).unlink(missing_ok=True)
        self._changed("books")

    def sweep(self) -> list[str]:
        """Delete leftovers that belong to no tome: folders of removed tomes whose files
        were still in use when they were removed (Windows will not delete an open file),
        and stray covers. Returns what was cleared away."""
        known = {str(r[0]) for r in self.db.execute("SELECT id FROM books")}
        removed = []
        for folder in (self.root / "books").iterdir():
            if folder.is_dir() and folder.name.isdigit() and folder.name not in known:
                shutil.rmtree(folder, ignore_errors=True)
                removed.append(str(folder))
        for cover in (self.root / "covers").glob("*.jpg"):
            if cover.stem.isdigit() and cover.stem not in known:
                cover.unlink(missing_ok=True)
                removed.append(str(cover))
        return removed

    def discard_unfinished(self) -> list[int]:
        """Drop tomes whose import never completed (e.g. the app quit mid-copy)."""
        ids = [r[0] for r in self.db.execute("SELECT id FROM books WHERE ready = 0")]
        for book_id in ids:
            self.delete_book(book_id)
        return ids

    # -- shelving -----------------------------------------------------------------

    def place_book(self, book_id: int, place_id: int) -> None:
        order = self.db.execute(
            "SELECT COALESCE(MAX(sort_order), -1) + 1 FROM book_places WHERE place_id = ?",
            (place_id,),
        ).fetchone()[0]
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO book_places (book_id, place_id, sort_order) VALUES (?,?,?)",
                (book_id, place_id, order),
            )
        self._changed("books")

    def unplace_book(self, book_id: int, place_id: int) -> None:
        with self.db:
            self.db.execute("DELETE FROM book_places WHERE book_id = ? AND place_id = ?",
                            (book_id, place_id))
        self._changed("books")

    def move_book(self, book_id: int, from_place_id: int | None, to_place_id: int) -> None:
        """Take a tome off one spot (if given) and stand it in another."""
        if from_place_id == to_place_id:
            return
        with self.db:
            if from_place_id is not None:
                self.db.execute("DELETE FROM book_places WHERE book_id = ? AND place_id = ?",
                                (book_id, from_place_id))
        self.place_book(book_id, to_place_id)

    def placements(self, book_id: int) -> list[Place]:
        rows = self.db.execute(
            """SELECT p.* FROM places p JOIN book_places bp ON bp.place_id = p.id
               WHERE bp.book_id = ? ORDER BY p.id""",
            (book_id,),
        )
        return [_place(r) for r in rows]

    # -- labels ---------------------------------------------------------------------

    def labels_of(self, book_id: int) -> list[str]:
        rows = self.db.execute(
            """SELECT l.name FROM labels l JOIN book_labels bl ON bl.label_id = l.id
               WHERE bl.book_id = ? ORDER BY l.name COLLATE NOCASE""",
            (book_id,),
        )
        return [r[0] for r in rows]

    def labels_by_book(self) -> dict[int, list[str]]:
        """Every tome's labels in one query, for filtering whole shelves."""
        found: dict[int, list[str]] = {}
        rows = self.db.execute(
            """SELECT bl.book_id, l.name FROM book_labels bl JOIN labels l ON l.id = bl.label_id
               ORDER BY l.name COLLATE NOCASE""")
        for book_id, name in rows:
            found.setdefault(book_id, []).append(name)
        return found

    def all_labels(self) -> list[str]:
        rows = self.db.execute("SELECT name FROM labels ORDER BY name COLLATE NOCASE")
        return [r[0] for r in rows]

    def set_labels(self, book_id: int, names) -> None:
        wanted = []
        for name in names:
            name = " ".join(str(name).split())
            if name and name.lower() not in (w.lower() for w in wanted):
                wanted.append(name)
        with self.db:
            self.db.execute("DELETE FROM book_labels WHERE book_id = ?", (book_id,))
            for name in wanted:
                self.db.execute("INSERT OR IGNORE INTO labels (name) VALUES (?)", (name,))
                self.db.execute(
                    """INSERT INTO book_labels (book_id, label_id)
                       SELECT ?, id FROM labels WHERE name = ? COLLATE NOCASE""",
                    (book_id, name),
                )
            self.db.execute("DELETE FROM labels WHERE id NOT IN (SELECT label_id FROM book_labels)")
        self._changed("books")

    # -- files ----------------------------------------------------------------------

    def book_dir(self, book_id: int) -> Path:
        return self.root / "books" / str(book_id)

    def book_file(self, book: Book) -> Path:
        return self.book_dir(book.id) / book.filename

    def archive_file(self, book_id: int) -> Path:
        return self.book_dir(book_id) / "original.pdf"

    def cover_file(self, book_id: int) -> Path:
        return self.root / "covers" / f"{book_id}.jpg"

    # -- reading state ----------------------------------------------------------------

    def save_reading_state(self, book_id: int, page: int, offset: float, zoom: float,
                           mode: str) -> None:
        with self.db:
            self.db.execute(
                """UPDATE books SET last_page = ?, last_offset = ?, last_zoom = ?,
                                    last_zoom_mode = ? WHERE id = ?""",
                (page, offset, zoom, mode, book_id),
            )

    def mark_opened(self, book_id: int) -> None:
        with self.db:
            self.db.execute("UPDATE books SET last_opened_at = ? WHERE id = ?",
                            (_now(), book_id))
        self._changed("books")

    # -- ribbons (bookmarks) ----------------------------------------------------------

    def ribbons(self, book_id: int) -> list[Ribbon]:
        rows = self.db.execute(
            "SELECT id, book_id, page, label FROM ribbons WHERE book_id = ? ORDER BY page, id",
            (book_id,),
        )
        return [Ribbon(r["id"], r["book_id"], r["page"], r["label"]) for r in rows]

    def add_ribbon(self, book_id: int, page: int, label: str = "") -> Ribbon:
        with self.db:
            cur = self.db.execute(
                "INSERT INTO ribbons (book_id, page, label, created_at) VALUES (?, ?, ?, ?)",
                (book_id, page, label.strip(), _now()),
            )
        self._changed("ribbons")
        return Ribbon(cur.lastrowid, book_id, page, label.strip())

    def rename_ribbon(self, ribbon_id: int, label: str) -> None:
        with self.db:
            self.db.execute("UPDATE ribbons SET label = ? WHERE id = ?",
                            (label.strip(), ribbon_id))
        self._changed("ribbons")

    def delete_ribbon(self, ribbon_id: int) -> None:
        with self.db:
            self.db.execute("DELETE FROM ribbons WHERE id = ?", (ribbon_id,))
        self._changed("ribbons")

    def remap_pages(self, book_id: int, mapping: dict[int, int]) -> None:
        """Follow pages to their new numbers after a tome's pages were rearranged.

        `mapping` is old page -> new page for every page that survived. Ribbons and
        pins on deleted pages are removed; excerpts keep their picture but lose the
        link to a page that no longer exists.
        """
        mapping = {int(old): int(new) for old, new in mapping.items()}
        with self.db:
            for table in ("ribbons", "table_pins", "excerpts"):
                rows = self.db.execute(
                    f"SELECT id, page FROM {table} WHERE book_id = ? AND page IS NOT NULL",
                    (book_id,)).fetchall()
                for row in rows:
                    new = mapping.get(row["page"])
                    if new is not None:
                        if new != row["page"]:
                            self.db.execute(f"UPDATE {table} SET page = ? WHERE id = ?",
                                            (new, row["id"]))
                    elif table == "excerpts":
                        self.db.execute("UPDATE excerpts SET page = NULL WHERE id = ?",
                                        (row["id"],))
                    else:
                        self.db.execute(f"DELETE FROM {table} WHERE id = ?", (row["id"],))
        for what in ("ribbons", "tables", "excerpts"):
            self._changed(what)

    # -- study tables (session prep boards) -------------------------------------------

    def tables(self) -> list[StudyTable]:
        rows = self.db.execute("SELECT id, name FROM study_tables ORDER BY sort_order, id")
        return [StudyTable(r["id"], r["name"]) for r in rows]

    def table(self, table_id: int) -> StudyTable | None:
        row = self.db.execute("SELECT id, name FROM study_tables WHERE id = ?",
                              (table_id,)).fetchone()
        return StudyTable(row["id"], row["name"]) if row else None

    def add_table(self, name: str) -> StudyTable:
        name = name.strip()
        if not name:
            raise LibraryError("A study table needs a name.")
        order = self.db.execute(
            "SELECT COALESCE(MAX(sort_order), -1) + 1 FROM study_tables").fetchone()[0]
        with self.db:
            cur = self.db.execute(
                "INSERT INTO study_tables (name, sort_order, created_at) VALUES (?, ?, ?)",
                (name, order, _now()))
        self._changed("tables")
        return StudyTable(cur.lastrowid, name)

    def rename_table(self, table_id: int, name: str) -> None:
        name = name.strip()
        if not name:
            raise LibraryError("A name cannot be empty.")
        with self.db:
            self.db.execute("UPDATE study_tables SET name = ? WHERE id = ?", (name, table_id))
        self._changed("tables")

    def delete_table(self, table_id: int) -> None:
        with self.db:
            self.db.execute("DELETE FROM study_tables WHERE id = ?", (table_id,))
        self._changed("tables")

    def pins(self, table_id: int) -> list[Pin]:
        rows = self.db.execute(
            "SELECT * FROM table_pins WHERE table_id = ? ORDER BY sort_order, id", (table_id,))
        return [Pin(r["id"], r["table_id"], r["book_id"], r["page"], _rect(r), r["label"],
                    r["thumb"]) for r in rows]

    def pin_counts(self) -> dict[int, int]:
        rows = self.db.execute("SELECT table_id, COUNT(*) FROM table_pins GROUP BY table_id")
        return {r[0]: r[1] for r in rows}

    def add_pin(self, table_id: int, book_id: int, page: int, rect=None, label: str = "",
                thumb: bytes | None = None) -> int:
        x0, y0, x1, y1 = rect if rect else (None, None, None, None)
        order = self.db.execute(
            "SELECT COALESCE(MAX(sort_order), -1) + 1 FROM table_pins WHERE table_id = ?",
            (table_id,)).fetchone()[0]
        with self.db:
            cur = self.db.execute(
                """INSERT INTO table_pins (table_id, book_id, page, x0, y0, x1, y1, label, thumb,
                                           sort_order, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (table_id, book_id, page, x0, y0, x1, y1, label.strip(), thumb, order, _now()))
        self._changed("tables")
        return cur.lastrowid

    def relabel_pin(self, pin_id: int, label: str) -> None:
        with self.db:
            self.db.execute("UPDATE table_pins SET label = ? WHERE id = ?",
                            (label.strip(), pin_id))
        self._changed("tables")

    def delete_pin(self, pin_id: int) -> None:
        with self.db:
            self.db.execute("DELETE FROM table_pins WHERE id = ?", (pin_id,))
        self._changed("tables")

    def reorder_pins(self, table_id: int, pin_ids: list[int]) -> None:
        with self.db:
            self.db.executemany(
                "UPDATE table_pins SET sort_order = ? WHERE id = ? AND table_id = ?",
                [(i, pin_id, table_id) for i, pin_id in enumerate(pin_ids)])
        self._changed("tables")

    # -- excerpts and the commonplace book ----------------------------------------------

    def excerpts(self) -> list[Excerpt]:
        rows = self.db.execute("SELECT * FROM excerpts ORDER BY created_at DESC, id DESC")
        return [Excerpt(r["id"], r["book_id"], r["page"], _rect(r), r["image_path"], r["note"],
                        r["created_at"]) for r in rows]

    def excerpt_file(self, excerpt: Excerpt) -> Path:
        return self.root / "excerpts" / excerpt.image_path

    def add_excerpt(self, png: bytes, book_id: int | None = None, page: int | None = None,
                    rect=None, note: str = "") -> Excerpt:
        """Keep an image clipped from a page. `png` is the encoded picture."""
        x0, y0, x1, y1 = rect if rect else (None, None, None, None)
        with self.db:
            cur = self.db.execute(
                """INSERT INTO excerpts (book_id, page, x0, y0, x1, y1, image_path, note,
                                         created_at)
                   VALUES (?, ?, ?, ?, ?, ?, '', ?, ?)""",
                (book_id, page, x0, y0, x1, y1, note.strip(), _now()))
            name = f"{cur.lastrowid}.png"
            (self.root / "excerpts" / name).write_bytes(png)
            self.db.execute("UPDATE excerpts SET image_path = ? WHERE id = ?",
                            (name, cur.lastrowid))
        self._changed("excerpts")
        return next(e for e in self.excerpts() if e.id == cur.lastrowid)

    def annotate_excerpt(self, excerpt_id: int, note: str) -> None:
        with self.db:
            self.db.execute("UPDATE excerpts SET note = ? WHERE id = ?",
                            (note.strip(), excerpt_id))
        self._changed("excerpts")

    def delete_excerpt(self, excerpt_id: int) -> None:
        row = self.db.execute("SELECT image_path FROM excerpts WHERE id = ?",
                              (excerpt_id,)).fetchone()
        with self.db:
            self.db.execute("DELETE FROM excerpts WHERE id = ?", (excerpt_id,))
        if row and row["image_path"]:
            (self.root / "excerpts" / row["image_path"]).unlink(missing_ok=True)
        self._changed("excerpts")

    # -- full text ----------------------------------------------------------------------

    def add_page_text(self, book_id: int, pages) -> None:
        """pages: iterable of (0-based page, text)."""
        with self.db:
            self.db.executemany(
                "INSERT INTO pages_fts (text, book_id, page) VALUES (?, ?, ?)",
                [(text, book_id, page) for page, text in pages],
            )

    def clear_text(self, book_id: int) -> None:
        with self.db:
            self.db.execute("DELETE FROM pages_fts WHERE book_id = ?", (book_id,))
            self.db.execute("UPDATE books SET indexed = 0 WHERE id = ?", (book_id,))

    def set_indexed(self, book_id: int, indexed: bool = True) -> None:
        with self.db:
            self.db.execute("UPDATE books SET indexed = ? WHERE id = ?", (int(indexed), book_id))
        self._changed("books")

    def unindexed_books(self) -> list[Book]:
        rows = self.db.execute("SELECT * FROM books WHERE ready = 1 AND indexed = 0 ORDER BY id")
        return [_book(r) for r in rows]

    def search(self, text: str, limit: int = 300) -> list[Hit]:
        query = fts_query(text)
        if not query:
            return []
        rows = self.db.execute(
            """SELECT book_id, page,
                      snippet(pages_fts, 0, char(2), char(3), '…', 14) AS snip
               FROM pages_fts WHERE pages_fts MATCH ?
               ORDER BY rank LIMIT ?""",
            (query, limit),
        )
        return [Hit(int(r["book_id"]), int(r["page"]), r["snip"]) for r in rows]
