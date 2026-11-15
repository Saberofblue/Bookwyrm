"""Bookwyrm updating itself: reading a release, fetching it, proving it, swapping it in.

A real update comes from GitHub. Here a local file stands in for GitHub's release notice
(the BOOKWYRM_UPDATE_FEED rehearsal), and small stand-in apps stand in for Bookwyrm.
"""

import json
import os
import plistlib
import subprocess
import sys
import time

import pytest
from PySide6.QtWidgets import QDialog

from bookwyrm.updates import mac, release, updater
from bookwyrm.updates.fetch import CANCELLED, Fetcher
from bookwyrm.updates.release import UpdateError
from bookwyrm.updates.updater import Updater

MAC = sys.platform == "darwin"
mac_only = pytest.mark.skipif(not MAC, reason="replacing the app is built for the Mac first")
GITHUB = {  # shaped as GitHub describes a release
    "tag_name": "v0.5.0", "name": "Bookwyrm 0.5.0", "body": "## New\n- Dice", "draft": False,
    "prerelease": False, "html_url": "https://github.com/Saberofblue/Bookwyrm/releases/tag/v0.5.0",
    "assets": [
        {"name": "Bookwyrm-windows.zip", "size": 108140723, "digest": "sha256:" + "ab" * 32,
         "browser_download_url": release.DOWNLOAD_PREFIX + "v0.5.0/Bookwyrm-windows.zip"},
        {"name": "Bookwyrm-mac-arm64.zip", "size": 90000000, "digest": "sha256:" + "cd" * 32,
         "browser_download_url": release.DOWNLOAD_PREFIX + "v0.5.0/Bookwyrm-mac-arm64.zip"},
    ],
}


# -- what a release is ---------------------------------------------------------------------


@pytest.mark.parametrize("text, parsed", [
    ("v0.4.1", (0, 4, 1)), ("0.4", (0, 4)), (" 1.0.0 ", (1, 0, 0)), ("v2", (2,)),
    ("0.5.0-beta", None), ("latest", None), ("", None), ("v1.2.3.4.5", None), ("1..2", None),
])
def test_parse_version(text, parsed):
    assert release.parse_version(text) == parsed


def test_only_a_plainly_higher_version_is_newer():
    assert release.is_newer("0.4.1", "0.4.0") and release.is_newer("v1.0", "0.9.9")
    assert release.is_newer("0.10.0", "0.9.0")  # by number, not by spelling
    assert not release.is_newer("0.4.0", "0.4.0") and not release.is_newer("0.4", "0.4.0")
    assert not release.is_newer("0.3.9", "0.4.0")  # never offers to go backwards
    assert not release.is_newer("0.5.0-beta", "0.4.0") and not release.is_newer("0.5", "dev")


def test_a_release_is_read_and_untrustworthy_downloads_are_left_out():
    found = release.parse_release(GITHUB)
    assert (found.version, found.title, found.notes) == ("0.5.0", "Bookwyrm 0.5.0",
                                                        "## New\n- Dice")
    assert [asset.name for asset in found.assets] == ["Bookwyrm-windows.zip",
                                                      "Bookwyrm-mac-arm64.zip"]
    assert found.assets[1].sha256 == "cd" * 32 and found.assets[1].size == 90000000
    assert release.asset_for(found, "darwin", "arm64").name == "Bookwyrm-mac-arm64.zip"
    assert release.asset_for(found, "win32", "AMD64").name == "Bookwyrm-windows.zip"
    assert release.asset_for(found, "darwin", "x86_64") is None  # no Intel build in it
    assert release.asset_for(found, "linux", "x86_64") is None

    def with_asset(**changes):
        entry = dict(GITHUB["assets"][1], **changes)
        return release.parse_release(dict(GITHUB, assets=[entry])).assets

    assert with_asset(browser_download_url="https://example.com/Bookwyrm-mac-arm64.zip") == ()
    assert with_asset(browser_download_url="http://github.com/Saberofblue/Bookwyrm/"
                                           "releases/download/v0.5.0/x.zip") == ()
    assert with_asset(digest=None) == () and with_asset(digest="md5:abcd") == ()
    assert with_asset(digest="sha256:nothex") == () and with_asset(size=0) == ()
    assert with_asset(browser_download_url="file:///tmp/x.zip") == ()  # only in a rehearsal
    for bad in (None, [], {}, {"tag_name": "nightly"}, dict(GITHUB, draft=True),
                dict(GITHUB, prerelease=True)):
        with pytest.raises(UpdateError):
            release.parse_release(bad)
    elsewhere = release.parse_release(dict(GITHUB, html_url="https://example.com/x"))
    assert elsewhere.page == release.RELEASES_PAGE  # never a page somewhere else


