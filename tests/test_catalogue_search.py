"""The Card Catalogue's tome-by-tome search, and narrowing it to places and tomes."""

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QMenu

from bookwyrm.db import search
from bookwyrm.db.repo import Library
from bookwyrm.library.importer import Importer
from bookwyrm.ui import prefs
from bookwyrm.ui.extras import search_scope
from bookwyrm.ui.extras.card_catalogue import (
    EAGER_ROWS,
    INFO,
    KIND,
    PER_TOME,
    ROLE,
    SNIPPET,
    CardCatalogue,
    find_needle,
    lead_in,
    segments,
)
from bookwyrm.ui.extras.search_scope import Scope, ScopePicker, scope_of
from bookwyrm.ui.keys import menu_text
from bookwyrm.ui.library.library_page import LibraryPage

Checked, Unchecked, Partly = (Qt.CheckState.Checked, Qt.CheckState.Unchecked,
                              Qt.CheckState.PartiallyChecked)


@pytest.fixture
def library(tmp_path):
    lib = Library(tmp_path / "lib")
    yield lib
    lib.close()


def add_tome(library, title, pages, place_id=None):
    """A ready, indexed tome whose page texts are `pages` (a list, or {page: text})."""
    number = len(library.all_books()) + 1
    book = library.add_book(title=title, author="", filename=f"t{number}.pdf",
                            sha256=f"hash{number}", page_count=max(1, len(pages)), file_size=1)
    library.mark_ready(book.id)
    texts = pages.items() if isinstance(pages, dict) else enumerate(pages)
    library.add_page_text(book.id, list(texts))
    library.set_indexed(book.id)
    if place_id is not None:
        library.place_book(book.id, place_id)
    return library.book(book.id)


@pytest.fixture
def shelves(library):
    """Two bookcases; the big tome mentions dragons on every one of 450 pages."""
    dnd = library.add_place("bookcase", "D&D")
    hunts = library.add_place("shelf", "Hunts", dnd.id)
    core = library.add_place("set", "Core", hunts.id)
    wars = library.add_place("bookcase", "Star Wars")
    big = add_tome(library, "Bestiary", [f"Entry {n}: a dragon of some kind." for n in range(450)],
                   hunts.id)
    small = add_tome(library, "Almanac", {3: "One dragon is mentioned in passing."}, core.id)
    other = add_tome(library, "Smugglers", ["A freighter, not a dragon.", "Blasters."], wars.id)
    loose = add_tome(library, "Pamphlet", ["Nothing but a dragon turtle."])
    return dict(dnd=dnd, hunts=hunts, core=core, wars=wars, big=big, small=small, other=other,
                loose=loose)


# -- the search itself ---------------------------------------------------------------------


def test_every_tome_is_found_however_many_pages_another_has(library, shelves):
    tomes = library.search_tomes("dragon")
    totals = {tome.book_id: tome.total for tome in tomes}
    assert totals == {shelves["big"].id: 450, shelves["small"].id: 1, shelves["other"].id: 1,
                      shelves["loose"].id: 1}  # true counts, nothing cut off at 400
    big = next(tome for tome in tomes if tome.book_id == shelves["big"].id)
    assert len(big.pages) == 6  # the best few only
    assert [hit.page for hit in big.pages] == sorted(hit.page for hit in big.pages)
    assert big.best in big.pages
    everything = library.search_tome("dragon", shelves["big"].id)
    assert [hit.page for hit in everything] == list(range(450))
    assert library.search_tomes("basilisk") == [] and library.search_tomes("  ") == []
    assert library.search_tome("dragon", 9999) == []


def test_search_can_be_limited_to_some_tomes(library, shelves):
    small, other = shelves["small"].id, shelves["other"].id
    assert {t.book_id for t in library.search_tomes("dragon", {small, other})} == {small, other}
    assert library.search_tomes("dragon", set()) == []  # an empty choice holds nothing
    assert [t.book_id for t in library.search_tomes("dragon", [small], per_tome=0)] == [small]
    assert library.books_within([shelves["dnd"].id]) == {shelves["big"].id, small}
    assert library.books_within([shelves["core"].id, shelves["wars"].id]) == {small, other}
    assert library.books_within([]) == set()


