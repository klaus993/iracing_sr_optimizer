"""CLI entry point for iRacing SR Optimizer."""

from __future__ import annotations

import argparse
import logging
import sys

from .config import CATEGORIES, IRACING_CUST_ID, RESULTS_CACHE_DIR, SCHEDULE_JSON
from .fetch_schedule import fetch_and_save_schedule
from .iracing_api import fetch_tracks
from .models import load_schedule
from .output_formatter import format_csv, format_json, format_table
from .sr_calculator import rank_series
from .sr_predictor import parse_sr_string
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
        choices=range(1, 13),
        metavar="N",
        help="Week number (1-12)",
    )
    parser.add_argument(
        "--all-weeks",
        action="store_true",
        help="Show all 12 weeks",
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
        "--fetch-results",
        action="store_true",
        help="Fetch empirical race results for the target week",
    )
    parser.add_argument(
        "--cust-id",
        type=int,
        default=None,
        help="iRacing customer ID for personal SR prediction (also reads IRACING_CUST_ID env var)",
    )
    parser.add_argument(
        "--my-sr",
        type=str,
        default=None,
        help="Manual SR override, e.g. 'C3.45' (bypasses API SR fetch)",
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

    # Parse --my-sr early so we fail fast on invalid input
    user_sr = None
    if args.my_sr:
        try:
            user_sr = parse_sr_string(args.my_sr)
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)

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
    weeks = list(range(1, 13)) if args.all_weeks else [args.week]

    # Fetch empirical results if requested
    empirical_data = None
    if args.fetch_results:
        from .fetch_results import fetch_results
        from .iracing_api import IRacingAPIError, get_client

        try:
            client = get_client()
        except IRacingAPIError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)

        target_week = weeks[0] if len(weeks) == 1 else (args.week or 1)
        print(f"Fetching empirical results for week {target_week}...")
        empirical_data = fetch_results(target_week, all_series, client)

    # Load cached empirical data if not fetching but cache exists
    if empirical_data is None:
        empirical_data = _load_cached_empirical(all_series, weeks[0] if len(weeks) == 1 else 1)

    # Fetch user SR from API if --cust-id provided (and --my-sr not set)
    if user_sr is None:
        cust_id = args.cust_id or (int(IRACING_CUST_ID) if IRACING_CUST_ID else None)
        if cust_id and not args.no_api:
            from .iracing_api import IRacingAPIError, get_client
            from .sr_predictor import get_user_sr

            try:
                client = get_client()
                user_sr = get_user_sr(client, cust_id, args.category or "")
                if user_sr:
                    print(f"Your SR: {user_sr.license_class}{user_sr.sr_display:.2f} (CPI: {user_sr.cpi:.1f})")
            except IRacingAPIError as e:
                print(f"Warning: Could not fetch user SR: {e}", file=sys.stderr)

    for week_num in weeks:
        results = rank_series(
            all_series,
            week_num,
            api_tracks=api_tracks,
            category_filter=args.category,
            license_filter=args.license,
            empirical_data=empirical_data,
            user_sr=user_sr,
        )

        if args.output_json:
            print(format_json(results))
        elif args.output_csv:
            print(format_csv(results))
        else:
            print(format_table(results, week_num, args.category))

        if args.all_weeks and week_num < 12:
            print("=" * 105)
            print()


def _load_cached_empirical(all_series, week_num):
    """Try to load cached empirical data for all series."""
    import json

    from .models import SeriesEmpirical

    data = {}
    for series in all_series:
        if not series.season_id:
            continue
        cache_file = RESULTS_CACHE_DIR / f"{series.season_id}_week{week_num}.json"
        if cache_file.exists():
            try:
                with open(cache_file) as f:
                    cached = json.load(f)
                data[series.name] = SeriesEmpirical(**cached)
            except (json.JSONDecodeError, KeyError, TypeError):
                continue

    return data if data else None


if __name__ == "__main__":
    main()