def test_a_rehearsal_feed_may_only_name_local_files(tmp_path, monkeypatch):
    assert release.source() == release.Source(release.LATEST_URL, False)
    feed_file = tmp_path / "feed.json"
    monkeypatch.setenv(release.FEED_VARIABLE, str(feed_file))
    assert release.source() == release.Source(feed_file.resolve().as_uri(), True)
    local = dict(GITHUB["assets"][1], browser_download_url=feed_file.as_uri())
    found = release.parse_release(dict(GITHUB, assets=[GITHUB["assets"][0], local]),
                                  rehearsal=True)
    assert [asset.url for asset in found.assets] == [feed_file.as_uri()]  # the web one is out


def test_download_names_by_computer():
    assert release.asset_name("darwin", "arm64") == "Bookwyrm-mac-arm64.zip"
    assert release.asset_name("darwin", "x86_64") == "Bookwyrm-mac-x86_64.zip"
    assert release.asset_name("win32", "AMD64") == "Bookwyrm-windows.zip"
    assert release.asset_name("linux", "x86_64") is None
    assert release.megabytes(108140723) == "103 MB" and release.megabytes(5 << 20) == "5.0 MB"


# -- fetching --------------------------------------------------------------------------------


def test_fetcher_reads_a_notice_and_explains_failures(qtbot, tmp_path):
    fetcher = Fetcher()
    good = tmp_path / "good.json"
    good.write_text(json.dumps(GITHUB), "utf-8")
    with qtbot.waitSignal(fetcher.notice, timeout=5000) as heard:
        fetcher.read_notice(good.as_uri())
    assert heard.args == [GITHUB, ""] and not fetcher.busy()
    (tmp_path / "bad.json").write_text("<html>not json", "utf-8")
    with qtbot.waitSignal(fetcher.notice, timeout=5000) as heard:
        fetcher.read_notice((tmp_path / "bad.json").as_uri())
    assert heard.args == [None, "The release notice could not be understood."]
    with qtbot.waitSignal(fetcher.notice, timeout=5000) as heard:
        fetcher.read_notice((tmp_path / "missing.json").as_uri())
    assert heard.args[0] is None and heard.args[1]


def test_fetcher_downloads_a_file_and_refuses_one_larger_than_announced(qtbot, tmp_path):
    fetcher = Fetcher()
    source_file = tmp_path / "source.bin"
    source_file.write_bytes(os.urandom(300_000))
    target = tmp_path / "target.bin"
    seen = []
    fetcher.progressed.connect(lambda done, total: seen.append((done, total)))
    with qtbot.waitSignal(fetcher.downloaded, timeout=10000) as done:
        fetcher.download(source_file.as_uri(), str(target), 300_000)
    assert done.args == [""] and target.read_bytes() == source_file.read_bytes()
    assert seen and seen[-1] == (300_000, 300_000)
    with qtbot.waitSignal(fetcher.downloaded, timeout=10000) as done:
        fetcher.download(source_file.as_uri(), str(target), 1000)  # announced as 1000 bytes
    assert "larger than the release said" in done.args[0] and not target.exists()
    with qtbot.waitSignal(fetcher.downloaded, timeout=10000) as done:
        fetcher.download((tmp_path / "nothing.bin").as_uri(), str(target), 10)
    assert done.args[0] and done.args[0] != CANCELLED and not target.exists()