def test_words_standing_together_come_first(library):
    apart = add_tome(library, "Apart", [
        "A saving grace. Saving coin. Saving time. He will throw and throw and throw again."])
    together = add_tome(library, "Together", [
        "Long pages of text about many other matters entirely. Make a saving throw. " * 3])
    tomes = library.search_tomes("saving throw")
    assert [t.book_id for t in tomes] == [together.id, apart.id]
    assert tomes[0].best.phrase and not tomes[1].best.phrase
    assert search.phrase_query("saving throw") == '"saving throw"*'
    assert search.phrase_query("saving throw ") == '"saving throw"'
    assert search.phrase_query("saving") == "" and search.phrase_query('"saving" throw') == ""


def test_a_whole_word_comes_before_a_longer_word_that_starts_with_it(library):
    longer = add_tome(library, "Longer", ["Dragonborn dragonborn dragonborn dragonborn."])
    whole = add_tome(library, "Whole", ["Some long page that mentions one dragon among much else, "
                                        "with many other words around it to water it down."])
    tomes = library.search_tomes("dragon")
    assert [t.book_id for t in tomes] == [whole.id, longer.id]
    assert tomes[0].best.whole and not tomes[1].best.whole
    assert search.whole_word_query("dragon") == '"dragon"'
    assert search.whole_word_query("dragon ") == ""  # already whole: the person typed a space


def test_quotes_and_unfinished_words_still_work(library):
    one = add_tome(library, "One", ["The silver dragon guards its hoard.",
                                    "Dragons of silver and of gold."])
    two = add_tome(library, "Two", {4: "Harvesting a dragon's scales requires care."})
    assert [(t.book_id, [h.page for h in t.pages])
            for t in library.search_tomes('"silver dragon"')] == [(one.id, [0])]
    assert [t.book_id for t in library.search_tomes("harvest scal")] == [two.id]
    assert library.search_tomes('"') == []


def test_the_index_is_followed_as_tomes_come_and_go(library):
    first = add_tome(library, "First", ["A dragon.", "Another dragon."])
    assert [t.book_id for t in library.search_tomes("dragon")] == [first.id]
    second = add_tome(library, "Second", {7: "A late dragon."})  # indexed after a search
    found = {t.book_id: t for t in library.search_tomes("dragon")}
    assert set(found) == {first.id, second.id} and found[second.id].best.page == 7
    library.delete_book(second.id)  # its rows are freed, and the next text reuses them
    third = add_tome(library, "Third", {2: "The newest dragon."})
    found = {t.book_id: t for t in library.search_tomes("dragon")}
    assert set(found) == {first.id, third.id} and found[third.id].best.page == 2
    library.clear_text(first.id)  # as when an edited tome is read again
    library.add_page_text(first.id, [(5, "A dragon moved to page six.")])
    assert [h.page for h in library.search_tome("dragon", first.id)] == [5]


def test_excerpts_are_made_only_for_the_rows_asked_for(library, shelves):
    hits = library.search_tome("dragon", shelves["big"].id)
    wanted = [hits[0].rowid, hits[200].rowid, hits[449].rowid]
    excerpts = library.snippets("dragon", wanted + [10**9])  # an unknown row is ignored
    assert sorted(excerpts) == sorted(wanted)
    assert all("\x02dragon\x03" in text for text in excerpts.values())
    assert "Entry 200:" in excerpts[hits[200].rowid]
    assert library.snippets("dragon", []) == {} and library.snippets("  ", wanted) == {}


# -- choosing where to search ----------------------------------------------------------------


