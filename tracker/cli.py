"""Shared CLI plumbing for the job scripts: parser construction and the
console/logging setup every entry point repeats."""

from __future__ import annotations

import argparse
import logging

from . import utf8_console

LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"


def build_parser(doc: str | None) -> argparse.ArgumentParser:
    """ArgumentParser preloaded with the shared -v/--verbose flag."""
    parser = argparse.ArgumentParser(
        description=doc, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser


def init_script(args: argparse.Namespace) -> None:
    """UTF-8 console + logging config — call right after parse_args()."""
    utf8_console()
    logging.basicConfig(
        level=logging.DEBUG if getattr(args, "verbose", False) else logging.INFO,
        format=LOG_FORMAT,
    )
