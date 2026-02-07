"""Fetch iRacing schedule data from the API and write schedule_data.json."""

from __future__ import annotations

import json
import logging
import sys
from collections import Counter

from .config import CACHE_DIR, DATA_DIR, SCHEDULE_JSON
from .iracing_api import IRacingAPIError, get_client, _to_dict

logger = logging.getLogger(__name__)

# License group ID -> license class letter
LICENSE_GROUP_MAP = {
    1: "R",
    2: "D",
    3: "C",
    4: "B",
    5: "A",
}

# API category string -> our category name
CATEGORY_MAP = {
    "oval": "OVAL",
    "dirt_oval": "DIRT_OVAL",
    "dirt_road": "DIRT_ROAD",
}


def _val(obj, key, default=None):
    """Get a value from a dict or pydantic model."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _map_category(category_str: str, car_types: list) -> str:
    """Map API category to our internal category name.

    For 'road' category, check car_types to distinguish formula vs sports car.
    """
    cat_lower = category_str.lower().replace(" ", "_")

    if cat_lower in CATEGORY_MAP:
        return CATEGORY_MAP[cat_lower]

    if cat_lower == "road":
        for ct in car_types:
            car_type = _val(ct, "car_type", "").lower()
            if "formula" in car_type:
                return "FORMULA_CAR"
        return "SPORTS_CAR"

    return "SPORTS_CAR"


def _map_license_class(license_group: int) -> str:
    """Map API license_group int to license class letter."""
    return LICENSE_GROUP_MAP.get(license_group, "R")


def _map_license_range(license_group_types: list) -> str:
    """Build a license range string from license group types."""
    if not license_group_types:
        return ""
    names = []
    for lgt in license_group_types:
        name = _val(lgt, "license_group_type", "")
        if name:
            names.append(name)
    return " --> ".join(names) if names else ""


def _calc_races_per_hour(race_time_descriptors: list) -> float:
    """Calculate races per hour from race time descriptors."""
    if not race_time_descriptors:
        return 1.0

    for rtd in race_time_descriptors:
        repeating = _val(rtd, "repeating", False)
        repeat_minutes = _val(rtd, "repeat_minutes")

        if repeating and repeat_minutes and repeat_minutes > 0:
            return 60.0 / repeat_minutes

        # Non-repeating: count session times per day
        session_times = _val(rtd, "session_times") or []
        if session_times:
            return max(len(session_times) / 24.0, 0.1)

    return 1.0


def _format_race_frequency(race_time_descriptors: list) -> str:
    """Build a human-readable race frequency string."""
    if not race_time_descriptors:
        return "Unknown"

    for rtd in race_time_descriptors:
        repeating = _val(rtd, "repeating", False)
        repeat_minutes = _val(rtd, "repeat_minutes")

        if repeating and repeat_minutes:
            if repeat_minutes == 60:
                return "Races every hour"
            elif repeat_minutes < 60:
                offsets = [i * repeat_minutes for i in range(60 // repeat_minutes)]
                times = " and ".join(f":{m:02d}" for m in offsets)
                return f"Races at {times}"
            else:
                return f"Races every {repeat_minutes} minutes"

    return "Scheduled times"


def _format_track_name(track) -> str:
    """Build full track name from API track data."""
    name = _val(track, "track_name", "Unknown")
    config = _val(track, "config_name", "") or ""
    return f"{name} - {config}" if config else name


def fetch_and_save_schedule() -> int:
    """Fetch all schedule data from iRacing API and save to schedule_data.json.

    Returns the number of series saved.
    """
    try:
        client = get_client()
    except IRacingAPIError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 0

    # 1. Fetch series metadata (for min/max starters)
    logger.info("Fetching series metadata...")
    raw_series = client.get_series()
    series_map = {}
    for s in raw_series:
        sid = _val(s, "series_id")
        series_map[sid] = _to_dict(s)
    logger.info(f"Got {len(series_map)} series")

    # 2. Fetch track data (for corner counts — also caches for later use)
    logger.info("Fetching track data...")
    raw_tracks = client.get_tracks()
    track_by_id = {}
    tracks_for_cache = []
    for t in raw_tracks:
        td = _to_dict(t)
        tracks_for_cache.append(td)
        track_by_id[td.get("track_id")] = td
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with open(CACHE_DIR / "tracks.json", "w") as f:
        json.dump(tracks_for_cache, f, indent=2)
    logger.info(f"Got and cached {len(track_by_id)} tracks")

    # 3. Fetch current seasons with schedules
    logger.info("Fetching current seasons...")
    raw_seasons = client.series_seasons()
    logger.info(f"Got {len(raw_seasons)} total seasons")

    all_series = []
    for season in raw_seasons:
        sd = _to_dict(season)

        if not sd.get("active", False):
            continue

        series_id = sd.get("series_id")
        si = series_map.get(series_id, {})

        # Category: get from track_types on the season
        track_types = sd.get("track_types", [])
        category_str = ""
        if track_types:
            category_str = _val(track_types[0], "track_type", "")
        if not category_str:
            category_str = si.get("category", "road")

        car_types = sd.get("car_types", [])
        category = _map_category(category_str, car_types)

        # License
        license_group = sd.get("license_group", 1)
        license_class = _map_license_class(license_group)
        license_range = _map_license_range(sd.get("license_group_types", []))

        # Heat racing
        is_heat = sd.get("is_heat_racing", False)
        heat_info = sd.get("heat_ses_info")

        # Team racing (driver_changes = pit crew swaps during race)
        is_team = sd.get("driver_changes", False)

        # Incident limits
        incident_limit = sd.get("incident_limit", 0)
        incident_dq = incident_limit if incident_limit > 0 else None

        incident_warn_mode = sd.get("incident_warn_mode", 0)
        incident_warn_param1 = sd.get("incident_warn_param1", 0)
        incident_penalty = incident_warn_param1 if incident_warn_mode > 0 else None

        drops = sd.get("drops", 0)
        min_entries = si.get("min_starters", 2)
        split_at = si.get("max_starters", 20)

        schedules = sd.get("schedules", [])

        # Aggregate cars across ALL weeks (not just week 1)
        cars = []
        for sched in schedules:
            for rwc in sched.get("race_week_cars", []):
                car_name = _val(rwc, "car_name", "")
                if car_name and car_name not in cars:
                    cars.append(car_name)

        # Race frequency: check all weeks, use the most common value
        race_freq = "Unknown"
        races_per_hour = 1.0
        if schedules:
            freq_counts: dict[float, int] = {}
            for sched in schedules:
                rtds = sched.get("race_time_descriptors", [])
                rph = _calc_races_per_hour(rtds)
                freq_counts[rph] = freq_counts.get(rph, 0) + 1
            # Use the most common races_per_hour across all weeks
            races_per_hour = max(freq_counts, key=freq_counts.get)
            # Format from first week (descriptors are usually identical)
            rtds = schedules[0].get("race_time_descriptors", [])
            race_freq = _format_race_frequency(rtds)

        # Build weekly schedule
        weeks = []
        for sched in schedules:
            week_num = sched.get("race_week_num", 0) + 1  # API is 0-indexed
            start_date = sched.get("start_date", "")
            if "T" in start_date:
                start_date = start_date.split("T")[0]

            track = sched.get("track", {})
            track_name = _format_track_name(track)
            tid = _val(track, "track_id")

            race_laps = sched.get("race_lap_limit")
            race_minutes = sched.get("race_time_limit")

            # Heat racing: get laps from season-level heat_ses_info
            heat_laps_val = None
            consolation_laps_val = None
            feature_laps_val = None

            if is_heat and heat_info:
                heat_laps_val = heat_info.get("heat_laps") or None
                feature_laps_val = heat_info.get("main_laps") or None
                consolation_laps_val = heat_info.get("consolation_first_session_laps") or None
                # For heat racing, the race_lap_limit is typically the feature;
                # we track heat+feature separately
                if heat_laps_val and feature_laps_val:
                    race_laps = None

            weeks.append({
                "week": week_num,
                "start_date": start_date,
                "track": track_name,
                "track_id": tid,
                "race_laps": race_laps if race_laps and race_laps > 0 else None,
                "race_minutes": race_minutes if race_minutes and race_minutes > 0 else None,
                "heat_laps": heat_laps_val,
                "consolation_laps": consolation_laps_val,
                "feature_laps": feature_laps_val,
            })

        if not weeks:
            continue

        all_series.append({
            "name": sd.get("season_name", "Unknown"),
            "category": category,
            "license_class": license_class,
            "license_range": license_range,
            "cars": cars,
            "race_frequency": race_freq,
            "races_per_hour": races_per_hour,
            "min_entries": min_entries,
            "split_at": split_at,
            "drops": drops,
            "is_heat_racing": is_heat,
            "is_team_racing": is_team,
            "incident_dq": incident_dq,
            "incident_penalty_threshold": incident_penalty,
            "weeks": weeks,
        })

    logger.info(f"Built {len(all_series)} active series")

    # Write output
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    output = {"series": all_series}
    with open(SCHEDULE_JSON, "w") as f:
        json.dump(output, f, indent=2)

    print(f"Saved {len(all_series)} series to {SCHEDULE_JSON}")
    cats = Counter(s["category"] for s in all_series)
    for cat, count in sorted(cats.items()):
        print(f"  {cat}: {count} series")

    return len(all_series)
