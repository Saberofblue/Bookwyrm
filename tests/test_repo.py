import sqlite3

import pytest

from bookwyrm.db.repo import Library, LibraryError, fts_query


@pytest.fixture
def library(tmp_path):
    lib = Library(tmp_path / "lib")
    yield lib
    lib.close()


def add_book(library, n=1, **extra):
    fields = dict(title=f"Tome {n}", author="", filename=f"t{n}.pdf", sha256=f"hash{n}",
                  page_count=10, file_size=1000)
    fields.update(extra)
    book = library.add_book(**fields)
    library.mark_ready(book.id)
    return library.book(book.id)


def test_three_levels_and_no_deeper(library):
    case = library.add_place("bookcase", "D&D")
    shelf = library.add_place("shelf", "Heliana", case.id)
    a_set = library.add_place("set", "Hunts DLC", shelf.id)
    assert [p.name for p in library.path(a_set.id)] == ["D&D", "Heliana", "Hunts DLC"]
    with pytest.raises(LibraryError):
        library.add_place("set", "Nested", a_set.id)  # nothing goes inside a set
    with pytest.raises(LibraryError):
        library.add_place("set", "Wrong parent", case.id)  # a set needs a shelf
    with pytest.raises(LibraryError):
        library.add_place("shelf", "Orphan")  # a shelf needs a bookcase
    with pytest.raises(LibraryError):
        library.add_place("bookcase", "Inside", case.id)
    with pytest.raises(LibraryError):
        library.add_place("bookcase", "   ")


def test_database_itself_enforces_the_hierarchy(library):
    case = library.add_place("bookcase", "A")
    with pytest.raises(sqlite3.IntegrityError):
        library.db.execute(
            "INSERT INTO places (kind, parent_id, name) VALUES ('set', ?, 'bad')", (case.id,))
    with pytest.raises(sqlite3.IntegrityError):
        library.db.execute("INSERT INTO places (kind, parent_id, name) VALUES ('shelf', NULL, 'x')")


def test_children_keep_their_order_and_can_be_reordered(library):
    case = library.add_place("bookcase", "A")
    one = library.add_place("shelf", "One", case.id)
    two = library.add_place("shelf", "Two", case.id)
    three = library.add_place("shelf", "Three", case.id)
    library.move_place(three.id, case.id, 0)
    assert [p.name for p in library.children(case.id)] == ["Three", "One", "Two"]
    other = library.add_place("bookcase", "B")
    library.move_place(one.id, other.id)
    assert [p.name for p in library.children(other.id)] == ["One"]
    assert [p.id for p in library.children(case.id)] == [three.id, two.id]
    with pytest.raises(LibraryError):
        library.move_place(one.id, None)  # a shelf cannot become top-level


def test_a_tome_can_stand_in_several_places(library):
    case = library.add_place("bookcase", "A")
    shelf = library.add_place("shelf", "S", case.id)
    a_set = library.add_place("set", "Set", shelf.id)
    book = add_book(library)
    assert [b.id for b in library.unshelved_books()] == [book.id]
    library.place_book(book.id, shelf.id)
    library.place_book(book.id, a_set.id)
    library.place_book(book.id, a_set.id)  # twice is harmless
    assert {p.id for p in library.placements(book.id)} == {shelf.id, a_set.id}
    assert library.unshelved_books() == []
    assert [b.id for b in library.books_in(case.id)] == []
    assert [b.id for b in library.books_in(case.id, recursive=True)] == [book.id]
    assert library.counts() == {shelf.id: 1, a_set.id: 1}


def test_moving_a_tome(library):
    case = library.add_place("bookcase", "A")
    one = library.add_place("shelf", "One", case.id)
    two = library.add_place("shelf", "Two", case.id)
    book = add_book(library)
    library.place_book(book.id, one.id)
    library.move_book(book.id, one.id, two.id)
    assert [p.id for p in library.placements(book.id)] == [two.id]
    library.move_book(book.id, None, one.id)  # from the Returns Cart: nothing to remove
    assert {p.id for p in library.placements(book.id)} == {one.id, two.id}


def test_deleting_a_bookcase_unshelves_but_keeps_tomes(library):
    case = library.add_place("bookcase", "A")
    shelf = library.add_place("shelf", "S", case.id)
    library.add_place("set", "Set", shelf.id)
    book = add_book(library)
    library.place_book(book.id, shelf.id)
    library.delete_place(case.id)
    assert library.places() == []
    assert library.book(book.id) is not None
    assert [b.id for b in library.unshelved_books()] == [book.id]


def test_duplicate_detection_by_hash(library):
    book = add_book(library, sha256="abc")
    assert library.find_by_hash("abc").id == book.id
    assert library.find_by_hash("nope") is None
    with pytest.raises(sqlite3.IntegrityError):
        add_book(library, 2, sha256="abc")


