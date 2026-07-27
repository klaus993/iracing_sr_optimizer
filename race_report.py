#!/usr/bin/env python3
"""Standalone tool: race strategy report for a single iRacing subsession.

Where `car_meta.py` answers "which car is good this week", this answers "what did the
field actually *do* in one race" — stint structure, pit stops, inferred tyre calls,
fuel bounds, the weather timeline, penalties, and how positions moved through it all.

Built for wet/mixed races, where the result is usually decided by *when* people stopped
rather than raw pace. On a dry race the weather and tyre sections degrade to "no
transition detected" and the stint/pit/fuel analysis still stands.

What the iRacing Data API does and does not give us:
  * Pit stops           `lap_events` contains 'pitted'. Both the in-lap and the out-lap
                        carry the flag, so consecutive flagged laps are ONE stop.
  * Max fuel fill %     Real, per car, per week: the season schedule's
                        `car_restrictions[].max_pct_fuel_fill`. IMSA caps GT3 at 50%.
  * Tank capacity (L)   NOT in the API. Hardcoded below from the iRacing wiki, per car.
  * Litres per stop     NOT derivable. Pit time is a composite of pit-lane transit,
                        service (fuel and tyres run concurrently), penalty service,
                        repairs and box waiting, so it cannot be decomposed into fuel.
                        We classify stops against the field's own modal stop instead.
  * Tyre compound       NOT exposed. Inferred from post-stop pace vs the class median,
                        and always reported as an inference with a confidence.

Fuel consumption is reported as a rigorous UPPER BOUND, not an estimate: a driver cannot
have started the race with more than `capacity x max_pct_fuel_fill`, so over an opening
stint of N laps, consumption <= usable_fuel / N. Underfilling only makes the true figure
lower, so the inequality holds regardless. Later stints have unknown fills and get no
bound. Pooling drivers of the same car model tightens it — the longest opening stint
anyone managed gives the closest bound to true consumption.

This script reuses car_usage.py for auth/retry/caching and car_meta.py for lap-time
formatting, so it needs the same OAuth credentials in the environment
(IRACING_EMAIL, IRACING_PASSWORD, IRACING_CLIENT_ID, IRACING_CLIENT_SECRET).

Usage:
    python race_report.py --subsession 87466883 --class IMSA23 --cust-id 837530
    python race_report.py --subsession 87466883 --json > race.json
    python race_report.py --subsession 87466883 --class IMSA23 --csv > race.csv

Run `python race_report.py --help` for all options.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import re
import statistics
import sys
import urllib.request
from typing import Optional

import car_meta as cm
import car_usage as cu

logger = logging.getLogger("race_report")


# --- Reference data -----------------------------------------------------------

# Base fuel tank capacity in litres, by car_id. NOT available from the Data API (the
# car payload carries weight/hp/BoP only), so these come from the iRacing Fandom wiki
# spec boxes, read via its MediaWiki api.php endpoint. The series' max_pct_fuel_fill
# (from the API) is applied on top of these to get usable fuel.
FUEL_CAPACITY_L: dict[int, float] = {
    132: 100.00,   # BMW M4 GT3 EVO
    156: 105.99,   # Mercedes-AMG GT3 2020
    173: 104.10,   # Ferrari 296 GT3
    185: 110.15,   # Ford Mustang GT3
    188: 110.15,   # McLaren 720S GT3 EVO
    206: 106.00,   # Aston Martin Vantage GT3 EVO
}

# A lap is "wet-phase" once the class median is this much slower than the dry baseline.
# 8% at Road America is ~10s, far outside normal lap-to-lap scatter (which sits ~1-2%).
WET_ONSET_PCT = 8.0

# Laps after a stop used to judge what tyres went on.
TYRE_JUDGE_LAPS = 2

# Rebuild the API client every N calls. iRacing OAuth tokens last ~10 minutes and a
# long fetch that reuses one client starts failing with "Access token not valid"
# partway through, silently losing data.
REFRESH_EVERY = 150

PIT_EVENT = "pitted"


# --- API access with token refresh --------------------------------------------

def _is_auth_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return ("access token not valid" in text or "unauthorized" in text
            or "401" in text or "invalid_token" in text)


class RefreshingClient:
    """irDataClient wrapper that rebuilds itself when the OAuth token expires.

    car_usage._api_call already retries rate limits, but an expired token is not a
    rate limit — it fails every subsequent call. This rebuilds the client (dropping the
    cached token so a fresh one is minted) on auth failure and every REFRESH_EVERY calls.
    """

    def __init__(self, refresh_every: int = REFRESH_EVERY):
        self._client = None
        self._calls = 0
        self._refresh_every = refresh_every

    def _rebuild(self):
        try:
            cu.TOKEN_CACHE_FILE.unlink()
        except (FileNotFoundError, OSError):
            pass
        logger.info("Building a fresh API client (new OAuth token)")
        self._client = cu.get_client()
        self._calls = 0
        return self._client

    @property
    def client(self):
        if self._client is None:
            self._rebuild()
        return self._client

    def call(self, label: str, method: str, **kwargs):
        if self._calls >= self._refresh_every:
            self._rebuild()
        self._calls += 1
        try:
            return cu._api_call(label, getattr(self.client, method), **kwargs)
        except cu.IRacingAPIError as exc:
            if not _is_auth_error(exc):
                raise
            logger.info("Auth failure on %s; refreshing token and retrying", label)
            self._rebuild()
            return cu._api_call(label, getattr(self.client, method), **kwargs)


def fetch_race_data(pool: RefreshingClient, subsession_id: int) -> dict:
    """Fetch everything one race report needs. Returns a dict of raw payloads."""
    result = cu.fetch_result(pool.client, subsession_id)
    chart = [cu._to_dict(x) for x in pool.call(
        f"lap_chart({subsession_id})", "result_lap_chart_data",
        subsession_id=subsession_id, simsession_number=0) or []]
    try:
        events = [cu._to_dict(x) for x in pool.call(
            f"event_log({subsession_id})", "result_event_log",
            subsession_id=subsession_id, simsession_number=0) or []]
    except cu.IRacingAPIError as e:
        logger.warning("Event log unavailable: %s", e)
        events = []

    week = _find_season_week(pool, result)
    forecast = _fetch_forecast(week)
    return {"result": result, "chart": chart, "events": events,
            "week": week, "forecast": forecast}


def _find_season_week(pool: RefreshingClient, result: dict) -> dict:
    """Locate the season schedule entry for this subsession's season + race week.

    Carries `car_restrictions` (the real max_pct_fuel_fill and BoP) and the signed
    `weather.weather_url` forecast link. Returns {} if the season is no longer listed
    (series_seasons only covers active seasons).
    """
    season_id = result.get("season_id")
    week_num = result.get("race_week_num")
    if season_id is None or week_num is None:
        return {}
    try:
        seasons = [cu._to_dict(x) for x in pool.call(
            "series_seasons", "series_seasons", include_series=True) or []]
    except cu.IRacingAPIError as e:
        logger.warning("Could not fetch season schedule: %s", e)
        return {}
    for s in seasons:
        if s.get("season_id") != season_id:
            continue
        for w in s.get("schedules") or []:
            w = cu._to_dict(w)
            if w.get("race_week_num") == week_num:
                return w
    logger.info("Season %s week %s not in the active schedule list", season_id, week_num)
    return {}


def _fetch_forecast(week: dict) -> list[dict]:
    """Fetch the signed weather-forecast JSON for a schedule week (may be empty)."""
    url = ((week.get("weather") or {}).get("weather_url")) if week else None
    if not url:
        return []
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            rows = json.loads(r.read())
    except Exception as e:  # noqa: BLE001 - forecast is optional context
        logger.warning("Could not fetch weather forecast: %s", e)
        return []
    return rows if isinstance(rows, list) else []


# --- Entry metadata (pure) ----------------------------------------------------

def race_simsession(result: dict) -> dict:
    """The race simsession (number 0), or {} if absent."""
    for ss in result.get("session_results") or []:
        if ss.get("simsession_number") == 0:
            return ss
    return {}


def build_entries(result: dict, car_class: Optional[str] = None) -> dict[int, dict]:
    """cust_id -> entry metadata for the race, optionally filtered to one car class."""
    want = car_class.strip().lower() if car_class else None
    out: dict[int, dict] = {}
    for row in race_simsession(result).get("results") or []:
        row = cu._to_dict(row)
        cls = str(row.get("car_class_short_name") or "")
        if want and cls.lower() != want:
            continue
        cid = row.get("cust_id")
        if cid is None:
            continue
        out[cid] = {
            "cust_id": cid,
            "name": row.get("display_name"),
            "car_number": row.get("car_number"),
            "car_id": row.get("car_id"),
            "car_name": row.get("car_name"),
            "car_class": cls,
            # iRacing positions are 0-indexed; +1 for display.
            "start_pic": (row.get("starting_position_in_class") or 0) + 1,
            "finish_pic": (row.get("finish_position_in_class") or 0) + 1,
            "finish_overall": (row.get("finish_position") or 0) + 1,
            "laps_complete": row.get("laps_complete") or 0,
            "incidents": row.get("incidents") or 0,
            "old_irating": row.get("oldi_rating"),
            "new_irating": row.get("newi_rating"),
            "best_lap": row.get("best_lap_time"),
        }
    return out


def normalize_chart(chart: list[dict], keep: Optional[set[int]] = None) -> dict[int, list[dict]]:
    """cust_id -> laps sorted by lap_number, restricted to `keep` cust_ids if given."""
    by: dict[int, list[dict]] = {}
    for row in chart:
        cid = row.get("cust_id")
        if cid is None or (keep is not None and cid not in keep):
            continue
        by.setdefault(cid, []).append(row)
    for cid in by:
        by[cid].sort(key=lambda r: r.get("lap_number") or 0)
    return by


# --- Stints and pit stops (pure) ----------------------------------------------

def pitted_laps(laps: list[dict]) -> list[int]:
    """Lap numbers flagged 'pitted', ascending.

    Lap 0 is excluded: a flag there means the car started from the pit lane or went
    through it on the way to the grid, which is not a strategy stop and would otherwise
    inflate the stop count and make the field's "first stop" read as lap 0.
    """
    out = []
    for lap in laps:
        events = [str(e).lower() for e in (lap.get("lap_events") or [])]
        if PIT_EVENT in events:
            n = lap.get("lap_number")
            if n is not None and n >= 1:
                out.append(n)
    return sorted(set(out))


def collapse_stops(flagged: list[int]) -> list[list[int]]:
    """Group consecutive flagged lap numbers into one stop each.

    iRacing flags both the in-lap and the out-lap of a stop, so laps [13,14,16,17]
    is two stops, not four. A lone flagged lap is still a stop (it happens when a
    driver pits on the final lap, or the out-lap carries no flag).
    """
    groups: list[list[int]] = []
    for n in flagged:
        if groups and n == groups[-1][-1] + 1:
            groups[-1].append(n)
        else:
            groups.append([n])
    return groups


def build_stints(laps: list[dict], stops: list[list[int]]) -> list[dict]:
    """Split a driver's race into stints separated by pit stops.

    The boundary is the *in-lap* (the first flagged lap of a stop): it is run on the old
    fuel load, and everything from the next lap on is run on the new one. Using only the
    in-lap means the stints tile the race exactly — every lap belongs to one stint and
    the lap counts sum to the driver's last lap, which the reconciliation check relies on.
    """
    numbers = [l.get("lap_number") for l in laps if l.get("lap_number") is not None]
    racing = [n for n in numbers if n and n > 0]
    if not racing:
        return []
    last = max(racing)
    stints = []
    start = 1
    for group in stops:
        in_lap = group[0]
        if start <= in_lap <= last:
            stints.append({"first_lap": start, "last_lap": in_lap,
                           "laps": in_lap - start + 1, "ended_with_stop": True})
            start = in_lap + 1
    if start <= last:
        stints.append({"first_lap": start, "last_lap": last,
                       "laps": last - start + 1, "ended_with_stop": False})
    return stints


def lap_seconds_map(laps: list[dict]) -> dict[int, float]:
    """lap_number -> lap time in seconds, skipping missing/invalid laps."""
    out = {}
    for lap in laps:
        s = cm._lap_seconds(lap.get("lap_time"))
        n = lap.get("lap_number")
        if s is not None and n is not None:
            out[n] = s
    return out


def clean_lap_baseline(laps: list[dict], lap_range: Optional[tuple[int, int]] = None
                       ) -> Optional[float]:
    """Median lap time (s) over laps with no pit/off/contact event and no incident.

    Used as the "what a normal lap cost" reference when measuring pit loss.
    """
    vals = []
    for lap in laps:
        n = lap.get("lap_number")
        if not n or n < 1:
            continue
        if lap_range and not (lap_range[0] <= n <= lap_range[1]):
            continue
        if lap.get("lap_events") or lap.get("incident"):
            continue
        s = cm._lap_seconds(lap.get("lap_time"))
        if s is not None:
            vals.append(s)
    return round(statistics.median(vals), 3) if vals else None


def stop_time_loss(laps: list[dict], group: list[int],
                   baseline: Optional[float]) -> Optional[float]:
    """Seconds lost across one stop's flagged laps, vs the driver's clean-lap baseline.

    This is total pit loss (transit + service + any penalty served + box time), NOT
    service time alone, and NOT decomposable into fuel vs tyres.
    """
    if baseline is None:
        return None
    times = lap_seconds_map(laps)
    total = 0.0
    seen = False
    for n in group:
        if n in times:
            total += times[n] - baseline
            seen = True
    return round(total, 2) if seen else None


# --- Weather and the wet transition (pure) ------------------------------------

def class_median_by_lap(by_driver: dict[int, list[dict]]) -> dict[int, float]:
    """lap_number -> median green-flag lap time (s) across the given drivers.

    Pit in/out laps are excluded. This matters: the whole field tends to stop within
    the same two or three laps, so leaving them in spikes the median exactly there and
    a dry race's pit window gets misread as rain arriving.
    """
    buckets: dict[int, list[float]] = {}
    for laps in by_driver.values():
        for lap in laps:
            n = lap.get("lap_number")
            if not n or n < 2:  # lap 1 includes the standing/rolling start
                continue
            events = [str(e).lower() for e in (lap.get("lap_events") or [])]
            if PIT_EVENT in events:
                continue
            s = cm._lap_seconds(lap.get("lap_time"))
            if s is not None:
                buckets.setdefault(n, []).append(s)
    return {n: round(statistics.median(v), 3) for n, v in sorted(buckets.items())}


def detect_wet_phase(median_by_lap: dict[int, float],
                     onset_pct: float = WET_ONSET_PCT) -> dict:
    """Find the dry->wet break from the class median pace curve.

    Returns dry_baseline, onset_lap (first lap `onset_pct` slower than baseline),
    peak_lap/peak (slowest), and wet_median. onset_lap is None on a dry race.
    """
    laps = sorted(median_by_lap)
    if len(laps) < 4:
        return {"onset_lap": None, "dry_baseline": None, "peak_lap": None,
                "peak": None, "wet_median": None, "onset_pct": onset_pct}
    # Baseline from the first third of the race, which is normally representative.
    head = laps[: max(3, len(laps) // 3)]
    dry = statistics.median([median_by_lap[n] for n in head])
    threshold = dry * (1 + onset_pct / 100.0)
    # Require the slowdown to persist into the next lap. Rain does not un-fall, whereas
    # a single slow lap is usually a spin, a caution-free incident or thin lap sample.
    onset = None
    for i, n in enumerate(laps):
        if median_by_lap[n] <= threshold:
            continue
        nxt = laps[i + 1] if i + 1 < len(laps) else None
        if nxt is None or median_by_lap[nxt] > threshold:
            onset = n
            break
    peak_lap = max(laps, key=lambda n: median_by_lap[n])
    wet = None
    if onset is not None:
        wet_vals = [median_by_lap[n] for n in laps if n >= onset]
        wet = round(statistics.median(wet_vals), 3)
    return {"onset_lap": onset, "dry_baseline": round(dry, 3), "peak_lap": peak_lap,
            "peak": median_by_lap[peak_lap], "wet_median": wet, "onset_pct": onset_pct}


def session_weather(result: dict) -> dict:
    """Session weather settings, with precip share and sim start time surfaced."""
    w = result.get("weather") or {}
    return {
        "precip_time_pct": w.get("precip_time_pct"),
        "precip_option": w.get("precip_option"),
        "simulated_start_time": w.get("simulated_start_time"),
        "temp_value": w.get("temp_value"),
        "temp_units": w.get("temp_units"),
        "wind_value": w.get("wind_value"),
        "skies": w.get("skies"),
        "track_water": w.get("track_water"),
        "allow_fog": w.get("allow_fog"),
    }


def forecast_in_session(forecast: list[dict], max_session_minutes: float) -> list[dict]:
    """Forecast rows overlapping the session, normalized to readable units.

    `time_offset` is minutes from the simulated session start, so offset 0 is lap 0 and
    rows are hourly — coarse for a ~55 minute race, which is why the observed pace break
    is the primary onset signal and the forecast is context.
    """
    out = []
    for row in forecast:
        off = row.get("time_offset")
        if off is None:
            continue
        if off < -60 or off > max_session_minutes + 60:
            continue
        out.append({
            "offset_min": off,
            "timestamp": row.get("timestamp"),
            "in_session": 0 <= off <= max_session_minutes,
            "affects_session": bool(row.get("affects_session")),
            "precip_chance_pct": _scaled(row.get("precip_chance")),
            "precip_amount": _scaled(row.get("precip_amount")),
            "cloud_cover_pct": _scaled(row.get("cloud_cover")),
            "air_temp_c": _scaled(row.get("air_temp")),
            "rel_humidity_pct": _scaled(row.get("rel_humidity")),
            "wind_speed": _scaled(row.get("wind_speed")),
        })
    return out


def _scaled(v, factor: float = 100.0):
    """Forecast numerics are integers scaled by 100 (air_temp 2004 -> 20.04)."""
    if v is None:
        return None
    return round(v / factor, 2)


def session_minutes(by_driver: dict[int, list[dict]]) -> float:
    """Length of the race in minutes, from the largest lap session_time seen."""
    latest = 0
    for laps in by_driver.values():
        for lap in laps:
            t = lap.get("session_time") or 0
            latest = max(latest, t)
    return round(latest / 10000.0 / 60.0, 1)


# --- Tyre inference (pure) ----------------------------------------------------

def infer_tyre_call(laps: list[dict], group: list[int], median_by_lap: dict[int, float],
                    wet: dict, judge_laps: int = TYRE_JUDGE_LAPS) -> dict:
    """Infer whether a stop fitted the right tyre for the conditions.

    Compares the driver's pace on the laps after the stop against the class median for
    those same laps. During the wet phase, fitting wets shows up as going from slower
    than median to clearly faster. Compound is never exposed by the API, so this is an
    inference: it reports a label plus a confidence, and says "unclear" when the
    evidence is thin.
    """
    times = lap_seconds_map(laps)
    out_lap = group[-1]
    after = [n for n in sorted(times) if n > out_lap][:judge_laps]
    before = [n for n in sorted(times) if n < group[0]][-judge_laps:]

    def delta(ns):
        vals = [times[n] - median_by_lap[n] for n in ns if n in median_by_lap]
        return round(statistics.mean(vals), 2) if vals else None

    d_before, d_after = delta(before), delta(after)
    onset = wet.get("onset_lap")
    in_wet = onset is not None and out_lap >= onset - 1

    label, confidence = "unclear", "low"
    if d_before is not None and d_after is not None:
        gain = d_before - d_after  # positive = closer to / ahead of median after
        if in_wet:
            if gain > 3.0:
                label, confidence = "wets (pace gain vs class)", "medium"
            elif gain < -3.0:
                label, confidence = "likely stayed on drys (lost pace)", "medium"
            else:
                label, confidence = "no clear pace change", "low"
        else:
            label = "dry stop (fresh drys / fuel)"
            confidence = "medium" if abs(gain) > 1.5 else "low"
    return {"stop_out_lap": out_lap, "delta_before": d_before, "delta_after": d_after,
            "label": label, "confidence": confidence, "judged_in_wet": in_wet}


# --- Fuel (pure) --------------------------------------------------------------

def fuel_fill_limits(week: dict) -> dict[int, dict]:
    """car_id -> {max_pct_fuel_fill, power_adjust_pct, weight_penalty_kg} for the week."""
    out = {}
    for r in (week or {}).get("car_restrictions") or []:
        r = cu._to_dict(r)
        cid = r.get("car_id")
        if cid is None:
            continue
        out[cid] = {"max_pct_fuel_fill": r.get("max_pct_fuel_fill"),
                    "power_adjust_pct": r.get("power_adjust_pct"),
                    "weight_penalty_kg": r.get("weight_penalty_kg"),
                    "max_dry_tire_sets": r.get("max_dry_tire_sets")}
    return out


def fuel_bound(car_id: Optional[int], limits: dict[int, dict],
               stints: list[dict]) -> dict:
    """Upper bound on litres/lap from the OPENING stint only.

    The opening stint is the one stint whose starting fuel we can bound: nobody can
    start above capacity x max_pct_fuel_fill. Underfilling only lowers the true value,
    so `usable / laps` is a valid ceiling either way. Later stints have unknown fills
    and get no bound.
    """
    cap = FUEL_CAPACITY_L.get(car_id) if car_id is not None else None
    pct = (limits.get(car_id) or {}).get("max_pct_fuel_fill") if car_id is not None else None
    first = stints[0] if stints else None
    usable = round(cap * pct / 100.0, 2) if (cap and pct) else None
    laps = first["laps"] if first else None
    bound = round(usable / laps, 3) if (usable and laps) else None
    return {"capacity_l": cap, "max_pct_fuel_fill": pct, "usable_l": usable,
            "opening_stint_laps": laps, "l_per_lap_max": bound,
            "opening_stint_ended_with_stop": bool(first and first["ended_with_stop"])}


def pool_fuel_bounds(rows: list[dict]) -> dict[str, dict]:
    """Tightest (lowest) opening-stint bound per car model, across its drivers.

    The driver who stretched the opening stint furthest gives the bound closest to true
    consumption. Only stints that actually ended in a pit stop are trusted — a stint
    that ended at the chequered flag may have had fuel to spare.
    """
    by_car: dict[str, list[dict]] = {}
    for r in rows:
        f = r.get("fuel") or {}
        if f.get("l_per_lap_max") is None or not f.get("opening_stint_ended_with_stop"):
            continue
        by_car.setdefault(r["car_name"], []).append(r)
    out = {}
    for car, rs in by_car.items():
        best = min(rs, key=lambda r: r["fuel"]["l_per_lap_max"])
        out[car] = {
            "car_name": car,
            "drivers": len(rs),
            "usable_l": best["fuel"]["usable_l"],
            "longest_opening_stint": best["fuel"]["opening_stint_laps"],
            "l_per_lap_max": best["fuel"]["l_per_lap_max"],
            "by": best["name"],
        }
    return out


# --- Penalties (pure) ---------------------------------------------------------

def penalty_summary(events: list[dict]) -> dict:
    """Group event-log rows into penalties/lead changes, keyed by the text message.

    The log identifies drivers inside `message` (e.g. "#44 Vladim...") rather than by
    cust_id (which is 0 on penalty rows), so matching is by car number where possible.
    """
    penalties, leads, other = [], [], []
    for e in events:
        desc = str(e.get("description") or "")
        row = {"lap": e.get("lap_number"), "description": desc,
               "message": str(e.get("message") or "").strip(),
               "car_number": _car_number_from_message(e.get("message"))}
        if "penalty" in desc.lower():
            penalties.append(row)
        elif "lead change" in desc.lower():
            leads.append(row)
        else:
            other.append(row)
    kinds: dict[str, int] = {}
    for p in penalties:
        kinds[_penalty_kind(p["message"])] = kinds.get(_penalty_kind(p["message"]), 0) + 1
    return {"penalties": penalties, "lead_changes": leads, "other": other,
            "penalty_kinds": dict(sorted(kinds.items(), key=lambda kv: -kv[1]))}


def _car_number_from_message(message) -> Optional[str]:
    text = str(message or "")
    idx = text.find("#")
    if idx < 0:
        return None
    num = ""
    for ch in text[idx + 1:]:
        if ch.isdigit():
            num += ch
        else:
            break
    return num or None


def _penalty_kind(message: str) -> str:
    """Coarse penalty category from the log message, e.g. 'Too many incident points'."""
    text = str(message or "")
    marker = "Penalty:"
    if marker in text:
        text = text.split(marker, 1)[1]
    return text.split(",")[0].strip() or "unknown"


# --- Per-driver assembly (pure) -----------------------------------------------

def analyze(result: dict, chart: list[dict], week: dict, events: list[dict],
            car_class: Optional[str] = None) -> dict:
    """Build the whole report structure from raw payloads. No network access."""
    entries = build_entries(result, car_class)
    by_driver = normalize_chart(chart, keep=set(entries))
    medians = class_median_by_lap(by_driver)
    wet = detect_wet_phase(medians)
    limits = fuel_fill_limits(week)

    rows = []
    for cid, meta in entries.items():
        laps = by_driver.get(cid, [])
        flagged = pitted_laps(laps)
        stops = collapse_stops(flagged)
        stints = build_stints(laps, stops)
        baseline = clean_lap_baseline(laps)
        stop_rows = []
        for g in stops:
            stop_rows.append({
                "laps": g,
                "in_lap": g[0],
                "out_lap": g[-1],
                "time_loss_s": stop_time_loss(laps, g, baseline),
                "tyre": infer_tyre_call(laps, g, medians, wet),
            })
        times = lap_seconds_map(laps)
        dry_laps = [n for n in times
                    if wet["onset_lap"] is None or n < wet["onset_lap"]]
        wet_laps = [n for n in times
                    if wet["onset_lap"] is not None and n >= wet["onset_lap"]]
        rows.append({
            **meta,
            # Max lap *number* present, not max lap with a valid time: a driver who
            # retires in the pits has a final flagged lap with no lap time, and
            # build_stints tiles lap numbers, so both must count it the same way.
            "last_lap": max([l.get("lap_number") or 0 for l in laps] or [0]),
            "pitted_laps": flagged,
            "stops": stop_rows,
            "stop_count": len(stops),
            "stints": stints,
            "stint_laps": [s["laps"] for s in stints],
            "clean_baseline_s": baseline,
            "dry_median_s": round(statistics.median([times[n] for n in dry_laps]), 3)
                            if dry_laps else None,
            "wet_median_s": round(statistics.median([times[n] for n in wet_laps]), 3)
                            if wet_laps else None,
            "lap_times": times,
            "best_lap_s": cm._lap_seconds(meta.get("best_lap")),
            "irating_delta": (meta["new_irating"] - meta["old_irating"])
                             if (meta.get("new_irating") and meta.get("old_irating")
                                 and meta["new_irating"] > 0 and meta["old_irating"] > 0)
                             else None,
            "positions": [(l.get("lap_number"), l.get("lap_position")) for l in laps],
            "bop": limits.get(meta.get("car_id")) or {},
        })
        rows[-1]["fuel"] = fuel_bound(meta.get("car_id"), limits, stints)

    rows.sort(key=lambda r: r["finish_pic"])
    # When the field committed, vs when green pace actually fell apart. In a rain race
    # these differ: drivers stop on the first drops, several laps before the track is
    # slow enough to cross the wet threshold.
    first_stops = [r["stops"][0]["in_lap"] for r in rows if r["stops"]]
    wet["field_first_stop_lap"] = min(first_stops) if first_stops else None
    wet["field_median_first_stop_lap"] = (round(statistics.median(first_stops))
                                          if first_stops else None)
    return {
        "subsession_id": result.get("subsession_id"),
        "series_name": result.get("series_name"),
        "season": f"{result.get('season_year')}s{result.get('season_quarter')}",
        "race_week": (result.get("race_week_num") or 0) + 1,
        "track": f"{(result.get('track') or {}).get('track_name')} - "
                 f"{(result.get('track') or {}).get('config_name')}".rstrip(" -"),
        "car_class": car_class or "ALL",
        "sof": result.get("event_strength_of_field"),
        "laps_complete": result.get("event_laps_complete"),
        "num_cautions": result.get("num_cautions"),
        "num_lead_changes": result.get("num_lead_changes"),
        "splits": [cu._to_dict(s) for s in result.get("session_splits") or []],
        "weather": session_weather(result),
        "wet": wet,
        "median_by_lap": medians,
        "session_minutes": session_minutes(by_driver),
        "drivers": rows,
        "fuel_by_car": pool_fuel_bounds(rows),
        "penalties": penalty_summary(events),
    }


def reconcile(report: dict) -> list[dict]:
    """Self-checks that must hold. Printed so the numbers can be trusted or doubted."""
    checks = []
    drivers = report["drivers"]

    bad = [f"{d['name']} ({sum(d['stint_laps'])}!={d['last_lap']})" for d in drivers
           if d["stint_laps"] and sum(d["stint_laps"]) != d["last_lap"]]
    checks.append({"check": "stint laps tile the race (sum == last lap run)",
                   "ok": not bad,
                   "detail": f"all {len(drivers)} drivers consistent" if not bad
                             else f"{len(bad)} mismatched: {', '.join(bad[:4])}"})

    bad = [d["name"] for d in drivers
           if d["stop_count"] and d["stop_count"] > len(d["pitted_laps"])]
    checks.append({"check": "collapsed stops <= flagged pit laps",
                   "ok": not bad,
                   "detail": "collapse rule consistent" if not bad else str(bad[:4])})

    order = [d["finish_pic"] for d in drivers]
    checks.append({"check": "class finishing order is 1..N without gaps",
                   "ok": order == list(range(1, len(order) + 1)),
                   "detail": f"positions {order[:6]}{'...' if len(order) > 6 else ''}"})

    wet, medians = report["wet"], report["median_by_lap"]
    if wet["onset_lap"] is None:
        checks.append({"check": "weather: no wet transition detected",
                       "ok": True,
                       "detail": f"class median stayed within {wet['onset_pct']}% "
                                 f"of baseline {wet['dry_baseline']}s"})
    else:
        pct = report["weather"].get("precip_time_pct")
        laps = sorted(medians)
        share = (len([n for n in laps if n >= wet["onset_lap"]]) / len(laps) * 100
                 if laps else 0)
        agree = pct is None or abs(share - pct) < 25
        checks.append({
            "check": "wet-phase lap share agrees with session precip_time_pct",
            "ok": agree,
            "detail": f"{share:.0f}% of racing laps after onset (L{wet['onset_lap']}) "
                      f"vs precip_time_pct {pct}",
        })
    return checks


# --- Rendering ----------------------------------------------------------------

def _fmt(v, spec="", dash="—"):
    """format() that pads the missing-value dash to the spec's width.

    A bare "—" is one character wide, so without this every missing value shifts the
    rest of the row left and the table stops lining up.
    """
    if v is not None:
        return format(v, spec)
    m = re.match(r"[<>^+\- ]*(\d+)", spec)
    width = int(m.group(1)) if m else 0
    return dash.ljust(width) if spec.startswith("<") else dash.rjust(width)


def render_header(r: dict) -> str:
    w, wet = r["weather"], r["wet"]
    lines = [
        f"{r['series_name']} — {r['track']}",
        f"subsession {r['subsession_id']} | {r['season']} week {r['race_week']} | "
        f"class {r['car_class']} | SoF {r['sof']} | {r['laps_complete']} laps | "
        f"{r['session_minutes']:.0f} min",
    ]
    if r["splits"]:
        sofs = [s.get("event_strength_of_field") for s in r["splits"]]
        try:
            mine = [s.get("subsession_id") for s in r["splits"]].index(r["subsession_id"]) + 1
        except ValueError:
            mine = "?"
        lines.append(f"split {mine} of {len(sofs)} (SoF by split: "
                     f"{', '.join(str(s) for s in sofs)})")
    lines.append(
        f"weather: precip {_fmt(w.get('precip_time_pct'), '.1f')}% of session | "
        f"temp {_fmt(w.get('temp_value'))} | sim start {w.get('simulated_start_time')} | "
        f"cautions {r['num_cautions']} | lead changes {r['num_lead_changes']}")
    if wet["onset_lap"] is None:
        lines.append(f"conditions: no wet transition detected "
                     f"(class median ~{_fmt(wet['dry_baseline'], '.1f')}s throughout)")
    else:
        lines.append(
            f"conditions: DRY until L{wet['onset_lap'] - 1} "
            f"(median {_fmt(wet['dry_baseline'], '.1f')}s) -> WET from L{wet['onset_lap']} "
            f"(median {_fmt(wet['wet_median'], '.1f')}s, worst L{wet['peak_lap']} at "
            f"{_fmt(wet['peak'], '.1f')}s, +"
            f"{(wet['peak'] / wet['dry_baseline'] - 1) * 100:.0f}%)")
        med, first = wet.get("field_median_first_stop_lap"), wet.get("field_first_stop_lap")
        if med is not None:
            lead = wet["onset_lap"] - med
            lines.append(
                f"strategy clock: first stop of the class on L{first}, median first stop "
                f"L{med} — {abs(lead)} lap(s) "
                f"{'BEFORE' if lead > 0 else 'after'} green pace crossed the wet "
                f"threshold, i.e. the field committed on the first drops, not on lap time")
    return "\n".join(lines)


def render_strategy_table(r: dict, highlight: Optional[int] = None) -> str:
    head = (f"{'Pos':>3} {'#':>4} {'Driver':<24} {'Car':<22} {'Grid':>4} {'Stops':>5} "
            f"{'Stints':<14} {'Pit loss':>9} {'Dry':>7} {'Wet':>7} {'Best':>9} "
            f"{'Inc':>4} {'iR':>7}")
    lines = [head, "-" * len(head)]
    for d in r["drivers"]:
        losses = [s["time_loss_s"] for s in d["stops"] if s["time_loss_s"] is not None]
        mark = "*" if highlight is not None and d["cust_id"] == highlight else " "
        lines.append(
            f"{d['finish_pic']:>3}{mark}{str(d['car_number'] or ''):>3} "
            f"{str(d['name'])[:24]:<24} {str(d['car_name'])[:22]:<22} "
            f"{d['start_pic']:>4} {d['stop_count']:>5} "
            f"{'+'.join(str(x) for x in d['stint_laps'])[:14]:<14} "
            f"{_fmt(round(sum(losses), 1) if losses else None, '>9.1f')} "
            f"{_fmt(d['dry_median_s'], '>7.1f')} {_fmt(d['wet_median_s'], '>7.1f')} "
            f"{cm._fmt_lap(d['best_lap']):>9} {d['incidents']:>4} "
            f"{_fmt(d['irating_delta'], '>+7d')}")
    return "\n".join(lines)


def render_fuel(r: dict) -> str:
    lines = ["Fuel — upper bound on litres/lap from the opening stint",
             "  (capacity x series max fill; a car cannot start above that, so this is a",
             "   ceiling. Litres per stop are NOT derivable from pit time.)",
             ""]
    head = (f"{'Car':<26} {'Tank':>7} {'Fill':>5} {'Usable':>7} {'Stint':>6} "
            f"{'<= L/lap':>9} {'Drivers':>7}  Longest by")
    lines += [head, "-" * len(head)]
    bounds = [f["l_per_lap_max"] for f in r["fuel_by_car"].values()
              if f["l_per_lap_max"]]
    tightest = min(bounds) if bounds else None
    for car, f in sorted(r["fuel_by_car"].items(),
                         key=lambda kv: -(kv[1]["l_per_lap_max"] or 0)):
        cap = FUEL_CAPACITY_L.get(
            next((d["car_id"] for d in r["drivers"] if d["car_name"] == car), None))
        pct = next((d["bop"].get("max_pct_fuel_fill") for d in r["drivers"]
                    if d["car_name"] == car), None)
        # A bound far above the class-best one is weak, not a different consumption:
        # that driver's opening stint was cut short (repairs, a spin, an early splash),
        # so the inequality still holds but says little.
        weak = (tightest and f["l_per_lap_max"] and f["l_per_lap_max"] > tightest * 1.25)
        lines.append(
            f"{car[:26]:<26} {_fmt(cap, '>7.1f')} {_fmt(pct, '>4')}% "
            f"{_fmt(f['usable_l'], '>7.1f')} {f['longest_opening_stint']:>6} "
            f"{f['l_per_lap_max']:>9.2f} {f['drivers']:>7}  {f['by']}"
            f"{'   [weak — stint cut short]' if weak else ''}")
    if not r["fuel_by_car"]:
        lines.append("  (no opening stint ended in a pit stop — no bound derivable)")
    elif tightest:
        lines.append("")
        lines.append(f"  tightest bound in class: {tightest:.2f} L/lap — the best evidence "
                     f"of true dry consumption,")
        lines.append(f"  since the longest opening stint is the one closest to running the "
                     f"tank out.")
    return "\n".join(lines)


def render_bop(r: dict) -> str:
    seen = {}
    for d in r["drivers"]:
        if d["car_name"] not in seen and d["bop"]:
            seen[d["car_name"]] = d["bop"]
    if not seen:
        return ""
    head = f"{'Car':<26} {'Fill %':>6} {'Power %':>8} {'Weight kg':>10} {'Dry sets':>9}"
    lines = ["BoP and fuel limits for this week (from the season schedule)", "",
             head, "-" * len(head)]
    for car, b in sorted(seen.items()):
        lines.append(f"{car[:26]:<26} {_fmt(b.get('max_pct_fuel_fill'), '>6')} "
                     f"{_fmt(b.get('power_adjust_pct'), '>8')} "
                     f"{_fmt(b.get('weight_penalty_kg'), '>10')} "
                     f"{_fmt(b.get('max_dry_tire_sets'), '>9')}")
    return "\n".join(lines)


def render_weather(r: dict, forecast: list[dict]) -> str:
    lines = ["Weather timeline"]
    med = r["median_by_lap"]
    if med:
        lines.append("  class median lap time by lap:")
        row = "   "
        for n in sorted(med):
            row += f" L{n}:{med[n]:.0f}"
            if len(row) > 96:
                lines.append(row)
                row = "   "
        if row.strip():
            lines.append(row)
    if forecast:
        lines.append("")
        lines.append(f"  forecast (hourly; offset is minutes from session start, "
                     f"session ran {r['session_minutes']:.0f} min):")
        head = (f"   {'Offset':>7} {'In sess':>8} {'Precip %':>9} {'Amount':>7} "
                f"{'Cloud %':>8} {'Air C':>6} {'Humid %':>8}")
        lines += [head, "   " + "-" * (len(head) - 3)]
        for f in forecast:
            lines.append(
                f"   {f['offset_min']:>7} {'yes' if f['in_session'] else 'no':>8} "
                f"{_fmt(f['precip_chance_pct'], '>9.0f')} "
                f"{_fmt(f['precip_amount'], '>7.2f')} "
                f"{_fmt(f['cloud_cover_pct'], '>8.0f')} "
                f"{_fmt(f['air_temp_c'], '>6.1f')} "
                f"{_fmt(f['rel_humidity_pct'], '>8.0f')}")
    else:
        lines.append("  (no forecast available for this week)")
    return "\n".join(lines)


def render_tyres(r: dict, highlight: Optional[int] = None) -> str:
    lines = ["Pit stops and inferred tyre calls",
             "  (compound is never exposed by the API — inferred from post-stop pace",
             "   vs the class median on the same laps)", ""]
    head = (f"{'Pos':>3} {'Driver':<22} {'Stop':>4} {'In':>3} {'Out':>4} {'Loss':>7} "
            f"{'d-before':>9} {'d-after':>8}  Inference")
    lines += [head, "-" * len(head)]
    for d in r["drivers"]:
        for i, s in enumerate(d["stops"], 1):
            mark = "*" if highlight is not None and d["cust_id"] == highlight else " "
            t = s["tyre"]
            lines.append(
                f"{d['finish_pic']:>3}{mark}{str(d['name'])[:21]:<22} {i:>4} "
                f"{s['in_lap']:>3} {s['out_lap']:>4} "
                f"{_fmt(s['time_loss_s'], '>7.1f')} "
                f"{_fmt(t['delta_before'], '>9.1f')} {_fmt(t['delta_after'], '>8.1f')}  "
                f"{t['label']} ({t['confidence']})")
    return "\n".join(lines)


def render_penalties(r: dict) -> str:
    p = r["penalties"]
    if not p["penalties"] and not p["lead_changes"]:
        return "Penalties: none in the event log."
    lines = ["Penalties and race control"]
    if p["penalty_kinds"]:
        lines.append("  " + ", ".join(f"{k} x{v}" for k, v in p["penalty_kinds"].items()))
    for row in p["penalties"][:20]:
        lines.append(f"   L{_fmt(row['lap'], '>2')}  {row['message'][:100]}")
    if len(p["penalties"]) > 20:
        lines.append(f"   ... and {len(p['penalties']) - 20} more")
    if p["lead_changes"]:
        lines.append(f"  lead changes: {len(p['lead_changes'])}")
        for row in p["lead_changes"]:
            lines.append(f"   L{_fmt(row['lap'], '>2')}  {row['message'][:100]}")
    return "\n".join(lines)


def render_reconcile(checks: list[dict]) -> str:
    lines = ["Reconciliation"]
    for c in checks:
        lines.append(f"  [{'ok ' if c['ok'] else 'WARN'}] {c['check']}: {c['detail']}")
    return "\n".join(lines)


# --- Export -------------------------------------------------------------------

EXPORT_FIELDS = [
    "finish_pic", "start_pic", "car_number", "name", "car_name", "car_class",
    "stop_count", "stint_laps", "pit_loss_s", "dry_median_s", "wet_median_s",
    "best_lap_s", "incidents", "old_irating", "new_irating", "irating_delta",
    "max_pct_fuel_fill", "usable_l", "opening_stint_laps", "l_per_lap_max",
    "tyre_calls",
]


def build_export_rows(r: dict) -> list[dict]:
    rows = []
    for d in r["drivers"]:
        losses = [s["time_loss_s"] for s in d["stops"] if s["time_loss_s"] is not None]
        f = d["fuel"]
        rows.append({
            "finish_pic": d["finish_pic"], "start_pic": d["start_pic"],
            "car_number": d["car_number"], "name": d["name"],
            "car_name": d["car_name"], "car_class": d["car_class"],
            "stop_count": d["stop_count"],
            "stint_laps": "+".join(str(x) for x in d["stint_laps"]),
            "pit_loss_s": round(sum(losses), 1) if losses else None,
            "dry_median_s": d["dry_median_s"], "wet_median_s": d["wet_median_s"],
            "best_lap_s": d["best_lap_s"], "incidents": d["incidents"],
            "old_irating": d["old_irating"], "new_irating": d["new_irating"],
            "irating_delta": d["irating_delta"],
            "max_pct_fuel_fill": f["max_pct_fuel_fill"], "usable_l": f["usable_l"],
            "opening_stint_laps": f["opening_stint_laps"],
            "l_per_lap_max": f["l_per_lap_max"],
            "tyre_calls": " | ".join(s["tyre"]["label"] for s in d["stops"]),
        })
    return rows


def export_csv(rows: list[dict]) -> str:
    out = io.StringIO()
    w = csv.DictWriter(out, fieldnames=EXPORT_FIELDS)
    w.writeheader()
    w.writerows(rows)
    return out.getvalue().rstrip("\r\n")


def export_json(report: dict) -> str:
    return json.dumps(report, indent=2, default=str)


# --- CLI ----------------------------------------------------------------------

def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="race_report",
        description="Race strategy report for one iRacing subsession: stints, pit "
                    "stops, inferred tyre calls, fuel bounds, weather and penalties.",
    )
    p.add_argument("--subsession", type=int, required=True,
                   help="Subsession id to analyze (e.g. 87466883)")
    p.add_argument("--class", dest="car_class", default=None,
                   help="Car class short name (e.g. IMSA23). Omit for every class.")
    p.add_argument("--cust-id", type=int, default=None,
                   help="Highlight this driver with a * in the tables")
    p.add_argument("--no-forecast", action="store_true",
                   help="Skip the signed weather-forecast fetch")
    fmt = p.add_mutually_exclusive_group()
    fmt.add_argument("--csv", dest="output_csv", action="store_true",
                     help="Output CSV to stdout (logs/progress stay on stderr)")
    fmt.add_argument("--json", dest="output_json", action="store_true",
                     help="Output the full report as JSON to stdout")
    p.add_argument("--verbose", "-v", action="count", default=0,
                   help="Verbose logging: -v INFO, -vv DEBUG")
    return p.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    # Windows consoles default to cp1252, which cannot encode names like "Pakuła" —
    # without this the whole report dies on one driver's name at print time.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):  # pragma: no cover - non-reconfigurable stream
            pass
    level = (logging.DEBUG if args.verbose >= 2
             else logging.INFO if args.verbose == 1 else logging.WARNING)
    logging.basicConfig(level=level,
                        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
                        datefmt="%H:%M:%S")
    logger.setLevel(level)
    for noisy in ("urllib3", "iracingdataapi", "requests", "botocore", "boto3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    try:
        pool = RefreshingClient()
        data = fetch_race_data(pool, args.subsession)
        if args.no_forecast:
            data["forecast"] = []
    except cu.IRacingAPIError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except Exception as e:  # pragma: no cover - surfaced with -vv
        logger.debug("Unexpected error", exc_info=True)
        print(f"Unexpected error: {e}\nRe-run with -vv for a traceback.",
              file=sys.stderr)
        return 1

    report = analyze(data["result"], data["chart"], data["week"], data["events"],
                     args.car_class)
    if not report["drivers"]:
        print(f"No entries found for class {args.car_class!r} in subsession "
              f"{args.subsession}.", file=sys.stderr)
        return 1
    forecast = forecast_in_session(data["forecast"], report["session_minutes"])
    report["forecast"] = forecast
    report["reconciliation"] = reconcile(report)

    if args.output_json:
        print(export_json(report))
        return 0
    if args.output_csv:
        print(export_csv(build_export_rows(report)))
        return 0

    blocks = [render_header(report), render_strategy_table(report, args.cust_id),
              render_weather(report, forecast), render_tyres(report, args.cust_id),
              render_fuel(report), render_bop(report), render_penalties(report),
              render_reconcile(report["reconciliation"])]
    print("\n\n".join(b for b in blocks if b))
    return 0


if __name__ == "__main__":
    sys.exit(main())
