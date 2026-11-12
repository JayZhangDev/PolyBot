"""The ``polybot`` command: ``health``, ``discover``, ``markets``, ``stream``, ``pipeline`` and ``agent``.

Also reachable as ``python -m polybot <command>``, which works without
installing the package.
"""

from __future__ import annotations

import argparse


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="polybot", description="PolyBot: Polymarket research, paper trading only.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("health", help="show the last fetch, rows added in 24 hours and recent errors")
    sub.add_parser("discover", help="refresh the catalog of open markets now")
    sub.add_parser("markets", help="show the catalog and which markets the settings select for streaming")
    sub.add_parser("stream", help="record live order books and trades from the websocket until stopped")
    sub.add_parser("rollup", help="pack finished days, copy new snapshots from SQLite and build one-minute bars")
    sub.add_parser("migrate", help="copy the existing SQLite data into the warehouse and report the counts")
    sub.add_parser("query", help='ask the warehouse a question: polybot query "SELECT ..."')
    sub.add_parser("pipeline", help="run one hourly cycle now")
    sub.add_parser("agent", help="run one agent cycle now (extra options are passed through)")
    args, rest = parser.parse_known_args(argv)

    # Imported here so `polybot health` stays quick and does not load the trading code.
    if args.command == "health":
        from polybot.health import main as health_main

        return health_main()
    if args.command == "discover":
        from polybot.discovery import main as discover_main

        return discover_main()
    if args.command == "markets":
        from polybot.catalog import main as markets_main

        return markets_main()
    if args.command == "stream":
        from polybot.streamer import main as stream_main

        return stream_main()
    if args.command in ("rollup", "migrate"):
        from polybot import rollup

        return rollup.main() if args.command == "rollup" else rollup.migrate()
    if args.command == "query":
        from polybot.warehouse import main as query_main

        return query_main(rest)
    if args.command == "pipeline":
        from polybot.pipeline import main as pipeline_main

        return pipeline_main()
    from polybot.agent import main as agent_main

    return agent_main(rest)


if __name__ == "__main__":
    raise SystemExit(main())
