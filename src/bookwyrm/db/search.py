"""Searching the text of the library tome by tome. Qt-free.

The first Card Catalogue asked the index for the 400 best pages in the whole
library. A few large tomes could take all 400 places, so other tomes that
mention the words never appeared, and no page past the 400th could be reached.

This search answers a different question first: *which tomes* mention the
words, and on how many pages each. Every such tome is reported with its true
page count and its best few pages; the rest of a tome's pages are fetched when
they are asked for. Nothing is capped.

How it stays quick
    The index can say which rows match and how well without reading any page's
    text, but it cannot say which tome a row belongs to without fetching that
    page's whole stored record. RowMap therefore remembers, for each row, its
    tome and page number, so one pass over the matches is enough to group them.
    Excerpts ("snippets") are the costly part, so they are made only for the
    rows that are actually going to be shown.
"""

from __future__ import annotations

import heapq
import re
from array import array
from dataclasses import dataclass

SNIPPET_WORDS = 32  # how much of the page an excerpt shows around a match


@dataclass(frozen=True)
class PageHit:
    rowid: int  # the page's row in the search index; ask Library.snippets() with it
    page: int  # 0-based
    score: float  # lower is a better match
    phrase: bool  # the words stand together on this page, in the order typed
    whole: bool  # the last word typed is a whole word here, not the start of a longer one


@dataclass(frozen=True)
class TomeHits:
    book_id: int
    total: int  # how many of this tome's pages match
    pages: tuple[PageHit, ...]  # the best few of them, in page order
    best: PageHit  # the best match of all


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


def phrase_query(text: str) -> str:
    """The query for pages where the typed words stand together, or '' if that is
    no different from the plain query (one word, or the person used quotes)."""
    if '"' in text:
        return ""
    words = [word for word in text.split() if re.search(r"\w", word)]
    if len(words) < 2:
        return ""
    return '"' + " ".join(words) + '"' + ("" if text.endswith(" ") else "*")


def whole_word_query(text: str) -> str:
    """The query with its last word taken as a whole word, or '' if it already is.

    While someone types, the last word also matches longer words that begin with
    it ("dragon" finds "dragonling"). Pages with the word itself should come first.
    """
    query = fts_query(text)
    return query[:-1] if query.endswith("*") else ""


def rank_key(hit: PageHit):
    """Best first: the words together, then whole words, then how well the page matches."""
    return (not hit.phrase, not hit.whole, hit.score, hit.page)


class RowMap:
    """Which tome and page each row of the search index holds.

    Rows are only ever added at the end, so the map is extended by reading the
    rows past the last one it knows. Removing a tome's text frees row numbers
    that later text may reuse, so the owner must throw the map away then.
    """

    def __init__(self):
        self.books = array("q", [0])
        self.pages = array("l", [0])
        self.top = 0  # the highest row read so far

    def extend(self, db) -> None:
        rows = db.execute("SELECT rowid, book_id, page FROM pages_fts WHERE rowid > ?",
                          (self.top,))
        books, pages = self.books, self.pages
        for rowid, book, page in rows:
            if rowid >= len(books):
                gap = rowid + 1 - len(books)
                books.extend([0] * gap)
                pages.extend([0] * gap)
            books[rowid] = int(book)
            pages[rowid] = int(page)
            if rowid > self.top:
                self.top = rowid


# Inside this module a match is a plain tuple (words apart?, only part of a word?,
# score, page, rowid): tuples sort best-first as they are, and a common word can match
# tens of thousands of pages.


def _hit(match) -> PageHit:
    apart, partial, score, page, rowid = match
    return PageHit(rowid, page, score, not apart, not partial)


def _rows_matching(db, query: str) -> set[int]:
    return {row[0] for row in db.execute(
        "SELECT rowid FROM pages_fts WHERE pages_fts MATCH ?", (query,))}


def matches(db, rows: RowMap, text: str, books=None) -> dict[int, list[tuple]]:
    """Every page that matches, grouped by tome. `books` limits the search to those
    tomes (a set of ids); None searches them all."""
    query = fts_query(text)
    if not query:
        return {}
    phrase, whole = phrase_query(text), whole_word_query(text)
    together = _rows_matching(db, phrase) if phrase else None
    entire = _rows_matching(db, whole) if whole else None
    found: dict[int, list[tuple]] = {}
    book_of, page_of = rows.books, rows.pages
    known = len(book_of)
    for rowid, score in db.execute(
            "SELECT rowid, rank FROM pages_fts WHERE pages_fts MATCH ?", (query,)):
        book = book_of[rowid] if rowid < known else 0
        if not book or (books is not None and book not in books):
            continue
        match = (together is not None and rowid not in together,
                 entire is not None and rowid not in entire,
                 score, page_of[rowid], rowid)
        hits = found.get(book)
        if hits is None:
            found[book] = [match]
        else:
            hits.append(match)
    return found


def by_tome(found: dict[int, list[tuple]], per_tome: int) -> list[TomeHits]:
    """Summarise grouped matches: one entry per tome, the best tome first."""
    tomes = []
    for book, hits in found.items():
        best = heapq.nsmallest(max(1, per_tome), hits)
        shown = sorted(best, key=lambda match: match[3]) if per_tome > 0 else []
        tomes.append(TomeHits(book, len(hits), tuple(_hit(m) for m in shown), _hit(best[0])))
    tomes.sort(key=lambda tome: rank_key(tome.best))
    return tomes


def in_page_order(hits: list[tuple]) -> list[PageHit]:
    return [_hit(match) for match in sorted(hits, key=lambda match: match[3])]


def snippets(db, text: str, rowids) -> dict[int, str]:
    """An excerpt for each of the given rows, with matches wrapped in \\x02 ... \\x03.

    Asked for row by row, the index starts the whole search again for every row,
    which for a very common word takes longer than the search itself. One walk
    through the matches between the first and last wanted row, making an excerpt
    only at the wanted ones, costs a single search however many rows are wanted.
    """
    query = fts_query(text)
    wanted = sorted({int(rowid) for rowid in rowids})
    found: dict[int, str] = {}
    if not query:
        return found
    for start in range(0, len(wanted), 400):
        chunk = wanted[start:start + 400]
        marks = ",".join("?" * len(chunk))
        rows = db.execute(
            f"""SELECT rowid,
                       CASE WHEN rowid IN ({marks})
                            THEN snippet(pages_fts, 0, char(2), char(3), '…', {SNIPPET_WORDS})
                       END
                FROM pages_fts
                WHERE pages_fts MATCH ? AND rowid >= ? AND rowid <= ?""",
            (*chunk, query, chunk[0], chunk[-1]))
        found.update((row[0], row[1]) for row in rows if row[1] is not None)
    return found
