"""Entry point: ``python -m nsh`` or the ``nsh`` console script."""
import asyncio
import sys

from . import __version__
from .app import NshApp

USAGE = """\
nsh - Not a SHell

Usage:
    nsh                 file-manager mode
    nsh shell           start in command-line mode
    nsh search [WORD]   fuzzy-pick a file; the selection is printed to stdout
    nsh -h | --help
    nsh -v | --version

Config: ~/.config/nsh/nshrc  (customize [colors] and [keys])"""


def main() -> None:
    # Don't crash printing help / a CJK search result on a console whose
    # encoding (e.g. cp949) can't represent every character.
    try:
        sys.stdout.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass

    args = sys.argv[1:]

    if args and args[0] in ("-h", "--help"):
        print(USAGE)
        return
    if args and args[0] in ("-v", "--version"):
        print(f"nsh {__version__}")
        return

    start_mode = None
    query = ""
    picker = False
    if args and args[0] == "search":
        start_mode = "search"
        picker = True
        query = " ".join(args[1:])
    elif args and args[0] == "shell":
        start_mode = "shell"

    app = NshApp(start_mode=start_mode, query=query, picker=picker)
    try:
        result = asyncio.run(app.run_async())
    except (KeyboardInterrupt, EOFError):
        result = None

    if result:
        print(result)


if __name__ == "__main__":
    main()