# -- stand-ins for the packaged app --------------------------------------------------------------


def fake_app(folder, version, passes=True, says=None, bundle_id=mac.BUNDLE_ID):
    """A stand-in for Bookwyrm.app that answers --version and --self-test as the real one does."""
    app = folder / "Bookwyrm.app"
    (app / "Contents" / "MacOS").mkdir(parents=True)
    with open(app / "Contents" / "Info.plist", "wb") as handle:
        plistlib.dump({"CFBundleIdentifier": bundle_id, "CFBundleExecutable": "Bookwyrm"}, handle)
    verdict = "RESULT: all 9 checks passed" if passes else "RESULT: 1 of 9 checks FAILED"
    code = 0 if passes else 1
    program = app / "Contents" / "MacOS" / "Bookwyrm"
    program.write_text(
        "#!/bin/sh\n"
        f'if [ "$1" = "--version" ]; then echo "{says or version}" > "$2"; exit 0; fi\n'
        f'if [ "$1" = "--self-test" ]; then echo "{verdict}" > "$2"; exit {code}; fi\n',
        "utf-8")
    program.chmod(0o755)
    (app / "Contents" / "which").write_text(version, "utf-8")  # tells the copies apart
    return app


def which(app) -> str:
    return (app / "Contents" / "which").read_text("utf-8")


def zipped(app, target):
    subprocess.check_call(["/usr/bin/ditto", "-c", "-k", "--keepParent", str(app), str(target)])
    return target


def write_feed(folder, version, archive=None, digest=None, name=None):
    data = {"tag_name": f"v{version}", "name": f"Bookwyrm {version}",
            "body": "## What is new\n- A **rehearsal**.", "html_url": folder.as_uri(),
            "draft": False, "prerelease": False, "assets": []}
    if archive is not None:
        data["assets"].append({
            "name": name or release.asset_name(), "size": archive.stat().st_size,
            "digest": "sha256:" + (digest or release.file_sha256(archive)),
            "browser_download_url": archive.as_uri()})
    path = folder / "feed.json"
    path.write_text(json.dumps(data), "utf-8")
    return path


def finished_pid() -> int:
    process = subprocess.Popen(["/usr/bin/true"])
    process.wait()
    return process.pid


def wait_for(condition, seconds=20):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.05)
    return False


@pytest.fixture
def stage(tmp_path, monkeypatch):
    """An installed copy at version 1.0.0, a release of 2.0.0 to update to, and an updater."""
    installed = fake_app(tmp_path / "Applications", "1.0.0")
    archive = zipped(fake_app(tmp_path / "built", "2.0.0"), tmp_path / release.asset_name())
    monkeypatch.setenv(release.FEED_VARIABLE, str(write_feed(tmp_path, "2.0.0", archive)))
    return {"installed": installed, "archive": archive, "folder": tmp_path,
            "staging": tmp_path / "staging"}


def make_updater(stage, current="1.0.0"):
    return Updater(current=current, staging=stage["staging"], bundle=stage["installed"])


# -- is there a newer version? ----------------------------------------------------------------


def test_check_reports_a_newer_release_and_nothing_otherwise(qtbot, tmp_path, monkeypatch):
    monkeypatch.setenv(release.FEED_VARIABLE, str(write_feed(tmp_path, "2.0.0")))
    for current, expected in (("1.9.0", "2.0.0"), ("2.0.0", None), ("2.1.0", None)):
        checker = Updater(current=current, staging=tmp_path / "staging", bundle=None)
        with qtbot.waitSignal(checker.checked, timeout=5000) as heard:
            checker.check()
        found = heard.args[0]
        assert (found.version if found else None) == expected
        assert checker.latest.version == "2.0.0" and not checker.busy()
    (tmp_path / "feed.json").write_text('{"tag_name": "nightly"}', "utf-8")
    checker = Updater(current="1.0.0", staging=tmp_path / "staging", bundle=None)
    with qtbot.waitSignal(checker.failed, timeout=5000) as heard:
        checker.check()
    assert heard.args == ["The release notice could not be understood."]
    assert not (tmp_path / "staging").exists()  # looking changes nothing on disk


