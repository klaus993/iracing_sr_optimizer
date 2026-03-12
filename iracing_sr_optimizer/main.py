"""CLI entry point for iRacing SR Optimizer."""

from __future__ import annotations

import argparse
import logging
import sys

from .config import CATEGORIES, SCHEDULE_JSON
from .fetch_schedule import fetch_and_save_schedule
from .iracing_api import fetch_tracks
from .models import load_schedule
from .output_formatter import format_csv, format_json, format_table
from .sr_calculator import rank_series
from .track_data import load_api_track_cache


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="iracing_sr_optimizer",
        description="Rank iRacing series by Safety Rating gaining potential",
    )
    parser.add_argument(
        "--fetch",
        action="store_true",
        help="Fetch schedule data from iRacing API before analyzing",
    )
    parser.add_argument(
        "--week",
        type=int,
        choices=range(1, 14),
        metavar="N",
        help="Week number (1-13)",
    )
    parser.add_argument(
        "--all-weeks",
        action="store_true",
        help="Show all 13 weeks",
    )
    parser.add_argument(
        "--category",
        type=str,
        choices=["OVAL", "SPORTS_CAR", "FORMULA_CAR", "DIRT_OVAL", "DIRT_ROAD"],
        help="Filter to one category",
    )
    parser.add_argument(
        "--license",
        type=str,
        choices=["R", "D", "C", "B", "A"],
        help="Only show series accessible at this license class or below",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="output_json",
        help="Output as JSON",
    )
    parser.add_argument(
        "--csv",
        action="store_true",
        dest="output_csv",
        help="Output as CSV",
    )
    parser.add_argument(
        "--no-api",
        action="store_true",
        help="Skip iRacing API, use hardcoded track data only",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable verbose logging",
    )

    args = parser.parse_args(argv)

    if not args.fetch and not args.week and not args.all_weeks:
        parser.error("Either --week N, --all-weeks, or --fetch is required")

    return args


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s: %(message)s",
    )

    # Fetch schedule from API if requested
    if args.fetch:
        count = fetch_and_save_schedule()
        if count == 0:
            sys.exit(1)
        # If no week specified, just fetch and exit
        if not args.week and not args.all_weeks:
            return

    # Load schedule
    if not SCHEDULE_JSON.exists():
        print(f"Error: Schedule data not found at {SCHEDULE_JSON}", file=sys.stderr)
        print("Run with --fetch first to download from iRacing API.", file=sys.stderr)
        sys.exit(1)

    all_series = load_schedule(SCHEDULE_JSON)
    logging.info(f"Loaded {len(all_series)} series from schedule")

    # Get track corner data
    api_tracks = None
    if not args.no_api:
        api_tracks = fetch_tracks(use_cache=True)
        if api_tracks:
            logging.info(f"Loaded {len(api_tracks)} tracks from API/cache")
        else:
            api_tracks = load_api_track_cache()

    # Determine weeks to process
    weeks = list(range(1, 14)) if args.all_weeks else [args.week]

    for week_num in weeks:
        results = rank_series(
            all_series,
            week_num,
            api_tracks=api_tracks,
            category_filter=args.category,
            license_filter=args.license,
        )

        if args.output_json:
            print(format_json(results))
        elif args.output_csv:
            print(format_csv(results))
        else:
            print(format_table(results, week_num, args.category))

        if args.all_weeks and week_num < 13:
            print("=" * 105)
            print()


if __name__ == "__main__":
    main()