def test_scope_helpers(library, shelves):
    ids = {name: thing.id for name, thing in shelves.items()}
    assert not Scope() and scope_of(places=[1]) and scope_of(cart=True)
    assert search_scope.books_in(library, Scope()) is None
    assert search_scope.books_in(library, scope_of(places=[ids["hunts"]])) == {
        ids["big"], ids["small"]}
    assert search_scope.books_in(library, scope_of(books=[ids["other"]], cart=True)) == {
        ids["other"], ids["loose"]}
    assert search_scope.describe(library, Scope()) == "every tome"
    assert search_scope.describe(library, scope_of(places=[ids["dnd"]])) == "the D&D bookcase"
    assert search_scope.describe(library, scope_of(places=[ids["core"]])) == "the Core set"
    assert search_scope.describe(library, scope_of(books=[ids["small"]])) == "Almanac"
    assert search_scope.describe(library, scope_of(cart=True)) == "the Returns Cart"
    assert search_scope.describe(
        library, scope_of(places=[ids["wars"]], books=[ids["small"]])) == "2 chosen tomes"
    gone = scope_of(places=[ids["wars"], 999], books=[ids["small"], 888])
    assert search_scope.still_valid(library, gone) == scope_of(
        places=[ids["wars"]], books=[ids["small"]])


def rows_by_name(picker):
    return {row.text(0): row for row in picker._rows()}


def test_picker_ticks_places_and_single_tomes(qtbot, library, shelves):
    ids = {name: thing.id for name, thing in shelves.items()}
    add_tome(library, "Second Hunt", ["A wyvern."], ids["hunts"])
    picker = ScopePicker(library)
    qtbot.addWidget(picker)
    picker.rebuild(Scope())
    rows = rows_by_name(picker)
    assert set(rows) == {"D&D", "Hunts", "Core", "Star Wars", "Returns Cart", "Bestiary",
                         "Almanac", "Smugglers", "Pamphlet", "Second Hunt"}
    assert rows["Almanac"].parent() is rows["Core"] and rows["Core"].parent() is rows["Hunts"]
    assert picker.scope() == Scope() and not picker.everything.isEnabled()

    with qtbot.waitSignal(picker.changed, timeout=2000):
        rows["Hunts"].setCheckState(0, Checked)  # a place brings everything inside it
    assert rows["Bestiary"].checkState(0) == Checked and rows["Almanac"].checkState(0) == Checked
    assert rows["D&D"].checkState(0) == Checked  # its only shelf is ticked, so it is too
    assert picker.scope() == scope_of(places=[ids["dnd"]])

    rows["Bestiary"].setCheckState(0, Unchecked)  # now only part of the shelf
    assert rows["Hunts"].checkState(0) == Partly and rows["D&D"].checkState(0) == Partly
    assert picker.scope() == scope_of(places=[ids["core"]], books=[rows["Second Hunt"].data(
        0, ROLE)[1]])

    rows["Pamphlet"].setCheckState(0, Checked)  # the only tome on the cart: the cart itself
    assert picker.scope().cart
    assert picker.everything.isEnabled()
    with qtbot.waitSignal(picker.changed, timeout=2000):
        picker.everything.click()
    assert picker.scope() == Scope()
    assert all(row.checkState(0) == Unchecked for row in picker._rows())


def test_picker_shows_a_given_scope_and_keeps_it_when_the_library_changes(
        qtbot, library, shelves):
    ids = {name: thing.id for name, thing in shelves.items()}
    picker = ScopePicker(library)
    qtbot.addWidget(picker)
    chosen = scope_of(places=[ids["wars"]], books=[ids["big"]])
    picker.rebuild(chosen)
    rows = rows_by_name(picker)
    assert rows["Star Wars"].checkState(0) == Checked
    assert rows["Smugglers"].checkState(0) == Checked
    assert rows["Bestiary"].checkState(0) == Checked and rows["Hunts"].checkState(0) == Partly
    assert picker.scope() == chosen
    newcomer = add_tome(library, "Rebels", ["A dragon? No."], ids["wars"])
    library.delete_book(ids["big"])
    picker.rebuild()  # read the library again
    rows = rows_by_name(picker)
    assert rows["Rebels"].checkState(0) == Checked  # shelved in a ticked place: searched too
    assert "Bestiary" not in rows and picker.scope() == scope_of(places=[ids["wars"]])
    assert search_scope.books_in(library, picker.scope()) == {ids["other"], newcomer.id}


