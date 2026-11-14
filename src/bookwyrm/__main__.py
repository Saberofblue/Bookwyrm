"""Entry point: ``python -m bookwyrm [file.pdf]``.

The same executable also runs the helper processes (``--host <pipe> <path>`` and
``--worker <pipe>``), which must not pay for or depend on Qt, so heavy imports
happen inside the branches. ``--self-test [report.txt]`` checks an installation.
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

    from bookwyrm.app import run

    return run()


if __name__ == "__main__":
    sys.exit(main())