def test_why_an_update_cannot_install_itself(qtbot, tmp_path, monkeypatch):
    monkeypatch.setenv(release.FEED_VARIABLE, str(write_feed(tmp_path, "2.0.0")))
    checker = Updater(current="1.0.0", staging=tmp_path / "staging", bundle=None)
    assert checker.blocked() == "Check for updates first."
    with qtbot.waitSignal(checker.checked, timeout=5000):
        checker.check()
    reason = checker.blocked()
    if MAC:
        assert reason == "This release has no download for this kind of Mac."
    else:
        assert "cannot install an update by itself yet" in reason
    with qtbot.waitSignal(checker.failed, timeout=5000) as heard:
        checker.prepare()  # refused, with the same reason
    assert heard.args == [reason] and not (tmp_path / "staging").exists()
    assert "source code" in mac.why_not(None)
    moved = tmp_path / "AppTranslocation" / "X" / "d" / "Bookwyrm.app"
    assert "Applications folder" in mac.why_not(moved)
    with pytest.raises(UpdateError):
        checker.install()  # nothing has been prepared


# -- fetching and proving the new version (Mac) ---------------------------------------------------


@mac_only
def test_update_is_fetched_proven_and_swapped_in(qtbot, stage):
    updating = make_updater(stage)
    with qtbot.waitSignal(updating.checked, timeout=5000) as heard:
        updating.check()
    assert heard.args[0].version == "2.0.0" and updating.blocked() is None
    steps = []
    updating.progressed.connect(lambda doing, _done, _total: steps.append(doing))
    with qtbot.waitSignal(updating.ready, timeout=30000):
        updating.prepare()
    assert updating.is_ready and not updating.busy()
    assert list(dict.fromkeys(steps)) == ["Downloading", "Checking the download", "Unpacking",
                                          "Trying the new version"]
    assert which(stage["installed"]) == "1.0.0"  # nothing replaced yet
    assert not (stage["staging"] / "download.zip").exists()

    updating.install(relaunch=False, pid=finished_pid())
    assert wait_for(lambda: (stage["staging"] / mac.OUTCOME).exists())
    assert which(stage["installed"]) == "2.0.0"
    program = mac.executable_of(stage["installed"])
    assert os.access(program, os.X_OK)  # still a program that can be run
    applications = stage["installed"].parent
    assert wait_for(lambda: sorted(p.name for p in applications.iterdir()) == ["Bookwyrm.app"])
    assert updater.take_outcome(stage["staging"]) == (True, "installed")
    assert updater.take_outcome(stage["staging"]) is None  # reported once
    assert (stage["staging"] / mac.STARTED).exists()


@mac_only
@pytest.mark.parametrize("fault, words", [
    ("checksum", "did not match the checksum"),
    ("selftest", "did not pass its own checks"),
    ("version", "did not start properly"),
    ("not bookwyrm", "did not contain Bookwyrm"),
])
def test_a_bad_download_is_refused_and_nothing_is_changed(qtbot, stage, monkeypatch, fault,
                                                          words):
    folder = stage["folder"]
    if fault == "checksum":
        write_feed(folder, "2.0.0", stage["archive"], digest="0" * 64)
    else:
        built = folder / "other"
        app = fake_app(built, "2.0.0", passes=fault != "selftest",
                       says="1.5.0" if fault == "version" else None,
                       bundle_id="com.example.other" if fault == "not bookwyrm" else mac.BUNDLE_ID)
        write_feed(folder, "2.0.0", zipped(app, folder / "other.zip"))
    updating = make_updater(stage)
    with qtbot.waitSignal(updating.checked, timeout=5000):
        updating.check()
    with qtbot.waitSignal(updating.failed, timeout=30000) as heard:
        updating.prepare()
    assert words in heard.args[0]
    assert not updating.is_ready and not updating.busy()
    assert which(stage["installed"]) == "1.0.0" and not stage["staging"].exists()
    with pytest.raises(UpdateError):
        updating.install(relaunch=False, pid=finished_pid())


