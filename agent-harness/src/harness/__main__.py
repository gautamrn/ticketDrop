from __future__ import annotations

import argparse
import asyncio
import contextlib
import sys

from . import app, config, embeddings, log, prefs
from .store import PgStore


def main() -> None:
    args = _parse_args()
    cfg = config.load()
    log.configure(level=cfg.log_level, fmt=cfg.log_format)

    if args.command == "prefs":
        asyncio.run(_prefs(cfg, args))
        return

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
    sub.add_parser("run", help="consume the drop stream and run the FSM (the default)")

    p = sub.add_parser("prefs", help="manage the preference set that hype scoring reads")
    psub = p.add_subparsers(dest="action", required=True)

    add = psub.add_parser("add", help="embed a label and store it as a preference")
    add.add_argument("--label", required=True, help="e.g. 'Prospa' or 'warehouse techno'")
    add.add_argument("--kind", default="artist", choices=prefs.TEXT_KINDS)
    add.add_argument("--weight", type=float, default=1.0)

    psub.add_parser("list", help="show the current preference set")

    return parser.parse_args()


async def _prefs(cfg: config.Config, args: argparse.Namespace) -> None:
    store = await PgStore.connect(cfg.database_url)
    embedder = embeddings.build(
        api_key=cfg.voyage_api_key,
        model=cfg.embedding_model,
        dimension=cfg.vector_dim,
        base_url=cfg.voyage_base_url,
        timeout_ms=cfg.embed_timeout_ms,
    )
    try:
        if args.action == "add":
            try:
                await prefs.add(
                    store, embedder, label=args.label, kind=args.kind, weight=args.weight
                )
            except ValueError as exc:
                print(f"error: {exc}", file=sys.stderr)
                raise SystemExit(2) from exc
            print(f"added {args.kind}: {args.label}")

        rows = await prefs.listing(store)
        if not rows:
            print("no preferences set; every drop will score at or below the demand weight")
            return
        print(f"{'kind':<10} {'weight':>6}  {'active':<6} label")
        for row in rows:
            print(f"{row.kind:<10} {row.weight:>6.2f}  {str(row.active).lower():<6} {row.label}")
    finally:
        await embedder.aclose()
        await store.aclose()


if __name__ == "__main__":
    main()
