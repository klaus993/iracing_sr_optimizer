"""Fetch past race results from iRacing API and aggregate empirical SR data."""

from __future__ import annotations

import json
import logging
import statistics
import time
from typing import Optional

from .config import RESULTS_CACHE_DIR
from .iracing_api import _to_dict, refresh_client, IRacingAPIError
from .models import Series, SeriesEmpirical

logger = logging.getLogger(__name__)

API_DELAY = 0.5  # seconds between API calls
PROGRESS_EVERY = 10


def _cache_path(season_id: int, week_num: int):
    return RESULTS_CACHE_DIR / f"{season_id}_week{week_num}.json"


def _load_cached(season_id: int, week_num: int) -> Optional[dict]:
    path = _cache_path(season_id, week_num)
    if path.exists():
        try:
            with open(path) as f:
                return json.load(f)
        except (json.JSONDecodeError, KeyError):
            pass
    return None


def _save_cache(season_id: int, week_num: int, data: dict):
    RESULTS_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with open(_cache_path(season_id, week_num), "w") as f:
        json.dump(data, f, indent=2)


def aggregate_driver_results(drivers: list[dict]) -> Optional[SeriesEmpirical]:
    """Aggregate per-driver results into empirical metrics.

    Each driver dict should have: incidents, laps_complete,
    old_sub_level, new_sub_level, old_cpi, new_cpi.
    """
    if not drivers:
        return None

    incidents = [d["incidents"] for d in drivers]
    sr_deltas = [d["new_sub_level"] - d["old_sub_level"] for d in drivers]

    # CPI: corners / incidents — handle zero-incident drivers gracefully
    cpis = []
    for d in drivers:
        if d["incidents"] > 0 and d.get("corners_completed", 0) > 0:
            cpis.append(d["corners_completed"] / d["incidents"])

    return SeriesEmpirical(
        series_name="",
        avg_incidents=statistics.mean(incidents),
        median_incidents=statistics.median(incidents),
        avg_sr_delta=statistics.mean(sr_deltas),
        avg_cpi=statistics.mean(cpis) if cpis else 0.0,
        sample_size=len(drivers),
        subsessions_fetched=0,
    )


def fetch_results_for_series(
    series: Series,
    week_num: int,
    client,
    max_subsessions: Optional[int] = None,
) -> Optional[SeriesEmpirical]:
    """Fetch all race results for a series in a given week.

    Args:
        series: Series with season_id set.
        week_num: 1-indexed week number.
        client: Authenticated iRacing API client.

    Returns:
        SeriesEmpirical with aggregated data, or None if no results.
    """
    if not series.season_id:
        return None

    # Check cache first
    cached = _load_cached(series.season_id, week_num)
    if cached:
        logger.debug(f"Using cached results for {series.name} week {week_num}")
        return SeriesEmpirical(**cached)

    # Fetch subsession list for this week (race_week_num is 0-indexed in API)
    api_week = week_num - 1
    try:
        season_results = client.result_season_results(
            series.season_id, event_type=5, race_week_num=api_week
        )
    except Exception as e:
        logger.warning(f"Failed to fetch season results for {series.name}: {e}")
        return None

    season_data = _to_dict(season_results) if not isinstance(season_results, dict) else season_results
    results_list = season_data.get("results_list", [])

    if not results_list:
        logger.debug(f"No results for {series.name} week {week_num}")
        return None

    all_drivers = []
    subsession_count = 0

    subsessions_seen = 0
    for result_entry in results_list:
        subsession_id = result_entry.get("subsession_id")
        if not subsession_id:
            continue

        if max_subsessions is not None and subsessions_seen >= max_subsessions:
            logger.info(
                f"Reached max_subsessions={max_subsessions} for {series.name}"
            )
            break

        time.sleep(API_DELAY)

        try:
            subsession_data = client.result(subsession_id)
        except Exception as e:
            if _should_refresh_for_error(e):
                try:
                    client = refresh_client()
                    subsession_data = client.result(subsession_id)
                except (Exception, IRacingAPIError) as retry_err:
                    logger.warning(
                        f"Failed to fetch subsession {subsession_id} after token refresh: {retry_err}"
                    )
                    continue
            else:
                logger.warning(f"Failed to fetch subsession {subsession_id}: {e}")
                continue

        sub_dict = _to_dict(subsession_data) if not isinstance(subsession_data, dict) else subsession_data
        corners_per_lap = sub_dict.get("corners_per_lap", 0)
        subsession_count += 1
        subsessions_seen += 1

        if subsessions_seen % PROGRESS_EVERY == 0:
            print(f" {subsessions_seen}...", end="", flush=True)

        for session_result in sub_dict.get("session_results", []):
            # Only look at race sessions (simsession_type=6 is race)
            if session_result.get("simsession_type") != 6:
                continue

            for driver in session_result.get("results", []):
                laps = driver.get("laps_complete", 0)
                if laps <= 0:
                    continue

                incidents = driver.get("incidents", 0)
                old_sl = driver.get("old_sub_level", 0)
                new_sl = driver.get("new_sub_level", 0)

                all_drivers.append({
                    "incidents": incidents,
                    "laps_complete": laps,
                    "old_sub_level": old_sl,
                    "new_sub_level": new_sl,
                    "old_cpi": driver.get("old_cpi", 0.0),
                    "new_cpi": driver.get("new_cpi", 0.0),
                    "corners_completed": corners_per_lap * laps,
                })

    if not all_drivers:
        return None

    empirical = aggregate_driver_results(all_drivers)
    if empirical is None:
        return None

    empirical.series_name = series.name
    empirical.subsessions_fetched = subsession_count

    # Cache
    _save_cache(series.season_id, week_num, {
        "series_name": empirical.series_name,
        "avg_incidents": empirical.avg_incidents,
        "median_incidents": empirical.median_incidents,
        "avg_sr_delta": empirical.avg_sr_delta,
        "avg_cpi": empirical.avg_cpi,
        "sample_size": empirical.sample_size,
        "subsessions_fetched": empirical.subsessions_fetched,
    })

    return empirical


def _should_refresh_for_error(err: Exception) -> bool:
    """Return True if the error likely indicates an expired/invalid token."""
    if isinstance(err, json.JSONDecodeError):
        return True
    msg = str(err).lower()
    return "access token not valid" in msg


def fetch_results(
    week_num: int,
    series_list: list[Series],
    client,
    max_subsessions: Optional[int] = None,
) -> dict[str, SeriesEmpirical]:
    """Fetch empirical race data for all series in the given week.

    Returns a dict mapping series_name -> SeriesEmpirical.
    """
    results = {}
    total = len(series_list)

    for i, series in enumerate(series_list, 1):
        if not series.season_id:
            continue

        logger.info(f"[{i}/{total}] Fetching results for {series.name}...")
        print(f"  [{i}/{total}] {series.name}...", end="", flush=True)

        empirical = fetch_results_for_series(
            series,
            week_num,
            client,
            max_subsessions=max_subsessions,
        )
        if empirical:
            results[series.name] = empirical
            print(f" {empirical.sample_size} drivers, {empirical.subsessions_fetched} subsessions")
        else:
            print(" no data")

    print(f"\nFetched empirical data for {len(results)}/{total} series")
    return results