@mac_only
def test_an_update_can_be_cancelled_and_tried_again(qtbot, stage):
    updating = make_updater(stage)
    with qtbot.waitSignal(updating.checked, timeout=5000):
        updating.check()
    updating.prepare()
    updating.cancel()
    qtbot.waitUntil(lambda: not updating.busy(), timeout=10000)
    assert not updating.is_ready and not stage["staging"].exists()
    with qtbot.waitSignal(updating.ready, timeout=30000):
        updating.prepare()
    assert updating.is_ready


# -- the swap step itself (Mac) --------------------------------------------------------------------


def opener(folder, body: str) -> str:
    """A stand-in for the program that opens an app."""
    script = folder / "opener.sh"
    script.write_text(f'#!/bin/sh\necho "$1" >> "{folder}/opened.txt"\n{body}\n', "utf-8")
    script.chmod(0o755)
    return str(script)


@mac_only
def test_swap_waits_for_the_app_to_close(tmp_path):
    old = fake_app(tmp_path / "Applications", "1.0.0")
    new = fake_app(tmp_path / "staging" / "new", "2.0.0")
    running = subprocess.Popen(["/bin/sleep", "30"])
    try:
        swap = mac.start_swap(old, new, tmp_path / "staging", relaunch=False, pid=running.pid)
        time.sleep(1.0)
        assert which(old) == "1.0.0" and swap.poll() is None  # still waiting
    finally:
        running.kill()
        running.wait()
    assert swap.wait(timeout=20) == 0
    assert which(old) == "2.0.0" and not new.exists()
    assert mac.take_outcome(tmp_path / "staging") == (True, "installed")
    assert "waiting for Bookwyrm" in (tmp_path / "staging" / "swap.log").read_text("utf-8")


@mac_only
def test_swap_starts_the_new_version_and_keeps_it_when_it_comes_up(tmp_path):
    old = fake_app(tmp_path / "Applications", "1.0.0")
    new = fake_app(tmp_path / "staging" / "new", "2.0.0")
    staging = tmp_path / "staging"
    comes_up = opener(tmp_path, f'touch "{staging}/started"')  # as the new version does
    swap = mac.start_swap(old, new, staging, relaunch=True, pid=finished_pid(),
                          testing=(comes_up, 4))
    assert swap.wait(timeout=30) == 0
    assert which(old) == "2.0.0"
    assert (tmp_path / "opened.txt").read_text("utf-8").split() == [str(old)]
    assert sorted(p.name for p in old.parent.iterdir()) == ["Bookwyrm.app"]
    assert mac.take_outcome(staging) == (True, "installed")


@mac_only
def test_swap_puts_the_old_version_back_when_the_new_one_does_not_start(tmp_path):
    old = fake_app(tmp_path / "Applications", "1.0.0")
    new = fake_app(tmp_path / "staging" / "new", "2.0.0")
    staging = tmp_path / "staging"
    never = opener(tmp_path, "true")  # "opens" it, but nothing ever comes up
    swap = mac.start_swap(old, new, staging, relaunch=True, pid=finished_pid(),
                          testing=(never, 2))
    assert swap.wait(timeout=30) == 1
    assert which(old) == "1.0.0"  # the old version is back
    assert sorted(p.name for p in old.parent.iterdir()) == ["Bookwyrm.app"]
    assert (tmp_path / "opened.txt").read_text("utf-8").split() == [str(old), str(old)]
    worked, note = mac.take_outcome(staging)
    assert not worked and note == "the new version did not start, so the old one was put back."


