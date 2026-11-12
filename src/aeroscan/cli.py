"""Command-line entry point."""

from __future__ import annotations

import argparse
import sys
from datetime import timedelta
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
    tf.add_argument(
        "--out", type=Path, default=Path("eval/results/pipeline.json"), help="pipeline table"
    )

    lv = sub.add_parser("live", help="monitor the Barcelona region from the OpenSky API")
    sv = sub.add_parser("serve", help="run the monitor plus the REST API and map")
    for sp in (lv, sv):
        sp.add_argument("--interval", type=float, default=60.0, help="seconds between polls")
        sp.add_argument("--db", type=Path, default=Path("alerts.sqlite"))
        sp.add_argument("--model", type=Path, default=DEFAULT_MODEL, help="triage model, if built")
        sp.add_argument("--credentials", type=Path, default=Path("credentials.json"))
        sp.add_argument(
            "--keep-tracks-hours",
            type=float,
            default=24.0,
            help="hours of position history to keep for the API (default 24)",
        )
    lv.add_argument("--polls", type=int, default=None, help="stop after this many polls")
    sv.add_argument(
        "--host", default="127.0.0.1", help="interface to bind (default: this machine only)"
    )
    sv.add_argument("--port", type=int, default=8000)
    sv.add_argument(
        "--no-live", action="store_true", help="serve an existing database without polling"
    )
    sv.add_argument(
        "--no-admin", action="store_true", help="don't mount the Django admin at /admin"
    )

    ad = sub.add_parser("admin", help="manage the Django admin's users and alert reviews")
    asub = ad.add_subparsers(dest="admin_cmd", required=True)
    au = asub.add_parser("createuser", help="create an admin login (asks for a password)")
    au.add_argument("--username", default=None)
    al = asub.add_parser("labels", help="export the alert reviews as CSV")
    al.add_argument("--out", type=Path, default=None, help="file to write (default: stdout)")
    for sp in (sv, au, al):
        if sp is not sv:
            sp.add_argument("--db", type=Path, default=Path("alerts.sqlite"))
        sp.add_argument(
            "--admin-db",
            type=Path,
            default=None,
            help="admin users and reviews (default: <db>.admin.sqlite beside the database)",
        )

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
        args.out.write_text(json.dumps(result, indent=2))
        print(json.dumps(result, indent=2))
        print(f"model saved to {args.model}")
    elif args.cmd == "live":
        _live(args)
    elif args.cmd == "serve":
        _serve(args)
    elif args.cmd == "admin":
        _admin(args)


def _live(args: argparse.Namespace, max_polls: int | None = None) -> None:
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
    store.set_meta(
        {
            "mode": pipeline.mode,
            "region": BARCELONA.name,
            "interval_s": str(args.interval),
            "keep_tracks_hours": str(args.keep_tracks_hours),
        }
    )
    store.set_rules(pipeline.detector.rules)
    try:
        run(
            BARCELONA,
            pipeline,
            client,
            store,
            args.interval,
            max_polls or getattr(args, "polls", None),
            keep_tracks=timedelta(hours=args.keep_tracks_hours),
        )
    except KeyboardInterrupt:
        print("stopped")
    finally:
        store.close()


def _serve(args: argparse.Namespace) -> None:
    import threading

    import uvicorn

    from .api.app import create_app

    admin = not args.no_admin and _has_django()
    app = create_app(args.db, admin=admin, admin_db=args.admin_db)
    if not args.no_live:
        # The poller owns its own SQLite connection, created inside its thread.
        threading.Thread(target=_live, args=(args,), daemon=True, name="poller").start()
    base = f"http://{args.host}:{args.port}"
    print(f"Map and API: {base}/   docs: {base}/docs")
    if admin:
        print(f"Admin: {base}/admin/   (first login: `aeroscan admin createuser`)")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


def _has_django() -> bool:
    import importlib.util

    return importlib.util.find_spec("django") is not None


def _admin(args: argparse.Namespace) -> None:
    if not _has_django():
        sys.exit("The admin needs Django: install the `django` extra (uv sync --extra django).")
    from .console import setup

    setup.configure(args.db, args.admin_db)
    if args.admin_cmd == "createuser":
        from django.core.management import CommandError, call_command

        opts: dict[str, object] = {"username": args.username} if args.username else {}
        if not sys.stdin.isatty():  # scripted: password from DJANGO_SUPERUSER_PASSWORD
            opts.update(interactive=False, email="")
        try:
            call_command("createsuperuser", **opts)
        except CommandError as e:
            sys.exit(str(e))
    elif args.admin_cmd == "labels":
        from .console.labels import write_labels

        if args.out is None:
            write_labels(sys.stdout)
        else:
            with args.out.open("w", newline="") as f:
                n = write_labels(f)
            print(f"{n} reviewed alerts -> {args.out}")


if __name__ == "__main__":
    main()