def test_a_tome_shelved_twice_is_ticked_in_both_places(qtbot, library, shelves):
    ids = {name: thing.id for name, thing in shelves.items()}
    library.place_book(ids["other"], ids["hunts"])  # Smugglers now also stands in Hunts
    picker = ScopePicker(library)
    qtbot.addWidget(picker)
    picker.rebuild(Scope())
    twins = picker._tomes[ids["other"]]
    assert len(twins) == 2
    twins[0].setCheckState(0, Checked)
    assert twins[1].checkState(0) == Checked
    assert search_scope.books_in(library, picker.scope()) == {ids["other"]}
    twins[1].setCheckState(0, Unchecked)
    assert twins[0].checkState(0) == Unchecked and picker.scope() == Scope()
    picker.rebuild(scope_of(books=[ids["other"]]))
    assert all(row.checkState(0) == Checked for row in picker._tomes[ids["other"]])


def test_picker_rows_can_be_ticked_by_their_name_and_found_by_typing(qtbot, library, shelves):
    picker = ScopePicker(library)
    qtbot.addWidget(picker)
    picker.rebuild(Scope())
    rows = rows_by_name(picker)
    picker._item_clicked(rows["Almanac"], 0)  # a click on the name, not on the box
    assert rows["Almanac"].checkState(0) == Checked
    picker._item_clicked(rows["Almanac"], 0)
    assert rows["Almanac"].checkState(0) == Unchecked
    rows["Almanac"].setCheckState(0, Checked)  # the box itself was clicked: it has changed…
    picker._item_clicked(rows["Almanac"], 0)  # …so the click that follows must not undo it
    assert rows["Almanac"].checkState(0) == Checked

    picker.find.setText("alma")
    hidden = {name for name, row in rows.items() if row.isHidden()}
    assert hidden == {"Bestiary", "Star Wars", "Smugglers", "Returns Cart", "Pamphlet"}
    assert rows["Hunts"].isExpanded() and rows["Core"].isExpanded()
    picker.find.setText("star")  # a place that matches shows what it holds
    assert not rows["Smugglers"].isHidden() and rows["D&D"].isHidden()
    picker.find.clear()
    assert not any(row.isHidden() for row in rows.values())


# -- the catalogue page -----------------------------------------------------------------------


@pytest.fixture
def catalogue(qtbot, library, shelves):
    widget = CardCatalogue(library)
    qtbot.addWidget(widget)
    widget.resize(900, 700)
    return widget


def tome_rows(catalogue):
    results = catalogue.results
    return {results.topLevelItem(i).data(0, INFO)[0]: results.topLevelItem(i)
            for i in range(results.topLevelItemCount())}


def kinds(tome_row):
    return [tome_row.child(i).data(0, KIND) for i in range(tome_row.childCount())]


def test_catalogue_lists_every_tome_with_its_true_page_count(catalogue, shelves):
    assert "all 4 tomes" in catalogue.summary.text()
    catalogue.field.setText("dragon")
    catalogue.search()
    rows = tome_rows(catalogue)
    assert set(rows) == {"Bestiary", "Almanac", "Smugglers", "Pamphlet"}
    assert rows["Bestiary"].data(0, INFO) == ("Bestiary", 450)
    assert kinds(rows["Bestiary"]) == ["page"] * PER_TOME + ["more"]
    assert kinds(rows["Almanac"]) == ["page"]  # nothing more to show
    assert catalogue.summary.text().startswith("453 pages in 4 tomes.")
    assert all(row.isExpanded() for row in rows.values())
    catalogue.field.setText("basilisk")
    catalogue.search()
    assert catalogue.summary.text() == "No page in the library mentions “basilisk”."
    assert catalogue.results.topLevelItemCount() == 0
    catalogue.field.setText("d")  # too short to search
    catalogue.search()
    assert "all 4 tomes" in catalogue.summary.text()