@mac_only
def test_swap_keeps_a_new_version_that_is_running_even_if_it_leaves_no_note(tmp_path):
    old = fake_app(tmp_path / "Applications", "1.0.0")
    new = fake_app(tmp_path / "staging" / "new", "2.0.0")
    # A real program that keeps running, started by a path inside the app (a link: macOS
    # will not run a copy of one of its own programs from elsewhere).
    os.symlink("/bin/sleep", new / "Contents" / "MacOS" / "Lingerer")
    staging = tmp_path / "staging"
    lingers = opener(tmp_path, '"$1/Contents/MacOS/Lingerer" 25 &')  # runs, writes no note
    swap = mac.start_swap(old, new, staging, relaunch=True, pid=finished_pid(),
                          testing=(lingers, 2))
    try:
        assert swap.wait(timeout=40) == 0  # seen to be running, so it is left in place
        assert which(old) == "2.0.0"
        assert sorted(p.name for p in old.parent.iterdir()) == ["Bookwyrm.app"]
        assert mac.take_outcome(staging) == (True, "installed")
    finally:
        subprocess.run(["/usr/bin/pkill", "-f", str(old / "Contents" / "MacOS" / "Lingerer")],
                       check=False)


@mac_only
def test_swap_leaves_the_old_version_alone_when_the_new_one_is_missing(tmp_path):
    old = fake_app(tmp_path / "Applications", "1.0.0")
    (tmp_path / "staging").mkdir()
    swap = mac.start_swap(old, tmp_path / "staging" / "gone.app", tmp_path / "staging",
                          relaunch=False, pid=finished_pid())
    assert swap.wait(timeout=20) == 1 and which(old) == "1.0.0"
    worked, note = mac.take_outcome(tmp_path / "staging")
    assert not worked and "could not be copied" in note


@mac_only
def test_finding_the_app_in_a_download_and_where_it_may_be_replaced(tmp_path):
    good = fake_app(tmp_path / "one", "2.0.0")
    assert mac.find_app(tmp_path / "one") == good
    assert mac.executable_of(good) == good / "Contents" / "MacOS" / "Bookwyrm"
    for folder, make in (("none", lambda f: f.mkdir()),
                         ("other", lambda f: fake_app(f, "2.0.0", bundle_id="org.other")),
                         ("empty", lambda f: (f / "Bookwyrm.app").mkdir(parents=True))):
        make(tmp_path / folder)
        with pytest.raises(UpdateError):
            mac.find_app(tmp_path / folder)
    assert mac.why_not(good) is None
    assert mac.bundle_path("/Applications/Bookwyrm.app/Contents/MacOS/Bookwyrm",
                           frozen=True).name == "Bookwyrm.app"
    assert mac.bundle_path("/usr/bin/python3", frozen=False) is None
    if os.geteuid() != 0:
        locked = tmp_path / "locked"
        app = fake_app(locked, "1.0.0")
        locked.chmod(0o555)
        try:
            assert "not allowed to change the folder" in mac.why_not(app)
        finally:
            locked.chmod(0o755)


def test_the_outcome_is_reported_once_and_abandoned_downloads_are_cleared(tmp_path):
    staging = tmp_path / "staging"
    assert updater.take_outcome(staging) is None and not staging.exists()
    (staging / "new" / "Bookwyrm.app").mkdir(parents=True)
    (staging / "download.zip").write_bytes(b"left over")
    (staging / mac.OUTCOME).write_text("failed: the old version could not be moved aside.\n",
                                       "utf-8")
    assert updater.take_outcome(staging) == (False, "the old version could not be moved aside.")
    assert sorted(p.name for p in staging.iterdir()) == [mac.STARTED]
    assert updater.take_outcome(staging) is None


# -- the window ----------------------------------------------------------------------------------


