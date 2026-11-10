"""Command-line entry point."""

from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    p = argparse.ArgumentParser(prog="aeroscan")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("fetch-data", help="download and verify the evaluation datasets")
    sub.add_parser(
        "eval", help="run the offline evaluation (see `aeroscan eval -h`)", add_help=False
    )
    args, rest = p.parse_known_args(argv)

    if args.cmd == "fetch-data":
        from .eval.datasets import ALL_FILES, fetch

        for rf in ALL_FILES:
            print(fetch(rf))
    elif args.cmd == "eval":
        from .eval.run import main as eval_main

        eval_main(rest)
