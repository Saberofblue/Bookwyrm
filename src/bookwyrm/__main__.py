"""Entry point: ``python -m bookwyrm [file.pdf]``.

The same executable also runs the helper processes (``--host <pipe> <path>`` and
``--worker <pipe>``), which must not pay for or depend on Qt, so heavy imports
happen inside the branches. ``--self-test [report.txt]`` checks an installation,
``--version`` names it, and ``--check-updates`` / ``--update`` look for a newer one.
"""

from __future__ import annotations

import sys


def _helper(role: str, start) -> int:
    """Run a helper process, logging anything that stops it (helpers have no console)."""
    from bookwyrm import log

    try:
        return start()
    except Exception:
        log.record_exception(f"the {role} helper stopped")
        raise


def main() -> int:
    argv = sys.argv
    if len(argv) == 4 and argv[1] == "--host":
        from bookwyrm.pdf.session_host import run

        return _helper("reader", lambda: run(int(argv[2]), argv[3]))
    if len(argv) == 3 and argv[1] == "--worker":
        from bookwyrm.library.worker import run

        return _helper("import", lambda: run(int(argv[2])))
    if len(argv) >= 2 and argv[1] == "--self-test":
        from bookwyrm.selftest import run

        return run(argv[2] if len(argv) > 2 else None)
    if len(argv) >= 2 and argv[1] == "--version":
        # Also written to a file when one is named: a windowed program has no console.
        from bookwyrm import __version__

        print(__version__)
        if len(argv) > 2:
            with open(argv[2], "w", encoding="utf-8") as report:
                report.write(__version__ + "\n")
        return 0
    if len(argv) >= 2 and argv[1] in ("--check-updates", "--update"):
        # The update steps without a window. --update installs a newer version if there
        # is one; add --no-relaunch to leave the new version unopened. A file name, if
        # given, receives what was said.
        from bookwyrm.updates.updater import run_headless

        rest = [word for word in argv[2:] if word != "--no-relaunch"]
        return run_headless(install=argv[1] == "--update",
                            relaunch="--no-relaunch" not in argv[2:],
                            report_path=rest[0] if rest else None)

    from bookwyrm.app import run

    return run()


if __name__ == "__main__":
    sys.exit(main())