def test_show_all_lists_the_rest_of_a_tomes_pages_and_can_go_back(catalogue, shelves):
    catalogue.field.setText("dragon")
    catalogue.search()
    bestiary = tome_rows(catalogue)["Bestiary"]
    best_pages = [bestiary.child(i).data(0, ROLE)[1] for i in range(PER_TOME)]
    catalogue._activated(bestiary.child(PER_TOME))  # "Show all 450 pages in this tome"
    assert bestiary.childCount() == 451
    assert [bestiary.child(i).data(0, ROLE)[1] for i in range(450)] == list(range(450))
    assert bestiary.child(450).data(0, INFO) == (shelves["big"].id, 450, True)
    assert tome_rows(catalogue)["Almanac"].childCount() == 1  # other tomes are untouched
    catalogue._activated(bestiary.child(450))  # "Show only the best matches"
    assert kinds(bestiary) == ["page"] * PER_TOME + ["more"]
    assert [bestiary.child(i).data(0, ROLE)[1] for i in range(PER_TOME)] == best_pages


def test_excerpts_are_made_for_the_first_rows_and_then_as_rows_come_into_view(
        qtbot, catalogue, shelves):
    catalogue.field.setText("dragon")
    catalogue.search()
    rows = list(catalogue._page_rows())
    assert len(rows) == PER_TOME + 3 and all(row.data(0, SNIPPET) for row in rows)
    assert "\x02dragon\x03" in rows[0].data(0, SNIPPET) and "dragon" in rows[0].toolTip(0)

    bestiary = tome_rows(catalogue)["Bestiary"]
    catalogue._activated(bestiary.child(PER_TOME))  # 450 rows; none has an excerpt yet
    late = bestiary.child(300)
    assert late.data(0, SNIPPET) is None
    catalogue.want_excerpt(catalogue.results.indexFromItem(late))  # what painting the row does
    qtbot.waitUntil(lambda: late.data(0, SNIPPET) is not None, timeout=2000)
    assert "Entry 300:" in late.data(0, SNIPPET)
    assert bestiary.child(301).data(0, SNIPPET) is None  # only what was asked for

    catalogue.show()
    catalogue.results.scrollToItem(bestiary.child(120))
    catalogue.results.viewport().grab()  # painting asks for the rows now in view
    qtbot.waitUntil(lambda: bestiary.child(120).data(0, SNIPPET) is not None, timeout=2000)
    catalogue.load_all_excerpts()
    assert all(row.data(0, SNIPPET) for row in catalogue._page_rows())
    assert EAGER_ROWS >= PER_TOME


def test_results_can_be_ordered_and_the_order_is_remembered(qtbot, library, catalogue, shelves):
    add_tome(library, "Zoology", ["dragon dragon dragon dragon dragon dragon dragon"])
    catalogue.field.setText("dragon")
    catalogue.search()
    assert list(tome_rows(catalogue))[0] == "Zoology"  # the best single page
    catalogue.order_box.setCurrentIndex(catalogue.order_box.findData("pages"))
    catalogue.order_box.activated.emit(catalogue.order_box.currentIndex())
    assert list(tome_rows(catalogue))[0] == "Bestiary"
    assert prefs.get("catalogue/order") == "pages"
    catalogue.order_box.setCurrentIndex(catalogue.order_box.findData("title"))
    catalogue.order_box.activated.emit(catalogue.order_box.currentIndex())
    assert list(tome_rows(catalogue)) == ["Almanac", "Bestiary", "Pamphlet", "Smugglers",
                                          "Zoology"]
    again = CardCatalogue(library)
    qtbot.addWidget(again)
    assert again.order_box.currentData() == "title"


