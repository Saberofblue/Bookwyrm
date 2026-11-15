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
PDF **copies** it there; the file you added is never changed. **Settings** (⌘, or the
button under the bookcase list) lets you keep the library somewhere else: Bookwyrm can
move everything there, or switch to a library that is already there.

**Updating.** Settings has a **Check for Updates** button (also under Help, or in the
Bookwyrm menu on a Mac). Bookwyrm asks GitHub for its newest release only when you press
it; nothing about your library is sent. On a Mac, **Update and Restart** then downloads
the new version, checks it against the release's checksum, makes sure it runs, and puts
it in place of the old one; if the new version fails to start, the old one is put back.
On Windows the button opens the download page for now.

A newer Bookwyrm upgrades a library's catalogue the first time it opens it, and older
versions cannot open it afterwards. A copy of the catalogue as it was is kept in the
library folder as `bookwyrm-before-upgrade.db`; to go back to an older version, quit
Bookwyrm and put that copy in place of `bookwyrm.db`.

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
| Card Catalogue | Search the text of your tomes at once (⇧⌘F) |
| Commonplace Book | Excerpts you have clipped from pages |
| Study Tables | Pages pinned together for a session |

Add tomes with **Add Tomes…** (⌘O) or by dragging PDFs onto a shelf. Drag tomes
between shelves; hold ⌥ (Ctrl on Windows) while dropping to shelve a tome in a second place.

On the shelves:

- **Filter, Label, Sort** above the shelves narrow and order what you see. Give tomes
  Labels from the right-click menu.
- **Cover size** slider, and **List** for a compact table of a big collection.
- **Minimize a shelf** by clicking its nameplate rail (the arrow shows which are folded).
- **Right-click a tome** to export a copy (with your edits) or the original, or to show
  its file in Finder or File Explorer.
- Bookwyrm reopens the tomes you had open last time (a Settings switch turns this off).

In the Card Catalogue:

- **Every tome that mentions your words is listed**, with how many of its pages do and
  the best few of them. **Show all** under a tome lists the rest; nothing is cut off.
- **Search in** narrows the search: tick whole bookcases, shelves or sets, or single
  tomes. Or right-click a bookcase, shelf, set or tome on the shelves and choose
  **Search Inside…**, or right-click a result and choose **Search Only This Tome**.
- Tomes can be listed best match first, most pages first, or by title.
- Words typed without quotes must all be on the page; pages where they stand together
  come first. Put a phrase in "quotes" to require it exactly.
- Results name pages by the number printed on them where a tome records one, as the
  reader does.
- **Right-click a result** to pin its page to a Study Table without opening the tome.
- A tome whose pages are pictures (a scan with no text in it) cannot be searched. The
  catalogue says which tomes those are, their covers carry a small crossed-out magnifying
  glass, and a tome's Details say how many of its pages hold text.

## Reading

| Action | Shortcut |
|---|---|
| Zoom in / out / actual size | ⌘+ / ⌘− / ⌘0 (also pinch, or ⌘-scroll) |
| Fit width / fit page | ⌘1 / ⌘2 |
| Two-page spread / full screen | ⌘3 / ⌃⌘F (F11 on Windows; Esc leaves) |
| Back / Forward after a jump | ⌘[ / ⌘] (Alt+← / Alt+→ on Windows) |
| Print | ⌘P |
| Find in tome, next, previous | ⌘F, ⌘G, ⇧⌘G |
| Tie or untie a Ribbon (bookmark) | ⌘D |
| Side panel (Contents, Pages, Ribbons) | ⌥⌘S |
| Reading mode: Daylight → Candlelight → Night | ⇧⌘L |
| Excerpt tool (clip part of a page) | ⌘E |
| Pin page to a Study Table | ⇧⌘P |
| Move tome to the other side / open it alongside | ⌘\ / ⇧⌘\ |
| Back to the Library | ⌘L |

Links inside a tome work: click a contents entry or a "see page 212" and use Back to
return. Where a tome records the numbers printed on its pages, the page box shows and
accepts those (type `86` for the page printed 86; `#93` for the 93rd sheet of the file).

## The Scriptorium (editing)

Open it with the quill button or ⇧⌘E. It works on tomes in your library only.

- **Annotate:** highlight, underline, strike, sticky notes, text boxes, freehand,
  boxes, ovals, lines and arrows; the Pointer moves, edits and deletes them.
- **Fill Form:** click a field and type; Tab moves to the next.
- **Edit Text / Add Text:** change the wording of a line in place, or write new text.
- **Pictures / Add Picture / Erase:** move, resize (drag a corner; hold Shift to
  stretch), replace, delete or add pictures; erase a region.
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

## License

Bookwyrm is free software: you may use, study, change and share it under the terms of
the GNU Affero General Public License, version 3 (see `LICENSE`). If you share a changed
version, or run one as a network service, you must offer its source under the same terms.
It comes with no warranty.

Copyright (C) 2026 Saberofblue.

Bookwyrm includes PyMuPDF (AGPL-3.0) and Qt for Python (LGPL-3.0). Bundled fonts, icons
and textures keep their own licenses, listed in
`src/bookwyrm/ui/theme/assets/CREDITS.md`.

## Develop

```sh
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest        # runs headless
.venv/bin/ruff check src tests
```

Layout: `src/bookwyrm/db` (catalogue), `library` (import and indexing), `pdf` (the
helper process that owns each open tome, and the editing engine), `updates` (Bookwyrm
updating itself), `ui` (interface and theme). `spikes/` holds the measurements the design was based on. Artwork credits are
in `src/bookwyrm/ui/theme/assets/CREDITS.md` and under Help → About.

**Releases and updates.** Bookwyrm updates itself from this project's GitHub releases,
finding its download by name: `Bookwyrm-mac-arm64.zip` and `Bookwyrm-windows.zip`, as
`packaging/build.py --zip` names them. A release that lacks the file for a system simply
offers that system nothing to install. `packaging/rehearse_update.py` rehearses an update
with real packaged builds and publishes nothing; with `--kit DIR` it prepares what is
needed to try the Check for Updates window by hand.
