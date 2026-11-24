"""Application entry point and command line handling."""

from __future__ import annotations

import argparse

from textual.app import App

from . import __version__
from .p4client import P4Client
from .screens.changes import ChangesScreen


class P4kApp(App):
    """Terminal changelist browser for Perforce, in the spirit of gitk."""

    TITLE = "p4k"

    def __init__(self, client: P4Client, path: str, page_size: int, user: str | None) -> None:
        super().__init__()
        self.client = client
        self.path = path
        self.page_size = page_size
        self.user = user

    def on_mount(self) -> None:
        self.push_screen(ChangesScreen(self.client, self.path, self.page_size, self.user))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="p4k",
        description="Browse submitted Perforce changelists in the terminal.",
    )
    parser.add_argument(
        "path",
        nargs="?",
        default="//...",
        help="depot or client path to show history for (default: //...)",
    )
    parser.add_argument("-u", "--user", help="only show changelists submitted by this user")
    parser.add_argument(
        "-m",
        "--page-size",
        type=int,
        default=200,
        help="changelists fetched per request (default: 200)",
    )
    parser.add_argument("--p4", default="p4", help="p4 executable to use (default: p4)")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = parser.parse_args(argv)
    if args.page_size < 1:
        parser.error("--page-size must be positive")
    return args


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    app = P4kApp(P4Client(args.p4), args.path, args.page_size, args.user)
    app.run()


if __name__ == "__main__":
    main()