def test_the_search_can_be_narrowed_and_widened_again(catalogue, shelves):
    ids = {name: thing.id for name, thing in shelves.items()}
    catalogue.field.setText("dragon")
    catalogue.search()
    assert catalogue.scope_button.text() == "Search in: Every Tome"
    assert catalogue.everywhere.isHidden()
    catalogue.set_scope(scope_of(places=[ids["dnd"]]))  # searches again by itself
    assert set(tome_rows(catalogue)) == {"Bestiary", "Almanac"}
    assert catalogue.scope_button.text() == "Search in: the D&&D bookcase"  # shown as D&D
    assert menu_text("D&D") == "D&&D"
    assert catalogue.summary.text().startswith(
        "451 pages in 2 tomes · searching the D&D bookcase.")
    assert not catalogue.everywhere.isHidden()
    assert catalogue.field.placeholderText().startswith("Search the D&D bookcase…")

    catalogue.set_scope(scope_of(books=[ids["other"]], cart=True))
    assert set(tome_rows(catalogue)) == {"Smugglers", "Pamphlet"}
    catalogue.field.setText("freighter")
    catalogue.search()
    assert set(tome_rows(catalogue)) == {"Smugglers"}
    catalogue.field.setText("basilisk")
    catalogue.search()
    assert catalogue.summary.text() == "No page in the 2 chosen tomes mentions “basilisk”."
    catalogue.field.clear()
    catalogue.search()
    assert catalogue.summary.text() == "Searches the text of the 2 chosen tomes."
    catalogue.set_scope(scope_of(places=[ids["hunts"]]))
    assert catalogue.summary.text() == "Searches the text of the 2 tomes in the Hunts shelf."
    empty = catalogue.library.add_place("shelf", "Bare", ids["wars"])
    catalogue.set_scope(scope_of(places=[empty.id]))
    assert catalogue.summary.text() == "There are no tomes in the Bare shelf to search."

    catalogue.field.setText("dragon")
    catalogue.everywhere.click()
    assert catalogue.scope() == Scope() and len(tome_rows(catalogue)) == 4
    assert catalogue.field.placeholderText().startswith("Search every tome…")


def test_the_picker_narrows_the_catalogue(qtbot, catalogue, shelves):
    catalogue.field.setText("dragon")
    catalogue.search()
    catalogue.picker.rebuild(catalogue.scope())
    with qtbot.waitSignal(catalogue.picker.changed, timeout=2000):
        rows_by_name(catalogue.picker)["Star Wars"].setCheckState(0, Checked)
    assert catalogue.scope() == scope_of(places=[shelves["wars"].id])
    assert set(tome_rows(catalogue)) == {"Smugglers"}


def test_a_result_can_narrow_the_search_to_its_tome(catalogue, shelves):
    catalogue.field.setText("dragon")
    catalogue.search()
    almanac = tome_rows(catalogue)["Almanac"]
    menu = catalogue.menu_for(almanac)
    assert [action.text() for action in menu.actions()] == [
        "Open at the Best Match", "Search Only This Tome", "Pin to Study Table"]
    menu.actions()[1].trigger()
    assert catalogue.scope() == scope_of(books=[shelves["small"].id])
    assert set(tome_rows(catalogue)) == {"Almanac"}
    assert catalogue.scope_button.text() == "Search in: Almanac"
    assert catalogue.summary.text().startswith("1 page in 1 tome · searching Almanac.")
    page_row = tome_rows(catalogue)["Almanac"].child(0)
    menu = catalogue.menu_for(page_row)
    assert menu.actions()[0].text() == "Open This Page" and not menu.actions()[1].isEnabled()
    catalogue.field.clear()
    catalogue.search()
    assert catalogue.summary.text() == "Searches the text of Almanac."


