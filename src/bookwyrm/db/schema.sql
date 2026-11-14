-- Bookwyrm catalogue, schema version 2.
-- (Version 2 added places.collapsed; database.py upgrades older catalogues in place.)

CREATE TABLE books (
    id              INTEGER PRIMARY KEY,
    title           TEXT    NOT NULL,
    author          TEXT    NOT NULL DEFAULT '',
    filename        TEXT    NOT NULL,              -- inside books/<id>/
    sha256          TEXT    NOT NULL UNIQUE,       -- of the file as imported (duplicate detection)
    file_hash       TEXT,                          -- of the file as last saved by Bookwyrm
    page_count      INTEGER NOT NULL DEFAULT 0,
    file_size       INTEGER NOT NULL DEFAULT 0,
    added_at        TEXT    NOT NULL,
    last_opened_at  TEXT,
    last_page       INTEGER NOT NULL DEFAULT 0,
    last_offset     REAL    NOT NULL DEFAULT 0,
    last_zoom       REAL    NOT NULL DEFAULT 1,
    last_zoom_mode  TEXT    NOT NULL DEFAULT 'fit_width',
    notes           TEXT    NOT NULL DEFAULT '',
    ready           INTEGER NOT NULL DEFAULT 0,    -- 1 once the file is in the library
    indexed         INTEGER NOT NULL DEFAULT 0,    -- 1 once pages_fts holds its text
    edited          INTEGER NOT NULL DEFAULT 0     -- 1 once an archive copy exists
);

-- Bookcase -> Shelf -> Set, and nothing deeper.
CREATE TABLE places (
    id          INTEGER PRIMARY KEY,
    kind        TEXT    NOT NULL CHECK (kind IN ('bookcase', 'shelf', 'set')),
    parent_id   INTEGER REFERENCES places(id) ON DELETE CASCADE,
    name        TEXT    NOT NULL,
    sort_order  INTEGER NOT NULL DEFAULT 0,
    color       TEXT,
    collapsed   INTEGER NOT NULL DEFAULT 0,      -- 1 = shown minimized in its bookcase
    CHECK ((kind = 'bookcase') = (parent_id IS NULL))
);
CREATE INDEX places_parent ON places(parent_id, sort_order);

CREATE TRIGGER places_parent_kind_insert BEFORE INSERT ON places
WHEN NEW.parent_id IS NOT NULL
 AND (SELECT kind FROM places WHERE id = NEW.parent_id)
     IS NOT CASE NEW.kind WHEN 'shelf' THEN 'bookcase' WHEN 'set' THEN 'shelf' END
BEGIN
    SELECT RAISE(ABORT, 'a shelf belongs in a bookcase and a set belongs on a shelf');
END;

CREATE TRIGGER places_parent_kind_update BEFORE UPDATE OF parent_id, kind ON places
WHEN NEW.parent_id IS NOT NULL
 AND (SELECT kind FROM places WHERE id = NEW.parent_id)
     IS NOT CASE NEW.kind WHEN 'shelf' THEN 'bookcase' WHEN 'set' THEN 'shelf' END
BEGIN
    SELECT RAISE(ABORT, 'a shelf belongs in a bookcase and a set belongs on a shelf');
END;

-- A tome may stand in several places; it is stored once.
CREATE TABLE book_places (
    book_id     INTEGER NOT NULL REFERENCES books(id)  ON DELETE CASCADE,
    place_id    INTEGER NOT NULL REFERENCES places(id) ON DELETE CASCADE,
    sort_order  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (book_id, place_id)
);
CREATE INDEX book_places_place ON book_places(place_id, sort_order);

CREATE TABLE labels (
    id    INTEGER PRIMARY KEY,
    name  TEXT NOT NULL UNIQUE COLLATE NOCASE
);
CREATE TABLE book_labels (
    book_id   INTEGER NOT NULL REFERENCES books(id)  ON DELETE CASCADE,
    label_id  INTEGER NOT NULL REFERENCES labels(id) ON DELETE CASCADE,
    PRIMARY KEY (book_id, label_id)
);

CREATE TABLE ribbons (
    id          INTEGER PRIMARY KEY,
    book_id     INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    page        INTEGER NOT NULL,                  -- 0-based
    label       TEXT    NOT NULL DEFAULT '',
    created_at  TEXT    NOT NULL
);
CREATE INDEX ribbons_book ON ribbons(book_id, page);

-- One row per page of text; page is 0-based.
CREATE VIRTUAL TABLE pages_fts USING fts5(
    text, book_id UNINDEXED, page UNINDEXED,
    tokenize = 'porter unicode61 remove_diacritics 2'
);

CREATE TABLE study_tables (
    id          INTEGER PRIMARY KEY,
    name        TEXT    NOT NULL,
    sort_order  INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT    NOT NULL
);
CREATE TABLE table_pins (
    id          INTEGER PRIMARY KEY,
    table_id    INTEGER NOT NULL REFERENCES study_tables(id) ON DELETE CASCADE,
    book_id     INTEGER NOT NULL REFERENCES books(id)        ON DELETE CASCADE,
    page        INTEGER NOT NULL,
    x0 REAL, y0 REAL, x1 REAL, y1 REAL,            -- optional region, page points
    label       TEXT    NOT NULL DEFAULT '',
    thumb       BLOB,
    sort_order  INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT    NOT NULL
);

CREATE TABLE excerpts (
    id          INTEGER PRIMARY KEY,
    book_id     INTEGER REFERENCES books(id) ON DELETE SET NULL,
    page        INTEGER,
    x0 REAL, y0 REAL, x1 REAL, y1 REAL,
    image_path  TEXT    NOT NULL,                  -- inside excerpts/
    note        TEXT    NOT NULL DEFAULT '',
    created_at  TEXT    NOT NULL
);