def test_dialog_says_when_bookwyrm_is_up_to_date_or_the_check_fails(qtbot, tmp_path,
                                                                     monkeypatch):
    from bookwyrm.ui.update_dialog import UpdateDialog

    monkeypatch.setenv(release.FEED_VARIABLE, str(write_feed(tmp_path, "2.0.0")))
    dialog = UpdateDialog(Updater(current="2.0.0", staging=tmp_path / "s", bundle=None))
    qtbot.addWidget(dialog)
    assert dialog.heading.text() == "Checking for updates…"
    qtbot.waitUntil(lambda: dialog.state == "current", timeout=5000)
    assert dialog.heading.text() == "Bookwyrm is up to date"
    assert dialog.status.text() == "You have version 2.0.0, the newest there is."
    assert dialog.primary.isHidden() and dialog.notes.isHidden()

    (tmp_path / "feed.json").unlink()
    dialog = UpdateDialog(Updater(current="2.0.0", staging=tmp_path / "s", bundle=None))
    qtbot.addWidget(dialog)
    qtbot.waitUntil(lambda: dialog.state == "failed", timeout=5000)
    assert dialog.heading.text() == "Could not check for updates"
    assert dialog.primary.text() == "Try Again"
    write_feed(tmp_path, "2.0.0")
    dialog.primary.click()
    qtbot.waitUntil(lambda: dialog.state == "current", timeout=5000)


def test_dialog_offers_the_download_page_when_it_cannot_install(qtbot, tmp_path, monkeypatch):
    from bookwyrm.ui import update_dialog

    monkeypatch.setenv(release.FEED_VARIABLE, str(write_feed(tmp_path, "2.0.0")))
    opened = []
    monkeypatch.setattr(update_dialog.QDesktopServices, "openUrl",
                        lambda url: opened.append(url.toString()))
    dialog = update_dialog.UpdateDialog(
        Updater(current="1.0.0", staging=tmp_path / "s", bundle=None))  # run from source
    qtbot.addWidget(dialog)
    qtbot.waitUntil(lambda: dialog.state == "available", timeout=5000)
    assert dialog.heading.text() == "Bookwyrm 2.0.0 is available"
    assert dialog.status.text().startswith("You have version 1.0.0.")
    assert "rehearsal" in dialog.notes.toPlainText() and not dialog.notes.isHidden()
    assert dialog.primary.text() == "Open the Download Page"
    assert dialog.secondary.text() == "Not Now"
    dialog.primary.click()
    assert opened == [tmp_path.as_uri()]
    assert not (tmp_path / "s").exists()  # nothing was downloaded


@mac_only
def test_dialog_updates_and_asks_to_restart(qtbot, stage):
    from bookwyrm.ui.update_dialog import UpdateDialog

    updating = make_updater(stage)
    dialog = UpdateDialog(updating)
    qtbot.addWidget(dialog)
    qtbot.waitUntil(lambda: dialog.state == "available", timeout=5000)
    assert dialog.primary.text() == "Update and Restart"
    with qtbot.waitSignal(dialog.restartRequested, timeout=30000):
        dialog.primary.click()
        assert dialog.state == "preparing" and dialog.secondary.text() == "Cancel"
        assert not dialog.bar.isHidden()
    assert dialog.state == "ready" and dialog.heading.text() == "Bookwyrm 2.0.0 is ready"
    assert dialog.primary.text() == "Restart Now"
    with qtbot.waitSignal(dialog.restartRequested, timeout=2000):
        dialog.primary.click()  # asked again after a restart that was put off
    again = UpdateDialog(updating)  # opened again later: the new version is still waiting
    qtbot.addWidget(again)
    assert again.state == "ready"
    assert which(stage["installed"]) == "1.0.0"  # the dialog itself replaces nothing


