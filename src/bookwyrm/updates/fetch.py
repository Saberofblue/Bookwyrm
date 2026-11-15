"""Reading the release notice and downloading a file, without holding up the window."""

from __future__ import annotations

import json
import os

from PySide6.QtCore import QObject, QUrl, Signal
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest

from bookwyrm import __version__

NOTICE_LIMIT = 2 << 20  # bytes; a release notice is a few kilobytes
QUIET_AFTER = 30_000  # ms with nothing arriving before a transfer is given up
CANCELLED = "cancelled"

_Error = QNetworkReply.NetworkError
_OFFLINE = (_Error.HostNotFoundError, _Error.ConnectionRefusedError, _Error.TimeoutError,
            _Error.TemporaryNetworkFailureError, _Error.NetworkSessionFailedError,
            _Error.UnknownNetworkError, _Error.RemoteHostClosedError)


def explain(reply: QNetworkReply) -> str:
    """What went wrong with a transfer, for the user."""
    error = reply.error()
    status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
    if error in _OFFLINE:
        return "GitHub could not be reached. Check that this computer is online, then try again."
    if status in (403, 429):
        return ("GitHub is not answering requests from this network just now. "
                "Try again in a little while.")
    if status == 404:
        return "No release of Bookwyrm could be found."
    if error == _Error.SslHandshakeFailedError:
        return "A secure connection to GitHub could not be made, so nothing was downloaded."
    return f"The download did not work: {reply.errorString()}"


class Fetcher(QObject):
    """One errand at a time: read the release notice, or download a file.

    A reply's signals are connected to methods, never to closures that mention `self`:
    such a closure would be the last thing keeping the fetcher alive, and letting go of
    it while the reply is being deleted would delete the reply's own parent under it.
    """

    notice = Signal(object, str)  # the notice as parsed JSON (or None), and what went wrong
    progressed = Signal(int, int)  # bytes received, bytes expected (0 = not known)
    downloaded = Signal(str)  # '' when the file is complete; CANCELLED; else what went wrong

    def __init__(self, parent=None):
        super().__init__(parent)
        self._manager = QNetworkAccessManager(self)
        self._reply: QNetworkReply | None = None
        self._file = None
        self._target = ""
        self._limit = 0
        self._received = 0
        self._problem = ""
        self._cancelled = False

    def busy(self) -> bool:
        return self._reply is not None

    def _request(self, url: str) -> QNetworkRequest:
        request = QNetworkRequest(QUrl(url))
        request.setHeader(QNetworkRequest.KnownHeaders.UserAgentHeader, f"Bookwyrm/{__version__}")
        request.setTransferTimeout(QUIET_AFTER)
        # Redirects are followed by default, and never from a secure address to a plain one
        # (Qt 6's NoLessSafeRedirectPolicy): GitHub hands a download on to its file servers.
        return request

    # -- the release notice -----------------------------------------------------------

    def read_notice(self, url: str) -> None:
        request = self._request(url)
        request.setRawHeader(b"Accept", b"application/vnd.github+json")
        self._cancelled = False
        self._reply = self._manager.get(request)
        self._reply.finished.connect(self._notice_arrived)

    def _notice_arrived(self) -> None:
        reply, self._reply = self._reply, None
        if reply is None:
            return
        reply.deleteLater()
        if self._cancelled:
            return
        if reply.error() != _Error.NoError:
            self.notice.emit(None, explain(reply))
            return
        raw = bytes(reply.read(NOTICE_LIMIT + 1).data())
        try:
            if len(raw) > NOTICE_LIMIT:
                raise ValueError("too long")
            self.notice.emit(json.loads(raw.decode("utf-8")), "")
        except ValueError:
            self.notice.emit(None, "The release notice could not be understood.")

    # -- a download ---------------------------------------------------------------------

    def download(self, url: str, target: str, expected_size: int = 0) -> None:
        """Fetch `url` into the file `target`. A download that turns out larger than
        `expected_size` is stopped: it is not the file that was announced."""
        self._target, self._limit, self._received = str(target), expected_size, 0
        self._problem, self._cancelled = "", False
        self._file = open(self._target, "wb")
        self._reply = self._manager.get(self._request(url))
        self._reply.readyRead.connect(self._arrived)
        self._reply.finished.connect(self._download_finished)

    def _arrived(self) -> None:
        reply = self._reply
        if self._file is None or reply is None:
            return
        data = bytes(reply.readAll().data())
        self._received += len(data)
        if self._limit and self._received > self._limit:
            self._problem = "The download was larger than the release said it would be."
            reply.abort()
            return
        self._file.write(data)
        self.progressed.emit(self._received, self._limit)

    def _download_finished(self) -> None:
        reply = self._reply
        if reply is None:
            return
        if reply.error() == _Error.NoError:
            self._arrived()  # whatever was still waiting to be read
        self._reply = None
        reply.deleteLater()
        if self._file is not None:
            self._file.close()
            self._file = None
        problem = (CANCELLED if self._cancelled else self._problem
                   or ("" if reply.error() == _Error.NoError else explain(reply)))
        if problem:
            try:
                os.remove(self._target)
            except OSError:
                pass
        self.downloaded.emit(problem)

    def cancel(self) -> None:
        self._cancelled = True
        if self._reply is not None:
            self._reply.abort()
