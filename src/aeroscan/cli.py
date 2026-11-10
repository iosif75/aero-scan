"""Command-line entry point."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

DEFAULT_MODEL = Path("models/triage_gbm.pkl")
DEFAULT_TRIAGE_DATA = Path("data/triage")


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    p = argparse.ArgumentParser(prog="aeroscan")
    sub = p.add_subparsers(dest="cmd", required=True)

    fd = sub.add_parser("fetch-data", help="download and verify the evaluation datasets")
    fd.add_argument(
        "--bcn",
        action="store_true",
        help="also build the 12 Barcelona days from adsb.lol "
        "(~35 GB downloaded, ~1 h, ~200 MB kept)",
    )
    sub.add_parser(
        "eval", help="run the offline evaluation (see `aeroscan eval -h`)", add_help=False
    )

    tr = sub.add_parser("triage", help="build the triage dataset and fit the triage model")
    tsub = tr.add_subparsers(dest="triage_cmd", required=True)
    tb = tsub.add_parser("build", help="turn loose-rule alerts into labelled examples")
    tb.add_argument("--out", type=Path, default=DEFAULT_TRIAGE_DATA)
    tf = tsub.add_parser("fit", help="fit the model and compare the pipeline with strict rules")
    tf.add_argument("--data", type=Path, default=DEFAULT_TRIAGE_DATA)
    tf.add_argument("--report", type=Path, default=Path("eval/results/report.json"))
    tf.add_argument("--model", type=Path, default=DEFAULT_MODEL)

    lv = sub.add_parser("live", help="monitor the Barcelona region from the OpenSky API")
    lv.add_argument("--interval", type=float, default=60.0, help="seconds between polls")
    lv.add_argument("--db", type=Path, default=Path("alerts.sqlite"))
    lv.add_argument("--model", type=Path, default=DEFAULT_MODEL, help="triage model, if built")
    lv.add_argument("--credentials", type=Path, default=Path("credentials.json"))
    lv.add_argument("--polls", type=int, default=None, help="stop after this many polls")

    args, rest = p.parse_known_args(argv)

    if args.cmd == "fetch-data":
        from .eval.datasets import ALL_FILES, fetch

        for rf in ALL_FILES:
            print(fetch(rf))
        if args.bcn:
            from .eval.adsblol import DEFAULT_DAYS_2025, extract_day

            for day in DEFAULT_DAYS_2025:
                print(extract_day(day))
    elif args.cmd == "eval":
        from .eval.run import main as eval_main

        eval_main(rest)
    elif args.cmd == "triage" and args.triage_cmd == "build":
        from .triage.dataset import build

        print(build(args.out))
    elif args.cmd == "triage" and args.triage_cmd == "fit":
        import json

        from .triage.model import main as fit_main

        result = fit_main(args.data, args.report, args.model)
        Path("eval/results/pipeline.json").write_text(json.dumps(result, indent=2))
        print(json.dumps(result, indent=2))
        print(f"model saved to {args.model}")
    elif args.cmd == "live":
        _live(args)


def _live(args: argparse.Namespace) -> None:
    from .live.opensky import Credentials, OpenSkyClient
    from .live.run import budget_warning, run
    from .live.store import AlertStore
    from .pipeline import Pipeline
    from .region import BARCELONA

    creds = Credentials.discover(args.credentials)
    client = OpenSkyClient(creds)
    pipeline = Pipeline.from_model_file(args.model)
    print(
        f"Monitoring {BARCELONA.name} ({BARCELONA.square_degrees:.0f} sq deg) every "
        f"{args.interval:.0f} s, {'authenticated' if creds else 'anonymous'}, "
        f"{pipeline.mode}; alerts -> {args.db}"
    )
    warning = budget_warning(BARCELONA, args.interval, client.authenticated)
    if warning:
        print(f"Note: {warning}")
    store = AlertStore(args.db)
    try:
        run(BARCELONA, pipeline, client, store, args.interval, args.polls)
    except KeyboardInterrupt:
        print("stopped")
    finally:
        store.close()


if __name__ == "__main__":
    main()
