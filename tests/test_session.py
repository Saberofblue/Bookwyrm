import pytest

from bookwyrm.pdf.session_client import DocumentSession
from bookwyrm.pdf.session_host import Host


class ScriptedConn:
    """A connection that replays queued requests and records replies."""

    def __init__(self, messages):
        self.incoming = list(messages)
        self.sent = []
        self.frames = []

    def recv(self):
        if not self.incoming:
            raise EOFError
        return self.incoming.pop(0)

    def poll(self):
        return bool(self.incoming)

    def send(self, msg):
        self.sent.append(msg)

    def send_bytes(self, data):
        self.frames.append(bytes(data))


def serve(sample_pdf, messages):
    conn = ScriptedConn(messages)
    host = Host(conn, str(sample_pdf))
    with pytest.raises(EOFError):
        host.serve()
    return conn


def test_host_reports_document_facts(sample_pdf):
    info = Host(ScriptedConn([]), str(sample_pdf)).info()
    assert info["page_count"] == 6
    assert info["sizes"][0] == (400, 600)
    assert info["title"] == "Test Tome"
    assert [entry[1] for entry in info["toc"]] == ["First", "Second", "Second-A"]


def test_host_serves_most_urgent_then_newest(sample_pdf):
    clip = (0, 0, 100, 100)
    conn = serve(sample_pdf, [
        ("tile", 1, 0, 1.0, clip, 2),
        ("tile", 2, 1, 1.0, clip, 1),
        ("preview", 3, 2, 100, 0),
        ("tile", 4, 3, 1.0, clip, 1),
    ])
    assert [msg[1] for msg in conn.sent] == [3, 4, 2, 1]


def test_host_skips_cancelled_requests(sample_pdf):
    clip = (0, 0, 100, 100)
    conn = serve(sample_pdf, [
        ("tile", 1, 0, 1.0, clip, 1),
        ("tile", 2, 1, 1.0, clip, 1),
        ("cancel", [2]),
    ])
    assert [msg[1] for msg in conn.sent] == [1]


def test_host_tile_geometry(sample_pdf):
    conn = serve(sample_pdf, [("tile", 1, 0, 2.0, (100, 50, 300, 250), 0)])
    op, req_id, width, height, stride = conn.sent[0]
    assert (op, req_id, width, height) == ("pixels", 1, 400, 400)
    assert len(conn.frames[0]) == stride * height


def test_host_survives_a_bad_request(sample_pdf):
    conn = serve(sample_pdf, [
        ("tile", 1, 999, 1.0, (0, 0, 10, 10), 0),
        ("tile", 2, 0, 1.0, (0, 0, 10, 10), 1),
    ])
    assert [msg[0] for msg in conn.sent] == ["failed", "pixels"]


def test_session_round_trip(qtbot, sample_pdf):
    session = DocumentSession(str(sample_pdf))
    try:
        with qtbot.waitSignal(session.opened, timeout=15000) as opened:
            pass
        assert opened.args[0]["page_count"] == 6
        with qtbot.waitSignal(session.pixels, timeout=15000) as pixels:
            req_id = session.tile(0, 2.0, (0, 0, 200, 150), 0)
        assert pixels.args[0] == req_id
        image = pixels.args[1]
        assert (image.width(), image.height()) == (400, 300)
    finally:
        session.close()


def test_session_reports_unreadable_file(qtbot, tmp_path):
    bad = tmp_path / "not-a-pdf.pdf"
    bad.write_bytes(b"this is not a pdf")
    session = DocumentSession(str(bad))
    try:
        with qtbot.waitSignal(session.crashed, timeout=15000):
            pass
    finally:
        session.close()
