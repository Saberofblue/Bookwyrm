"""Bookwyrm updating itself from its own releases.

    release.py   what a release is, whether it is newer, which download suits this computer
    fetch.py     reading the release notice and downloading a file (QtNetwork)
    mac.py       putting a new version in place of the running one on a Mac
    updater.py   the steps in order: check, download, verify, try the new version, swap

Nothing here runs unless the user asks for it ("Check for Updates"). The only places
contacted are GitHub's release notice for this project and the download it names.
"""
