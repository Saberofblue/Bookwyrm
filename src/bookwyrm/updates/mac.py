"""Putting a new version of Bookwyrm in place of the running one, on a Mac.

A Mac app is a folder (Bookwyrm.app). The running app cannot safely replace its own
folder, so it hands the job to a small shell script and quits. The script waits for
the app to be gone, moves the new app to where the old one was, and starts it. Should
the new version fail to come up, the script puts the old one back.

Before any of that, the new app is unpacked beside nothing it could harm and made to
prove itself: it must report the expected version and pass its own self-test.
"""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path

from bookwyrm.updates.release import UpdateError

BUNDLE_ID = "org.bookwyrm.Bookwyrm"
OUTCOME, STARTED = "outcome.txt", "started"
INSTALLED = "installed"  # the outcome note left when the swap went through

SWAP_SCRIPT = r"""#!/bin/sh
# Bookwyrm's update step. Bookwyrm starts this and then quits. It waits for that, puts
# the new version where the old one was, and starts it. If the new version does not come
# up, the old one is put back.
#   $1 process id of the running Bookwyrm    $2 the app to replace    $3 the new app
#   $4 folder for notes (outcome.txt, started)    $5 "relaunch" to start the new version
#   $6 the program that opens an app, and $7 how many half-seconds to wait for the new
#      version to come up; both have defaults and are only given when this is tested
pid="$1"; app="$2"; new="$3"; notes="$4"; relaunch="$5"
opener="${6:-/usr/bin/open}"; patience="${7:-60}"
say() { printf '%s  %s\n' "$(date '+%H:%M:%S')" "$1"; }
outcome() { printf '%s\n' "$1" > "$notes/outcome.txt"; say "$1"; }
start() { if [ "$relaunch" = relaunch ]; then "$opener" "$app"; fi; }
# Is anything running from inside the app? (Its own program, at the start of a command
# line: looking for the path anywhere would also find the command doing the looking.)
running() {
    ps -axo command= | inside="$app/Contents/MacOS/" \
        awk 'index($0, ENVIRON["inside"]) == 1 { found = 1 } END { exit !found }'
}

say "waiting for Bookwyrm (process $pid) to close"
waited=0
while kill -0 "$pid" 2>/dev/null; do
    waited=$((waited + 1))
    if [ "$waited" -gt 3000 ]; then  # ten minutes
        outcome "failed: Bookwyrm did not close, so it was left as it is."
        exit 1
    fi
    sleep 0.2
done

folder=$(dirname "$app")
incoming="$folder/.Bookwyrm-incoming.app"
previous="$folder/.Bookwyrm-previous.app"
rm -rf "$incoming" "$previous" "$notes/started"

if ! mv "$new" "$incoming"; then
    rm -rf "$incoming"
    outcome "failed: the new version could not be copied next to the old one."
    start
    exit 1
fi
if ! mv "$app" "$previous"; then
    rm -rf "$incoming"
    outcome "failed: the old version could not be moved aside."
    start
    exit 1
fi
if ! mv "$incoming" "$app"; then
    mv "$previous" "$app"
    rm -rf "$incoming"
    outcome "failed: the new version could not be put in place, so the old one was put back."
    start
    exit 1
fi
outcome "installed"
if [ "$relaunch" != relaunch ]; then
    rm -rf "$previous"
    exit 0
fi

"$opener" "$app"
# The new version leaves a note ("started") once its window is up. Without one, and with
# nothing running after half a minute, it has failed to start.
waited=0
while [ ! -e "$notes/started" ]; do
    waited=$((waited + 1))
    if [ "$waited" -gt "$patience" ] && ! running; then break; fi
    if [ "$waited" -gt $((patience * 6)) ]; then break; fi  # still running: leave it be
    sleep 0.5
done
if [ -e "$notes/started" ] || running; then
    rm -rf "$previous"
    say "the new version is running"
    exit 0
fi
say "the new version did not start"
failed="$folder/.Bookwyrm-failed.app"
rm -rf "$failed"
mv "$app" "$failed" && mv "$previous" "$app" && rm -rf "$failed"
outcome "failed: the new version did not start, so the old one was put back."
"$opener" "$app"
exit 1
"""


