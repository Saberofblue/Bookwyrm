"""The pipe between Bookwyrm and its helper processes, on macOS, Linux and Windows.

A helper is the same executable started with ``--host`` or ``--worker`` and the
number of one end of a two-way pipe. Unix hands that end over as an inherited
file descriptor; Windows as an inherited handle. Either way both sides end up
with a ``multiprocessing`` connection object with the same send/recv methods.

Qt-free: helpers import this too.
"""

from __future__ import annotations

import multiprocessing
import os
import subprocess
import sys

WINDOWS = sys.platform == "win32"


def helper_command(*args: str) -> list[str]:
    if getattr(sys, "frozen", False):  # packaged app: the executable is Bookwyrm itself
        return [sys.executable, *args]
    return [sys.executable, "-m", "bookwyrm", *args]


def start_helper(role: str, *args: str):
    """Start a helper process. Returns (process, our end of the pipe)."""
    ours, theirs = multiprocessing.Pipe(duplex=True)
    number = theirs.fileno()
    command = helper_command(role, str(number), *args)
    try:
        if WINDOWS:
            os.set_handle_inheritable(number, True)
            startup = subprocess.STARTUPINFO()
            startup.lpAttributeList = {"handle_list": [number]}  # inherit this handle only
            quiet = subprocess.DEVNULL if getattr(sys, "frozen", False) else None
            process = subprocess.Popen(
                command, stdin=subprocess.DEVNULL, stdout=quiet, stderr=quiet,
                startupinfo=startup, close_fds=True,
                creationflags=subprocess.CREATE_NO_WINDOW,  # no console window flashing up
            )
        else:
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, pass_fds=(number,))
    finally:
        theirs.close()  # the helper has its own copy now
    return process, ours


def connect(number: int):
    """In a helper: the connection for the pipe end whose number we were given."""
    from multiprocessing import connection

    if WINDOWS:
        return connection.PipeConnection(number)
    return connection.Connection(number)
