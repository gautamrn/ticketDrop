from __future__ import annotations

import argparse
import asyncio
import contextlib

from . import app, config, log


def main() -> None:
    _parse_args()
    cfg = config.load()
    log.configure(level=cfg.log_level, fmt=cfg.log_format)

    # Ctrl-C on Windows arrives as KeyboardInterrupt rather than through the
    # signal handler, and it is a normal way to stop the process.
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(app.serve(cfg))


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="harness", description="TicketDrop agent harness")
    sub = parser.add_subparsers(dest="command")

    # Bare `python -m harness` already starts the worker, but once any subcommand
    # exists the obvious guess is `harness run`, and argparse answers that guess
    # with "invalid choice". Naming the default makes the guess correct.
    sub.add_parser("run", help="consume the drop stream (the default)")

    return parser.parse_args()


if __name__ == "__main__":
    main()