def test_deleting_a_tome_removes_files_and_text(library):
    book = add_book(library)
    library.book_dir(book.id).mkdir(parents=True)
    library.book_file(book).write_bytes(b"%PDF")
    library.cover_file(book.id).write_bytes(b"jpg")
    library.add_page_text(book.id, [(0, "silver dragons hoard books")])
    assert library.search("dragons")
    library.delete_book(book.id)
    assert library.book(book.id) is None
    assert not library.book_dir(book.id).exists()
    assert not library.cover_file(book.id).exists()
    assert library.search("dragons") == []


def test_unfinished_imports_are_discarded(library):
    half = library.add_book(title="Half", author="", filename="h.pdf", sha256="h",
                            page_count=1, file_size=1)
    done = add_book(library, 2)
    assert library.discard_unfinished() == [half.id]
    assert [b.id for b in library.all_books()] == [done.id]


def test_reading_state_and_recents(library):
    first, second = add_book(library, 1), add_book(library, 2)
    library.save_reading_state(first.id, 41, 12.5, 1.5, "custom")
    saved = library.book(first.id)
    assert (saved.last_page, saved.last_offset, saved.last_zoom, saved.last_zoom_mode) == \
        (41, 12.5, 1.5, "custom")
    assert library.recent_books() == []
    library.mark_opened(second.id)
    assert [b.id for b in library.recent_books()] == [second.id]


def test_labels(library):
    book = add_book(library)
    library.set_labels(book.id, ["Monsters", " magic  items ", "monsters", ""])
    assert library.labels_of(book.id) == ["magic items", "Monsters"]
    library.set_labels(book.id, ["Maps"])
    assert library.all_labels() == ["Maps"]  # unused labels disappear


def test_update_book_guards(library):
    book = add_book(library)
    library.update_book(book.id, title="New", notes="n")
    assert library.book(book.id).title == "New"
    with pytest.raises(LibraryError):
        library.update_book(book.id, title="  ")
    with pytest.raises(ValueError):
        library.update_book(book.id, sha256="x")


def test_search_finds_pages_with_snippets(library):
    one, two = add_book(library, 1), add_book(library, 2)
    library.add_page_text(one.id, [(0, "The silver dragon guards its hoard."),
                                   (1, "Wyverns are lesser cousins of dragons.")])
    library.add_page_text(two.id, [(4, "Harvesting a dragon's scales requires care.")])
    hits = library.search("dragon")
    assert {(h.book_id, h.page) for h in hits} == {(one.id, 0), (one.id, 1), (two.id, 4)}
    assert "\x02" in hits[0].snippet and "\x03" in hits[0].snippet
    assert [(h.book_id, h.page) for h in library.search('"silver dragon"')] == [(one.id, 0)]
    assert [(h.book_id, h.page) for h in library.search("harvest scal")] == [(two.id, 4)]
    assert library.search("") == [] and library.search('"') == []
    library.clear_text(one.id)
    assert {h.book_id for h in library.search("dragon")} == {two.id}


@pytest.mark.parametrize("typed, query", [
    ("dragon", '"dragon"*'),
    ("silver dragon", '"silver" "dragon"*'),
    ("silver ", '"silver"'),
    ('"silver dragon" hoard', '"silver dragon" "hoard"*'),
    ("AND OR (", '"AND" "OR"'),
    ("   ", ""),
])
def test_fts_query_is_always_safe(typed, query):
    assert fts_query(typed) == query


def test_changes_are_announced(library):
    heard = []
    library.subscribe(heard.append)
    case = library.add_place("bookcase", "A")
    book = add_book(library)
    library.place_book(book.id, case.id)
    assert "places" in heard and "books" in heard


def test_reopening_keeps_everything(tmp_path):
    first = Library(tmp_path / "lib")
    case = first.add_place("bookcase", "Kept")
    first.close()
    again = Library(tmp_path / "lib")
    assert [p.id for p in again.places()] == [case.id]
    again.close()


def test_sweep_clears_leftovers_of_removed_tomes(library):
    kept = add_book(library)
    library.book_dir(kept.id).mkdir(parents=True)
    library.cover_file(kept.id).write_bytes(b"jpg")
    orphan = library.root / "books" / "999"
    orphan.mkdir(parents=True)
    (orphan / "left-open.pdf").write_bytes(b"%PDF")
    stray_cover = library.root / "covers" / "999.jpg"
    stray_cover.write_bytes(b"jpg")
    other = library.root / "books" / "notes"  # not Bookwyrm's: left alone
    other.mkdir()
    removed = library.sweep()
    assert sorted(removed) == sorted([str(orphan), str(stray_cover)])
    assert not orphan.exists() and not stray_cover.exists()
    assert library.book_dir(kept.id).exists() and library.cover_file(kept.id).exists()
    assert other.exists()
