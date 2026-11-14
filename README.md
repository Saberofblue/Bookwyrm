# Bookwyrm

A library, reader and scriptorium for your PDFs, kept by a silver dragon.

## Run it

**On a Mac**

- Packaged app: `dist/Bookwyrm.app` (double-click; drag it to Applications if you like).
  Rebuild it after code changes with `packaging/build.sh`.
- From source: `./run.sh`

**On Windows** (written for Windows but not yet run on it; see "Windows" below)

- Double-click `Install Bookwyrm.bat`. It sets everything up, checks itself, and adds
  Desktop and Start-menu shortcuts. After that use the shortcut or `Start Bookwyrm.bat`.

Your library lives in a folder called `Bookwyrm Library` in your home folder. Adding a
PDF **copies** it there; the file you added is never changed. Set the environment
variable `BOOKWYRM_LIBRARY` to use a different folder (handy for experiments).

Shortcuts below are written for a Mac. On Windows use **Ctrl** for ⌘, **Shift** for ⇧
and **Alt** for ⌥; the tooltips inside the app always show the right keys for your system.

## The library

| Term | Meaning |
|---|---|
| Bookcase → Shelf → Set | Three levels of organization; a Set is a run of tomes between bookends |
| Tome | One PDF. It can stand in several places and is stored once |
| Returns Cart | Added tomes not yet shelved |
| The Stacks | Every tome |
| Reading Desk | Recently read tomes |
| Card Catalogue | Search the text of every tome at once (⇧⌘F) |
| Commonplace Book | Excerpts you have clipped from pages |
| Study Tables | Pages pinned together for a session |

Add tomes with **Add Tomes…** (⌘O) or by dragging PDFs onto a shelf. Drag tomes
between shelves; hold ⌥ (Ctrl on Windows) while dropping to shelve a tome in a second place.

## Reading

| Action | Shortcut |
|---|---|
| Zoom in / out / actual size | ⌘+ / ⌘− / ⌘0 (also pinch, or ⌘-scroll) |
| Fit width / fit page | ⌘1 / ⌘2 |
| Find in tome, next, previous | ⌘F, ⌘G, ⇧⌘G |
| Tie or untie a Ribbon (bookmark) | ⌘D |
| Side panel (Contents, Pages, Ribbons) | ⌥⌘S |
| Reading mode: Daylight → Candlelight → Night | ⇧⌘L |
| Excerpt tool (clip part of a page) | ⌘E |
| Pin page to a Study Table | ⇧⌘P |
| Move tome to the other side / open it alongside | ⌘\ / ⇧⌘\ |
| Back to the Library | ⌘L |

## The Scriptorium (editing)

Open it with the quill button or ⇧⌘E. It works on tomes in your library only.

- **Annotate:** highlight, underline, strike, sticky notes, text boxes, freehand,
  boxes, ovals, lines and arrows; the Pointer moves, edits and deletes them.
- **Fill Form:** click a field and type; Tab moves to the next.
- **Edit Text / Add Text:** change the wording of a line in place, or write new text.
- **Pictures / Add Picture / Erase:** move, resize, replace, delete or add pictures;
  erase a region.
- **Pages…, Contents…, Properties…:** rearrange, rotate, delete, insert and extract
  pages; edit the table of contents; edit title, author and keywords.
- **Undo / Redo** (⌘Z / ⇧⌘Z) work until you **Save** (⌘S). The first save keeps a
  pristine copy; **Restore…** brings it back.

Good to know:

- Editing a line re-sets it in the tome's own font when that font contains the letters
  you typed, otherwise in the closest standard font. It does not re-flow paragraphs.
- Words that are part of a picture cannot be edited, only covered or erased.
- A picture stored once and shown on several pages changes on all of them; Bookwyrm
  warns you first.
- Forms that calculate totals with scripts will not recalculate; type those values.

## Windows

**Sending Bookwyrm to someone with a Windows PC**

```sh
.venv/bin/python packaging/make_windows_package.py    # writes dist/Bookwyrm-for-Windows.zip
```

Send them that zip (about 3 MB). They unzip it and double-click **Install Bookwyrm**;
`READ ME FIRST.txt` inside tells them the rest. The installer:

- finds Python 3.11 or newer, or offers to install it;
- makes a private environment inside the folder and downloads the two packages Bookwyrm
  needs (about 110 MB; nothing else is installed system-wide);
- runs Bookwyrm's self-test, adds Desktop and Start-menu shortcuts, and opens the app;
- writes everything to `setup-log.txt`, which they send back if it does not finish.

Once installed, errors go to `bookwyrm.log` in their `Bookwyrm Library` folder.

**Checking an installation:** `python -m bookwyrm --self-test [report.txt]` (any system)
exercises the helper processes, rendering, editing and saving, importing and search in a
throwaway library and prints `[ OK ]` or `[FAIL]` for each.

**A stand-alone `Bookwyrm.exe`** (no Python needed on the user's PC) has to be built on
Windows: run `packaging\build.bat --zip` there, or put the project in a GitHub repository,
where `.github/workflows/build.yml` tests and builds it on a Windows machine and offers
the results as downloads. The same recipe rehearses the installer above there.

What differs on Windows, handled in the code: helper processes are connected by an
inherited pipe handle instead of a Unix descriptor (`channel.py`); a file that is open
cannot be replaced or deleted, so saving closes the tome first, waits out brief locks
and never loses changes if the swap fails (`pdf/editing.py`); and file names avoid the
characters and device names Windows refuses.

## Develop

```sh
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest        # runs headless
.venv/bin/ruff check src tests
```

Layout: `src/bookwyrm/db` (catalogue), `library` (import and indexing), `pdf` (the
helper process that owns each open tome, and the editing engine), `ui` (interface and
theme). `spikes/` holds the measurements the design was based on. Artwork credits are
in `src/bookwyrm/ui/theme/assets/CREDITS.md` and under Help → About.
