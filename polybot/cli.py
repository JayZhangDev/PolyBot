"""The ``polybot`` command: ``polybot health``, ``polybot pipeline``, ``polybot agent``.

Also reachable as ``python -m polybot <command>``, which works without
installing the package.
"""

from __future__ import annotations

import argparse


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="polybot", description="PolyBot: Polymarket research, paper trading only.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("health", help="show the last fetch, rows added in 24 hours and recent errors")
    sub.add_parser("pipeline", help="run one hourly cycle now")
    sub.add_parser("agent", help="run one agent cycle now (extra options are passed through)")
    args, rest = parser.parse_known_args(argv)

    # Imported here so `polybot health` stays quick and does not load the trading code.
    if args.command == "health":
        from polybot.health import main as health_main

        return health_main()
    if args.command == "pipeline":
        from polybot.pipeline import main as pipeline_main

        return pipeline_main()
    from polybot.agent import main as agent_main

    return agent_main(rest)


if __name__ == "__main__":
    raise SystemExit(main())