def test_rows_open_the_tome_at_the_page(qtbot, library, catalogue, shelves):
    add_tome(library, "Rules", {8: "On a failure, make a saving throw at once.",
                                9: "Saving up to throw a party."})
    catalogue.field.setText("saving throw")
    catalogue.search()
    rules = tome_rows(catalogue)["Rules"]
    book_id = rules.data(0, ROLE)[0]
    with qtbot.waitSignal(catalogue.openRequested) as opened:
        catalogue._activated(rules)  # the tome's own row: its best page
    assert opened.args == [book_id, 8, "saving throw"]
    with qtbot.waitSignal(catalogue.openRequested) as opened:
        catalogue._activated(rules.child(1))
    assert opened.args == [book_id, 9, "saving"]  # words apart: highlight the longest
    catalogue.show()
    qtbot.keyClick(catalogue.field, Qt.Key.Key_Down)  # from the field into the results
    assert catalogue.results.currentItem() is rules
    with qtbot.waitSignal(catalogue.openRequested):
        qtbot.keyClick(catalogue.results, Qt.Key.Key_Return)


def test_a_scope_that_leaves_the_library_falls_away(library, catalogue, shelves):
    catalogue.field.setText("dragon")
    catalogue.set_scope(scope_of(places=[shelves["wars"].id]))
    assert set(tome_rows(catalogue)) == {"Smugglers"}
    library.delete_place(shelves["wars"].id)
    catalogue.refresh()  # as when the library reports a change
    assert catalogue.scope() == Scope() and len(tome_rows(catalogue)) == 4
    catalogue.set_scope(scope_of(books=[shelves["small"].id, shelves["other"].id]))
    library.delete_book(shelves["other"].id)
    catalogue.search()
    assert catalogue.scope() == scope_of(books=[shelves["small"].id])


@pytest.mark.parametrize("query, together, needle", [
    ("dragon", True, "dragon"), ("frost breath weapon", False, "breath"),
    ("frost  breath weapon", True, "frost breath weapon"),
    ('"silver dragon" hoard', True, "silver dragon"), ("  ", True, ""),
])
def test_find_needle_uses_the_phrase_when_the_words_stand_together(query, together, needle):
    assert find_needle(query, together) == needle


def test_excerpt_pieces_and_keeping_the_match_in_view(qtbot, catalogue):
    pieces = segments("…a  long\nlead-in before the \x02dragon\x03 and \x02wyrm\x03 after…")
    assert pieces == [("…a long lead-in before the ", False), ("dragon", True), (" and ", False),
                      ("wyrm", True), (" after…", False)]
    assert segments("\x02all\x03") == [("all", True)] and segments("") == []
    metrics = catalogue.fontMetrics()
    roomy = lead_in(pieces, metrics, 4000)
    assert roomy == pieces  # everything fits: nothing is cut
    tight = lead_in(pieces, metrics, metrics.horizontalAdvance("before the dragon") * 2)
    assert tight[0][0].startswith("…") and tight[0][0].endswith("the ")
    assert len(tight[0][0]) < len(pieces[0][0]) and tight[1:] == pieces[1:]
    assert lead_in([("dragon", True), (" tail", False)], metrics, 10)[0] == ("dragon", True)


# -- from the shelves ---------------------------------------------------------------------------


def test_search_inside_a_place_from_its_menu(qtbot, library, shelves):
    importer = Importer(library)
    page = LibraryPage(library, importer)
    qtbot.addWidget(page)
    try:
        menu = QMenu()
        page._place_menu(menu, shelves["hunts"].id)
        inside = next(action for action in menu.actions() if action.text() == "Search Inside…")
        inside.trigger()
        assert page.tree.current_key() == ("virtual", "catalogue")
        assert page.catalogue.scope() == scope_of(places=[shelves["hunts"].id])
        assert page.catalogue.scope_button.text() == "Search in: the Hunts shelf"
        page.show_catalogue("dragon", scope_of(books=[shelves["other"].id]))
        assert set(tome_rows(page.catalogue)) == {"Smugglers"}
        page.show_catalogue("dragon", Scope())  # an empty scope widens it again
        assert len(tome_rows(page.catalogue)) == 4
        page.show_catalogue("blasters")  # no scope given: keep the current one
        assert set(tome_rows(page.catalogue)) == {"Smugglers"}
    finally:
        importer.close()
