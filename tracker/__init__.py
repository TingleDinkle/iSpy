"""AppstoreSpy market tracker: historical app metadata, metrics, and reviews."""

import sys

__version__ = "0.2.0"


def utf8_console() -> None:
    """Best-effort UTF-8 console. Windows defaults to cp1252, which crashes
    print/logging on game names containing emoji, CJK, or fullwidth chars
    (e.g. 'Block Blast！'). Call at the top of every CLI entry point."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass
