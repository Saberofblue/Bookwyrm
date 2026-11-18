"""The self-test and the Windows package: what a far-away user relies on."""

import importlib.util
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load(path: Path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_self_test_passes_and_writes_a_report(tmp_path):
    report = tmp_path / "report.txt"
    done = subprocess.run([sys.executable, "-m", "bookwyrm", "--self-test", str(report)],
                          capture_output=True, text=True, timeout=180)
    assert done.returncode == 0, done.stdout + done.stderr
    text = report.read_text("utf-8")
    assert "RESULT: all 10 checks passed" in text
    assert text.count("[ OK ]") == 10 and "[FAIL]" not in text
    assert "edit, undo, redo and save" in text
    assert "secure connections, for updates" in text


def test_self_test_reports_a_failure_instead_of_crashing(monkeypatch, capsys):
    from bookwyrm import selftest

    def broken(report, work):
        report.check("a check that works", lambda: "fine")
        report.check("a check that breaks", lambda: 1 / 0)
    monkeypatch.setattr(selftest, "_run_checks", broken)
    assert selftest.run() == 1
    printed = capsys.readouterr().out
    assert "[ OK ] a check that works" in printed
    assert "[FAIL] a check that breaks" in printed and "ZeroDivisionError" in printed
    assert "RESULT: 1 of 2 checks FAILED" in printed


def test_windows_package_holds_what_is_needed_and_nothing_private(tmp_path, monkeypatch):
    packager = load(ROOT / "packaging" / "make_windows_package.py")
    names = {str(p.relative_to(ROOT)).replace("\\", "/") for p in packager.package_files()}
    for needed in ("Install Bookwyrm.bat", "Start Bookwyrm.bat", "READ ME FIRST.txt", "LICENSE",
                   "installer/setup_bookwyrm.py", "packaging/Bookwyrm.ico",
                   "src/bookwyrm/__main__.py", "src/bookwyrm/db/schema.sql",
                   "src/bookwyrm/ui/theme/theme.qss", "src/bookwyrm/ui/theme/assets/CREDITS.md",
                   "src/bookwyrm/ui/theme/fonts/Cinzel[wght].ttf"):
        assert needed in names, needed
    assert not [n for n in names if n.startswith((".venv", "dist", "tests", "spikes", ".git"))]
    assert not [n for n in names if "__pycache__" in n or n.endswith(".pyc")]


def test_windows_scripts_are_plain_text_windows_can_run():
    for name in ("Install Bookwyrm.bat", "Start Bookwyrm.bat"):
        data = (ROOT / name).read_bytes()
        data.decode("ascii")  # batch files misbehave on anything else
        assert b"\r\n" in data and b"\n" not in data.replace(b"\r\n", b""), name
    assert b"setup_bookwyrm.py" in (ROOT / "Install Bookwyrm.bat").read_bytes()


def test_installer_quotes_paths_for_powershell():
    installer = load(ROOT / "installer" / "setup_bookwyrm.py")
    assert installer._quoted("C:\\Users\\O'Brien\\Bookwyrm") == "'C:\\Users\\O''Brien\\Bookwyrm'"
    assert installer.PACKAGES[1][1] == ["--no-deps", "pymupdf==2.1"]


def test_package_zip_is_built(tmp_path, monkeypatch):
    packager = load(ROOT / "packaging" / "make_windows_package.py")
    monkeypatch.setattr(packager, "ROOT", ROOT)
    target = ROOT / "dist" / "Bookwyrm-for-Windows.zip"
    assert packager.main() == 0 and target.exists()
    with zipfile.ZipFile(target) as archive:
        listed = archive.namelist()
    assert "Bookwyrm/Install Bookwyrm.bat" in listed and "Bookwyrm/READ ME FIRST.txt" in listed
    assert all(name.startswith("Bookwyrm/") for name in listed)