@mac_only
def test_dialog_goes_back_to_the_offer_when_a_download_is_cancelled(qtbot, stage):
    from bookwyrm.ui.update_dialog import UpdateDialog

    dialog = UpdateDialog(make_updater(stage))
    qtbot.addWidget(dialog)
    qtbot.waitUntil(lambda: dialog.state == "available", timeout=5000)
    dialog.primary.click()
    dialog.secondary.click()  # Cancel
    assert dialog.state == "available" and dialog.primary.text() == "Update and Restart"
    assert dialog.result() != QDialog.DialogCode.Accepted and not dialog.updater.is_ready


def test_settings_show_the_version_and_ask_for_a_check(qtbot, tmp_path):
    from bookwyrm import __version__
    from bookwyrm.db.repo import Library
    from bookwyrm.ui.settings import SettingsDialog

    library = Library(tmp_path / "lib")
    try:
        dialog = SettingsDialog(library)
        qtbot.addWidget(dialog)
        assert f"This is Bookwyrm {__version__}." in dialog.version.text()
        assert "only when you ask" in dialog.version.text()
        with qtbot.waitSignal(dialog.updateCheckRequested, timeout=1000):
            dialog.check_updates.click()
    finally:
        library.close()


class _Stub:
    """Stands in for the updater and the dialog when the window is asked to restart."""

    def __init__(self, fails: bool = False):
        self.fails, self.calls = fails, []

    def install(self):
        self.calls.append("install")
        if self.fails:
            raise UpdateError("The update could not be started: no shell")

    def call_off(self):
        self.calls.append("call_off")

    def show_failure(self, problem):
        self.calls.append(("failure", problem))

    def waiting_to_restart(self):
        self.calls.append("waiting")

    def accept(self):
        self.calls.append("accept")


def test_the_window_closes_for_an_update_only_once_the_swap_is_waiting(qtbot, tmp_path,
                                                                       monkeypatch):
    from bookwyrm.db.repo import Library
    from bookwyrm.ui.main_window import MainWindow

    library = Library(tmp_path / "lib")
    try:
        window = MainWindow(library)
        qtbot.addWidget(window)
        window.show()
        window._updater, dialog = _Stub(fails=True), _Stub()
        window._restart_for_update(dialog)  # the swap could not be started
        assert dialog.calls == [("failure", "The update could not be started: no shell")]
        assert window.isVisible() and not window._released

        window._updater, dialog = _Stub(), _Stub()
        monkeypatch.setattr(window, "release_library", lambda: False)  # the user stays
        window._restart_for_update(dialog)
        assert window._updater.calls == ["install", "call_off"] and dialog.calls == ["waiting"]
        assert window.isVisible()

        monkeypatch.undo()
        window._updater, dialog = _Stub(), _Stub()
        window._restart_for_update(dialog)
        assert window._updater.calls == ["install"] and dialog.calls == ["accept"]
        assert window._released and not window.isVisible()
    finally:
        library.close()


def test_a_restart_after_an_update_says_how_it_went(qtbot, tmp_path, monkeypatch):
    from bookwyrm import __version__, app
    from bookwyrm.ui.library import dialogs

    staging = tmp_path / "staging"
    monkeypatch.setattr(updater, "default_staging", lambda: staging)
    said, complaints = [], []

    class Window:
        def notice(self, text):
            said.append(text)

    monkeypatch.setattr(dialogs, "complain", lambda _parent, text: complaints.append(text))
    app._report_update(Window())  # an ordinary start: nothing to say
    assert said == [] and complaints == []
    staging.mkdir()
    (staging / mac.OUTCOME).write_text("installed\n", "utf-8")
    app._report_update(Window())
    assert said == [f"Bookwyrm has been updated to version {__version__}."]
    assert (staging / mac.STARTED).exists()  # the waiting swap step is told it came up
    (staging / mac.OUTCOME).write_text("failed: the new version did not start, so the old "
                                       "one was put back.\n", "utf-8")
    app._report_update(Window())
    qtbot.waitUntil(lambda: bool(complaints), timeout=2000)
    assert complaints[0].startswith("The update could not be installed: the new version did "
                                    "not start, so the old one was put back.")
    assert f"You are still using version {__version__}." in complaints[0]
