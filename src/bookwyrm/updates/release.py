"""What a release is, whether it is newer, and which download suits this computer. Qt-free.

Where updates come from
    The newest release is described by one document on GitHub (LATEST_URL). Every
    download it names must sit under this project's own releases (DOWNLOAD_PREFIX) and
    comes with a SHA-256 checksum, which the downloaded file must match.

Rehearsals
    Setting the environment variable BOOKWYRM_UPDATE_FEED to the path of a local file
    makes that file stand in for GitHub's document, so an update can be rehearsed without
    publishing anything. Such a feed may only name downloads that are local files
    (file: URLs): the variable can make Bookwyrm install something already on this
    computer, which its user could do by hand, but never fetch from another web site.
"""

from __future__ import annotations

import hashlib
import os
import platform
import re
import sys
from dataclasses import dataclass
from pathlib import Path

REPOSITORY = "Saberofblue/Bookwyrm"
LATEST_URL = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
DOWNLOAD_PREFIX = f"https://github.com/{REPOSITORY}/releases/download/"
RELEASES_PAGE = f"https://github.com/{REPOSITORY}/releases/latest"
FEED_VARIABLE = "BOOKWYRM_UPDATE_FEED"


class UpdateError(Exception):
    """Why an update could not go ahead, phrased for the user."""


@dataclass(frozen=True)
class Source:
    url: str  # where the description of the newest release is read from
    rehearsal: bool  # True when that is a local file named by BOOKWYRM_UPDATE_FEED


@dataclass(frozen=True)
class Asset:
    name: str
    url: str
    size: int
    sha256: str  # lower-case hex; never empty (a download without a checksum is not offered)


@dataclass(frozen=True)
class Release:
    version: str  # "0.4.1"
    title: str
    notes: str  # Markdown, as written on the release page
    page: str  # the release's page, for reading or downloading by hand
    assets: tuple[Asset, ...]


def source() -> Source:
    feed = os.environ.get(FEED_VARIABLE)
    if feed:
        return Source(Path(feed).expanduser().resolve().as_uri(), True)
    return Source(LATEST_URL, False)


def parse_version(text: str) -> tuple[int, ...] | None:
    """'v0.4.1' -> (0, 4, 1). None for anything that is not plain numbers and dots, so
    that a test or preview build ('0.5.0-beta') is never taken for a release."""
    match = re.fullmatch(r"\s*v?(\d+(?:\.\d+){0,3})\s*", str(text))
    return tuple(int(part) for part in match.group(1).split(".")) if match else None


def is_newer(candidate: str, current: str) -> bool:
    new, now = parse_version(candidate), parse_version(current)
    if new is None or now is None:
        return False
    width = max(len(new), len(now))
    return new + (0,) * (width - len(new)) > now + (0,) * (width - len(now))


def trusted(url: str, rehearsal: bool) -> bool:
    """May a download be taken from this address?"""
    if rehearsal:
        return url.startswith("file:")
    return url.startswith(DOWNLOAD_PREFIX)


def parse_release(data, rehearsal: bool = False) -> Release:
    """Read GitHub's description of a release. Downloads from anywhere unexpected, or
    without a checksum, are left out rather than trusted."""
    if not isinstance(data, dict) or parse_version(data.get("tag_name", "")) is None:
        raise UpdateError("The release notice could not be understood.")
    if data.get("draft") or data.get("prerelease"):
        raise UpdateError("The newest release is not a finished one.")
    assets = []
    for entry in data.get("assets") or []:
        if not isinstance(entry, dict):
            continue
        name, url = str(entry.get("name", "")), str(entry.get("browser_download_url", ""))
        digest = str(entry.get("digest") or "")
        checksum = digest[7:].lower() if digest.startswith("sha256:") else ""
        if (name and trusted(url, rehearsal) and re.fullmatch(r"[0-9a-f]{64}", checksum)
                and isinstance(entry.get("size"), int) and entry["size"] > 0):
            assets.append(Asset(name, url, entry["size"], checksum))
    version = ".".join(str(part) for part in parse_version(data["tag_name"]))
    page = str(data.get("html_url") or "")
    if not rehearsal and not page.startswith(f"https://github.com/{REPOSITORY}/"):
        page = RELEASES_PAGE
    return Release(version, str(data.get("name") or f"Bookwyrm {version}"),
                   str(data.get("body") or ""), page, tuple(assets))


def asset_name(system: str | None = None, machine: str | None = None) -> str | None:
    """The name of the download made for a kind of computer, or None if there is none."""
    system = sys.platform if system is None else system
    machine = (platform.machine() if machine is None else machine).lower()
    if system == "darwin":
        return f"Bookwyrm-mac-{'arm64' if machine in ('arm64', 'aarch64') else 'x86_64'}.zip"
    if system == "win32":
        return "Bookwyrm-windows.zip"
    return None


def asset_for(release: Release, system: str | None = None,
              machine: str | None = None) -> Asset | None:
    wanted = asset_name(system, machine)
    return next((asset for asset in release.assets if asset.name == wanted), None)


def file_sha256(path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def megabytes(count: int) -> str:
    return f"{count / (1 << 20):.0f} MB" if count >= 10 << 20 else f"{count / (1 << 20):.1f} MB"
