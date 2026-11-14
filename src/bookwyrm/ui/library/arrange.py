"""Filtering and sorting what the shelves show. Pure functions; no Qt."""

from __future__ import annotations

from bookwyrm.ui.library.bookcase_view import Group, Section

# key, label shown in the Sort menu
SORTS = (
    ("shelf", "Shelf order"),
    ("title", "Title"),
    ("author", "Author"),
    ("added", "Newest first"),
    ("read", "Recently read"),
)


def sort_books(books: list, sort: str) -> list:
    if sort == "title":
        return sorted(books, key=lambda b: b.title.casefold())
    if sort == "author":  # tomes with no author go last
        return sorted(books, key=lambda b: (not b.author, b.author.casefold(), b.title.casefold()))
    if sort == "added":
        return sorted(books, key=lambda b: (b.added_at, b.id), reverse=True)
    if sort == "read":  # never-opened tomes go last, by title
        opened = sorted((b for b in books if b.last_opened_at),
                        key=lambda b: b.last_opened_at, reverse=True)
        return opened + sorted((b for b in books if not b.last_opened_at),
                               key=lambda b: b.title.casefold())
    return list(books)


def arrange(sections: list[Section], text: str = "", label: str | None = None,
            sort: str = "shelf", labels_by_book: dict | None = None) -> list[Section]:
    """Apply the filter box, the Label filter and the sort order.

    While a filter is active, sets and shelves left with nothing to show are
    dropped, so the result reads as a list of matches.
    """
    needle = text.casefold().strip()
    wanted = label.casefold() if label else None
    filtering = bool(needle) or wanted is not None
    labels_by_book = labels_by_book or {}

    def keep(book) -> bool:
        if needle and needle not in book.title.casefold() \
                and needle not in book.author.casefold():
            return False
        if wanted is not None:
            return wanted in (name.casefold() for name in labels_by_book.get(book.id, ()))
        return True

    arranged = []
    for section in sections:
        groups = []
        for group in section.groups:
            books = sort_books([b for b in group.books if keep(b)], sort)
            if books or not filtering:
                groups.append(Group(group.place_id, group.title, books))
        if groups or not filtering:
            arranged.append(Section(section.place_id, section.title, groups, section.hint))
    return arranged


def count(sections: list[Section]) -> int:
    return sum(len(group.books) for section in sections for group in section.groups)