def bundle_path(executable: str | None = None, frozen: bool | None = None) -> Path | None:
    """The Bookwyrm.app this process runs from, or None when it is not a packaged Mac app."""
    frozen = getattr(sys, "frozen", False) if frozen is None else frozen
    if not frozen or sys.platform != "darwin":
        return None
    path = Path(executable or sys.executable)
    return next((parent for parent in path.parents if parent.suffix == ".app"), None)


def why_not(bundle: Path | None) -> str | None:
    """Why Bookwyrm cannot replace itself here, or None if it can."""
    if bundle is None:
        return ("This copy of Bookwyrm is running from its source code, so it cannot "
                "replace itself. Update it the way you got it.")
    if "/AppTranslocation/" in str(bundle):
        return ("macOS is running Bookwyrm from a temporary place, where it cannot be "
                "replaced. Move Bookwyrm into your Applications folder, open it from "
                "there, and check for updates again.")
    if not os.access(bundle.parent, os.W_OK) or not os.access(bundle, os.W_OK):
        return (f"Bookwyrm is not allowed to change the folder it is kept in "
                f"({bundle.parent}), so it cannot replace itself there.")
    return None


def unpack_command(archive: Path, into: Path) -> list[str]:
    """ditto keeps the links and the signature inside an app intact; plain unzipping does not."""
    return ["/usr/bin/ditto", "-x", "-k", str(archive), str(into)]


def find_app(folder: Path) -> Path:
    """The one app inside an unpacked download, checked to be Bookwyrm."""
    apps = sorted(Path(folder).glob("*.app"))
    if len(apps) != 1:
        raise UpdateError("The download did not contain Bookwyrm.")
    try:
        with open(apps[0] / "Contents" / "Info.plist", "rb") as handle:
            info = plistlib.load(handle)
    except (OSError, plistlib.InvalidFileException, ValueError) as problem:
        raise UpdateError("The download did not contain Bookwyrm.") from problem
    if info.get("CFBundleIdentifier") != BUNDLE_ID or not executable_of(apps[0]).is_file():
        raise UpdateError("The download did not contain Bookwyrm.")
    return apps[0]


def executable_of(bundle: Path) -> Path:
    try:
        with open(bundle / "Contents" / "Info.plist", "rb") as handle:
            name = plistlib.load(handle).get("CFBundleExecutable") or "Bookwyrm"
    except (OSError, plistlib.InvalidFileException, ValueError):
        name = "Bookwyrm"
    return bundle / "Contents" / "MacOS" / name


def start_swap(bundle: Path, new_bundle: Path, staging: Path, relaunch: bool = True,
               pid: int | None = None, testing: tuple[str, int] | None = None
               ) -> subprocess.Popen:
    """Start the script that swaps `new_bundle` in for `bundle` once process `pid`
    (this one, by default) has gone. Returns the script's process. `testing` replaces the
    program that opens an app and the wait for it to come up (see the script)."""
    staging = Path(staging)
    script = staging / "swap.sh"
    script.write_text(SWAP_SCRIPT, "utf-8")
    (staging / OUTCOME).unlink(missing_ok=True)
    log = open(staging / "swap.log", "w")
    try:
        return subprocess.Popen(
            ["/bin/sh", str(script), str(os.getpid() if pid is None else pid), str(bundle),
             str(new_bundle), str(staging), "relaunch" if relaunch else "stay",
             *(str(part) for part in testing or ())],
            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
            start_new_session=True, close_fds=True)  # it must outlive this process
    except OSError as problem:
        raise UpdateError(f"The update could not be started: {problem}") from problem
    finally:
        log.close()


def take_outcome(staging: Path) -> tuple[bool, str] | None:
    """What the last swap left to say, if anything: (it worked, the note). Reading it
    removes it, so it is reported once."""
    note = Path(staging) / OUTCOME
    try:
        text = note.read_text("utf-8").strip()
        note.unlink()
    except OSError:
        return None
    if not text:
        return None
    return (text == INSTALLED, text.removeprefix("failed:").strip())


def mark_started(staging: Path) -> None:
    """Tell a waiting swap script that this version has come up."""
    try:
        if Path(staging).is_dir():
            (Path(staging) / STARTED).write_text("", "utf-8")
    except OSError:
        pass
