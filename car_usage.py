#!/usr/bin/env python3
"""Standalone tool: which car was most used in an iRacing series/class this week.

Counts *race entries* (one tally per driver-entry that started a race session) for
every car in a given car class, across all of the week's splits/sessions.

Built for the iRacing IMSA series, GT3 / IMSA23 class, but works for any series+class.

This script is self-contained — it does NOT import the iracing_sr_optimizer package.
It only needs `iracingdataapi>=1.4.2` (pip install -r requirements.txt) and your own
iRacing OAuth credentials in the environment. No credentials are baked into the code.

Usage:
    export IRACING_EMAIL=...
    export IRACING_PASSWORD=...
    export IRACING_CLIENT_ID=...
    export IRACING_CLIENT_SECRET=...
    python car_usage.py --series IMSA --class IMSA23

Run `python car_usage.py --help` for all options.

Authentication uses the OAuth `password_limited` grant (legacy cookie auth was retired
by iRacing on Dec 9, 2025). The masking algorithm is the same one the rest of this repo
uses; see https://oauth.iracing.com/oauth2/book/token_endpoint.html
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import json
import logging
import os
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Optional

logger = logging.getLogger("car_usage")

# --- Config (mirrors iracing_sr_optimizer/config.py + iracing_api.py) ---------

CACHE_DIR = Path.home() / ".iracing_sr_cache"
TOKEN_CACHE_FILE = CACHE_DIR / "oauth_token.json"
RESULTS_CACHE_DIR = CACHE_DIR / "results"
IRACING_TOKEN_URL = "https://oauth.iracing.com/oauth2/token"

# iRacing event_type codes: 2=practice, 3=qualify, 4=time trial, 5=race.
RACE_EVENT_TYPE = 5

# Polite throttle between live result() calls (seconds). The library also handles
# 429 backoff, but a small gap keeps us well under iRacing's rate limits.
THROTTLE_SECONDS = 0.25

# Retry/backoff for rate limiting (HTTP 429) on any API call.
MAX_RETRIES = 5
BACKOFF_BASE_SECONDS = 2.0


class IRacingAPIError(Exception):
    pass


def _is_rate_limit(exc: Exception) -> bool:
    """Best-effort detection of an iRacing rate-limit (HTTP 429) across error types."""
    status = getattr(exc, "status", None) or getattr(exc, "code", None)
    if status == 429:
        return True
    resp = getattr(exc, "response", None)
    if resp is not None and getattr(resp, "status_code", None) == 429:
        return True
    text = str(exc).lower()
    return "429" in text or "rate limit" in text or "too many requests" in text


def _api_call(label: str, fn, *args, **kwargs):
    """Call an iRacing API function, logging errors and retrying on rate limits.

    Logs every failure (with the call label), backs off exponentially on HTTP 429,
    and re-raises as IRacingAPIError once retries are exhausted or for non-retryable
    errors. `label` is a short human description used in log lines, e.g. "result(123)".
    """
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            if _is_rate_limit(exc) and attempt < MAX_RETRIES:
                wait = BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
                logger.warning(
                    "Rate limited on %s (attempt %d/%d): %s — backing off %.0fs",
                    label, attempt, MAX_RETRIES, exc, wait,
                )
                time.sleep(wait)
                continue
            if _is_rate_limit(exc):
                logger.error("Rate limited on %s; giving up after %d attempts: %s",
                             label, attempt, exc)
            else:
                logger.error("API error on %s (attempt %d): %s",
                             label, attempt, exc)
            logger.debug("Traceback for %s failure", label, exc_info=True)
            raise IRacingAPIError(f"{label} failed: {exc}") from exc


# --- OAuth (copied from iracing_sr_optimizer/iracing_api.py, self-contained) --

def _mask_secret(secret: str, identifier: str) -> str:
    """Mask a secret using iRacing's masking algorithm.

    SHA-256 hash of secret + normalized identifier, then base64 encoded.
    Used for both password (identifier=username) and client_secret (identifier=client_id).
    See https://oauth.iracing.com/oauth2/book/token_endpoint.html
    """
    normalized_id = identifier.strip().lower()
    combined = f"{secret}{normalized_id}"
    return base64.b64encode(
        hashlib.sha256(combined.encode("utf-8")).digest()
    ).decode("utf-8")


def _get_oauth_token() -> str:
    """Get an access token via the OAuth password_limited grant.

    Raises IRacingAPIError if credentials are missing or auth fails.
    """
    email = os.environ.get("IRACING_EMAIL", "")
    password = os.environ.get("IRACING_PASSWORD", "")
    client_id = os.environ.get("IRACING_CLIENT_ID", "")
    client_secret = os.environ.get("IRACING_CLIENT_SECRET", "")

    # Log which credentials are present without leaking any values.
    logger.debug(
        "Credentials present: email=%s password=%s client_id=%s client_secret=%s",
        bool(email), bool(password), bool(client_id), bool(client_secret),
    )

    if not client_id or not client_secret:
        raise IRacingAPIError(
            "Missing OAuth credentials. Set IRACING_CLIENT_ID and IRACING_CLIENT_SECRET.\n"
            "Register at https://oauth.iracing.com/oauth2/book/client_registration.html\n"
            "Legacy cookie-based auth was retired by iRacing on Dec 9, 2025."
        )
    if not email or not password:
        raise IRacingAPIError(
            "Missing iRacing credentials. Set IRACING_EMAIL and IRACING_PASSWORD."
        )

    # Check cached token
    if TOKEN_CACHE_FILE.exists():
        try:
            with open(TOKEN_CACHE_FILE) as f:
                cached = json.load(f)
            if cached.get("expires_at", 0) > time.time() + 30:
                ttl = int(cached.get("expires_at", 0) - time.time())
                logger.info("Using cached OAuth token (expires in %ds)", ttl)
                return cached["access_token"]
            logger.debug("Cached token expired or expiring soon; requesting new one")
        except (json.JSONDecodeError, KeyError) as e:
            logger.debug("Token cache unreadable (%s); requesting new token", e)

    logger.info("Requesting new OAuth token from %s", IRACING_TOKEN_URL)
    masked_pw = _mask_secret(password, email)
    masked_secret = _mask_secret(client_secret, client_id)

    data = urllib.parse.urlencode({
        "grant_type": "password_limited",
        "username": email,
        "password": masked_pw,
        "client_id": client_id,
        "client_secret": masked_secret,
        "scope": "iracing.auth",
    }).encode()

    req = urllib.request.Request(
        IRACING_TOKEN_URL,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            token_data = json.loads(resp.read())
    except Exception as e:
        raise IRacingAPIError(f"OAuth token request failed: {e}")

    access_token = token_data.get("access_token")
    if not access_token:
        raise IRacingAPIError(f"No access_token in OAuth response: {token_data}")

    expires_in = token_data.get("expires_in", 600)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with open(TOKEN_CACHE_FILE, "w") as f:
        json.dump({
            "access_token": access_token,
            "expires_at": time.time() + expires_in,
        }, f)

    logger.info("Got new OAuth access token")
    return access_token


def get_client():
    """Create an authenticated irDataClient via the OAuth password_limited grant."""
    try:
        from iracingdataapi import irDataClient
    except ImportError:
        raise IRacingAPIError(
            "iracingdataapi is not installed. Run: pip install -r requirements.txt"
        )
    token = _get_oauth_token()
    try:
        logger.info("Authenticating via OAuth password_limited grant")
        try:
            # use_pydantic=True silences the raw-dict deprecation warning; our
            # _to_dict() already normalizes pydantic models back to plain dicts.
            return irDataClient(access_token=token, use_pydantic=True)
        except TypeError:
            # Older iracingdataapi without the kwarg.
            return irDataClient(access_token=token)
    except Exception as e:
        raise IRacingAPIError(f"Failed to create client with OAuth token: {e}")


def _to_dict(obj) -> dict:
    """Convert a pydantic model or dict to a plain dict (from iracing_api.py)."""
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    if hasattr(obj, "dict"):
        return obj.dict()
    return dict(obj)


# --- Series / season / week resolution ---------------------------------------

def resolve_series(
    client, series_query: Optional[str], series_id: Optional[int]
) -> tuple[int, str]:
    """Resolve a (series_id, series_name) pair from an explicit id or a name query.

    If `series_id` is given, it takes precedence (name is looked up for display).
    Otherwise match by name: a case-insensitive **exact** name match wins over any
    substring match — this prevents e.g. "IMSA iRacing Series" from being shadowed by
    "IMSA iRacing Series - Fixed". Raises IRacingAPIError if nothing resolves.
    """
    all_series = [_to_dict(s) for s in _api_call("get_series()", client.get_series)]
    logger.debug("Scanning %d series", len(all_series))

    # Explicit id wins.
    if series_id is not None:
        for sd in all_series:
            if sd.get("series_id") == series_id:
                name = sd.get("series_name", "") or ""
                logger.info("Resolved series by id -> [%s] %s", series_id, name)
                return series_id, name
        raise IRacingAPIError(f"No series found with series_id {series_id}.")

    q = (series_query or "").lower()
    exact = []
    substring = []
    for sd in all_series:
        name = sd.get("series_name", "") or ""
        sid = sd.get("series_id")
        if sid is None:
            continue
        if name.lower() == q:
            exact.append((sid, name))
        elif q in name.lower():
            substring.append((sid, name))

    if exact:
        logger.info("Resolved series by exact name -> [%s] %s", exact[0][0], exact[0][1])
        return exact[0]

    if not substring:
        raise IRacingAPIError(
            f"No series matched {series_query!r}. Try a different --series substring "
            f"or pass --series-id."
        )

    if len(substring) > 1:
        logger.warning("Multiple series matched %r (no exact match):", series_query)
        for sid, name in substring:
            logger.warning("  [%s] %s", sid, name)
        logger.warning("Using the first. Pass --series-id N to pick exactly.")

    logger.info("Resolved series %r -> [%s] %s",
                series_query, substring[0][0], substring[0][1])
    return substring[0]


def resolve_season_and_week(
    client,
    series_id: int,
    week_override: Optional[int],
    year_override: Optional[int],
    quarter_override: Optional[int],
) -> tuple[int, int, int, Optional[str]]:
    """Resolve (season_year, season_quarter, race_week_num, track_name) for a series.

    Uses the active season for `series_id` and the schedule entry whose date range
    contains today. Any of the three values can be overridden via CLI args.
    race_week_num is 0-indexed (matching the API); CLI --week is 1-indexed.
    track_name is the track raced that week (None if not found).
    """
    seasons = list(_api_call(
        "series_seasons()", client.series_seasons, include_series=True
    ))
    logger.debug("Searching %d seasons for active season of series_id %d",
                 len(seasons), series_id)
    season = None
    for s in seasons:
        sd = _to_dict(s)
        if sd.get("series_id") == series_id and sd.get("active", False):
            season = sd
            logger.debug("Found active season_id %s", sd.get("season_id"))
            break
    # Fall back to any season for the series if none is flagged active.
    if season is None:
        for s in seasons:
            sd = _to_dict(s)
            if sd.get("series_id") == series_id:
                season = sd
                logger.warning(
                    "No active season for series_id %d; falling back to season_id %s",
                    series_id, sd.get("season_id"),
                )
                break
    if season is None:
        raise IRacingAPIError(f"No season found for series_id {series_id}.")

    season_year = year_override or season.get("season_year")
    season_quarter = quarter_override or season.get("season_quarter")
    if year_override or quarter_override:
        logger.info("Season year/quarter overridden via CLI: year=%s quarter=%s",
                    year_override, quarter_override)

    if week_override is not None:
        race_week_num = week_override - 1  # CLI is 1-indexed, API is 0-indexed
        logger.info("Race week overridden via CLI: week %d (race_week_num %d)",
                    week_override, race_week_num)
    else:
        race_week_num = _current_race_week(season)

    if season_year is None or season_quarter is None:
        raise IRacingAPIError(
            "Could not determine season_year/season_quarter; pass --year and --quarter."
        )

    track_name = _track_for_week(season, race_week_num)
    logger.info("Resolved season: %ds%s, race_week_num %d, track %s",
                season_year, season_quarter, race_week_num, track_name or "?")
    return int(season_year), int(season_quarter), int(race_week_num), track_name


def _track_for_week(season: dict, race_week_num: int) -> Optional[str]:
    """Track name raced in a given week, formatted '<track_name> - <config_name>'.

    Mirrors iracing_sr_optimizer/fetch_schedule.py:_format_track_name. None if absent.
    """
    for sched in season.get("schedules", []) or []:
        sd = _to_dict(sched)
        if sd.get("race_week_num") == race_week_num:
            track = _to_dict(sd.get("track", {}) or {})
            name = track.get("track_name")
            if not name:
                return None
            config = track.get("config_name") or ""
            return f"{name} - {config}" if config else name
    return None


def _current_race_week(season: dict) -> int:
    """Pick the current race_week_num from a season's schedules by today's date.

    iRacing weeks start on a date; the current week is the latest schedule whose
    start_date is on or before today. Falls back to the season's max week, then 0.
    """
    today = dt.date.today()
    schedules = season.get("schedules", []) or []
    current = None
    current_start = None
    max_week = 0
    for sched in schedules:
        sd = _to_dict(sched)
        wk = sd.get("race_week_num", 0)
        max_week = max(max_week, wk)
        start = sd.get("start_date", "") or ""
        if "T" in start:
            start = start.split("T")[0]
        try:
            start_date = dt.date.fromisoformat(start)
        except (ValueError, TypeError):
            continue
        if start_date <= today and (current_start is None or start_date > current_start):
            current_start = start_date
            current = wk
    if current is not None:
        logger.debug("Current week %d inferred from start_date %s (today %s)",
                     current, current_start, today)
        return current
    logger.warning("Could not infer current week from dates; using last week (%d).", max_week)
    return max_week


# --- Subsession discovery + fetch --------------------------------------------

def find_week_subsessions(
    client,
    series_id: int,
    season_year: int,
    season_quarter: int,
    race_week_num: int,
    event_type: int,
) -> list[int]:
    """Return all subsession_ids for the given series + week (race events only)."""
    logger.info(
        "Searching results: series_id=%d %ds%s race_week_num=%d event_types=%s",
        series_id, season_year, season_quarter, race_week_num, [event_type],
    )
    raw = list(_api_call(
        "result_search_series()",
        client.result_search_series,
        season_year=season_year,
        season_quarter=season_quarter,
        series_id=series_id,
        race_week_num=race_week_num,
        event_types=[event_type],
        official_only=True,
    ))
    logger.debug("result_search_series returned %d rows", len(raw))
    ids = []
    for row in raw:
        rd = _to_dict(row)
        sid = rd.get("subsession_id")
        if sid is not None:
            ids.append(int(sid))
    # De-dup while preserving order (search can return a row per simsession).
    seen = set()
    unique = []
    for sid in ids:
        if sid not in seen:
            seen.add(sid)
            unique.append(sid)
    logger.info("Discovered %d unique subsessions (%d raw rows)", len(unique), len(raw))
    return unique


def fetch_result(client, subsession_id: int) -> dict:
    """Fetch one subsession result, caching the immutable final result on disk."""
    RESULTS_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file = RESULTS_CACHE_DIR / f"{subsession_id}.json"
    if cache_file.exists():
        try:
            with open(cache_file) as f:
                result = json.load(f)
            logger.debug("Cache hit for subsession %s", subsession_id)
            return result
        except (json.JSONDecodeError, OSError):
            logger.warning("Corrupt cache for %s, refetching.", subsession_id)

    logger.debug("Cache miss for subsession %s; fetching from API", subsession_id)
    result = _to_dict(_api_call(
        f"result({subsession_id})", client.result, subsession_id=subsession_id
    ))
    try:
        with open(cache_file, "w") as f:
            json.dump(result, f)
    except OSError as e:
        logger.warning("Could not cache result %s: %s", subsession_id, e)
    return result


# --- Tally (pure, unit-testable) ---------------------------------------------

def count_cars(results: list[dict], car_class_short_name: str) -> Counter:
    """Count race entries per car name within a car class across many results.

    `results` is a list of subsession-result dicts (as returned by the Data API's
    `result` endpoint). For each result we look at the race simsession's rows and
    count one entry per row (driver-entry) whose car class matches
    `car_class_short_name` (case-insensitive). Returns a Counter: car_name -> entries.
    """
    target = car_class_short_name.strip().lower()
    counts: Counter = Counter()
    total_rows = 0
    for result in results:
        class_map = _build_class_map(result)
        for row in _iter_race_rows(result):
            total_rows += 1
            short = _row_class_short_name(row, class_map)
            if short is not None and short.strip().lower() == target:
                car_name = row.get("car_name") or f"car_id:{row.get('car_id')}"
                counts[car_name] += 1
    logger.debug(
        "Tallied %d entries in class %r from %d race rows across %d subsessions",
        sum(counts.values()), car_class_short_name, total_rows, len(results),
    )
    return counts


def count_cars_by_class(results: list[dict]) -> dict[str, Counter]:
    """Count race entries per car, grouped by car class, across many results.

    Returns {class_short_name -> Counter(car_name -> entries)}. Rows whose class can't
    be resolved are bucketed under "(unknown)" so no entry is silently dropped.
    """
    by_class: dict[str, Counter] = {}
    for result in results:
        class_map = _build_class_map(result)
        for row in _iter_race_rows(result):
            short = _row_class_short_name(row, class_map) or "(unknown)"
            car_name = row.get("car_name") or f"car_id:{row.get('car_id')}"
            by_class.setdefault(short, Counter())[car_name] += 1
    logger.debug(
        "Tallied %d classes across %d subsessions: %s",
        len(by_class), len(results),
        {c: sum(v.values()) for c, v in by_class.items()},
    )
    return by_class


def class_short_names(results: list[dict]) -> Counter:
    """Distinct car-class short names seen across results (for --class hints)."""
    seen: Counter = Counter()
    for result in results:
        class_map = _build_class_map(result)
        for row in _iter_race_rows(result):
            short = _row_class_short_name(row, class_map)
            if short:
                seen[short] += 1
    return seen


def _build_class_map(result: dict) -> dict:
    """Map car_class_id -> short name from a result's top-level `car_classes` array.

    iRacing result rows reliably carry `car_class_id`; the human short name lives in
    the subsession's top-level `car_classes` list. We prefer `short_name`, falling back
    to `name`. Empty map if the result has no `car_classes` (then row fields are used).
    """
    class_map = {}
    for cc in result.get("car_classes", []) or []:
        ccd = _to_dict(cc)
        cid = ccd.get("car_class_id")
        short = ccd.get("short_name") or ccd.get("name")
        if cid is not None and short:
            class_map[cid] = str(short)
    return class_map


def _iter_race_rows(result: dict):
    """Yield per-driver result rows from the RACE simsession of one result dict.

    The race is identified by simsession name (case-insensitive) rather than a numeric
    code, since iRacing's simsession_type integers are not the same as event_type codes.
    Falls back to the last simsession (the race is always last) if no name matches.
    """
    simsessions = result.get("session_results") or []
    race_sessions = [ss for ss in simsessions if _is_race_simsession(ss)]
    chosen = race_sessions or (simsessions[-1:] if simsessions else [])
    for ss in chosen:
        for row in ss.get("results", []) or []:
            yield row


def _is_race_simsession(ss: dict) -> bool:
    """True if a simsession is the race, by its name fields (not numeric type)."""
    for key in ("simsession_type_name", "simsession_name"):
        if str(ss.get(key, "")).strip().lower() == "race":
            return True
    return False


def _row_class_short_name(row: dict, class_map: dict) -> Optional[str]:
    """Resolve a result row's car-class short name.

    Prefer the top-level `car_classes` map keyed by the row's `car_class_id`; fall back
    to short-name fields that may be present directly on the row.
    """
    cid = row.get("car_class_id")
    if cid is not None and cid in class_map:
        return class_map[cid]
    for key in ("car_class_short_name", "car_class_name"):
        val = row.get(key)
        if val:
            return str(val)
    return None


# --- Output -------------------------------------------------------------------

def format_ranking(counts: Counter, top: Optional[int]) -> str:
    total = sum(counts.values())
    lines = []
    ranked = counts.most_common(top)
    width = max((len(name) for name, _ in ranked), default=4)
    lines.append(f"{'#':>2}  {'Car':<{width}}  {'Entries':>7}  {'Share':>6}")
    lines.append("-" * (2 + 2 + width + 2 + 7 + 2 + 6))
    for i, (name, n) in enumerate(ranked, 1):
        share = (n / total * 100) if total else 0.0
        lines.append(f"{i:>2}  {name:<{width}}  {n:>7}  {share:>5.1f}%")
    return "\n".join(lines)


def format_all_classes(by_class: dict[str, Counter], top: Optional[int]) -> str:
    """Render a per-class ranking, classes ordered by total entries (most first)."""
    blocks = []
    ordered = sorted(by_class.items(), key=lambda kv: sum(kv[1].values()), reverse=True)
    for class_name, counts in ordered:
        total = sum(counts.values())
        blocks.append(f"{class_name} — {total} entries")
        blocks.append(format_ranking(counts, top))
        blocks.append("")  # blank line between classes
    return "\n".join(blocks).rstrip()


# --- CLI ----------------------------------------------------------------------

def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="car_usage",
        description="Find the most-used car in an iRacing series/class for a week "
                    "(counts race entries across all splits).",
    )
    p.add_argument("--series", default="IMSA",
                   help="Series name to match: exact name wins, else substring "
                        "(default: IMSA). Ignored if --series-id is given.")
    p.add_argument("--series-id", type=int, default=None,
                   help="Select a series by exact id (e.g. 447 for IMSA iRacing Series); "
                        "overrides --series")
    p.add_argument("--class", dest="car_class", default=None,
                   help="Car class short name to count (e.g. IMSA23). "
                        "Omit to rank the most-used car in every class.")
    p.add_argument("--week", type=int, default=None,
                   help="Race week, 1-indexed (default: current week)")
    p.add_argument("--year", type=int, default=None,
                   help="Season year override (default: active season)")
    p.add_argument("--quarter", type=int, default=None,
                   help="Season quarter override 1-4 (default: active season)")
    p.add_argument("--event-type", type=int, default=RACE_EVENT_TYPE,
                   help=f"iRacing event type (default {RACE_EVENT_TYPE}=race)")
    p.add_argument("--max-sessions", type=int, default=None,
                   help="Cap the number of subsessions fetched (for a quick sample)")
    p.add_argument("--top", type=int, default=None,
                   help="Show only the top N cars (default: all)")
    p.add_argument("--verbose", "-v", action="count", default=0,
                   help="Verbose logging: -v for INFO, -vv for DEBUG")
    return p.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    if args.verbose >= 2:
        level = logging.DEBUG
    elif args.verbose == 1:
        level = logging.INFO
    else:
        level = logging.WARNING
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    # Keep our own logs at the chosen level, but silence chatty third-party
    # libraries so -vv shows car_usage logs instead of every HTTP request.
    logger.setLevel(level)
    for noisy in ("urllib3", "iracingdataapi", "requests", "botocore", "boto3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    logger.debug("Args: %s", vars(args))
    logger.debug("Cache dir: %s", CACHE_DIR)

    start = time.monotonic()
    try:
        client = get_client()

        series_id, series_name = resolve_series(client, args.series, args.series_id)
        season_year, season_quarter, race_week_num, track_name = resolve_season_and_week(
            client, series_id, args.week, args.year, args.quarter
        )
        track_suffix = f" — {track_name}" if track_name else ""
        print(
            f"Series: {series_name} (id {series_id}) | "
            f"{season_year}s{season_quarter} week {race_week_num + 1}"
            f"{track_suffix} | class {args.car_class}",
            file=sys.stderr,
        )

        subsessions = find_week_subsessions(
            client, series_id, season_year, season_quarter,
            race_week_num, args.event_type,
        )
        if args.max_sessions is not None:
            subsessions = subsessions[: args.max_sessions]
        print(f"Found {len(subsessions)} race subsessions for the week.",
              file=sys.stderr)

        results = []
        cache_hits = 0
        for i, sid in enumerate(subsessions, 1):
            cached = (RESULTS_CACHE_DIR / f"{sid}.json").exists()
            cache_hits += int(cached)
            results.append(fetch_result(client, sid))
            if i % 25 == 0 or i == len(subsessions):
                print(f"  ...processed {i}/{len(subsessions)}", file=sys.stderr)
            if not cached:
                time.sleep(THROTTLE_SECONDS)
        logger.info("Fetched %d subsessions (%d from cache, %d from API)",
                    len(results), cache_hits, len(results) - cache_hits)

    except IRacingAPIError as e:
        logger.debug("IRacingAPIError during run", exc_info=True)
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        # Surface unexpected failures with a full traceback under -vv for debugging.
        logger.debug("Unexpected error during run", exc_info=True)
        print(f"Unexpected error: {e}\nRe-run with -vv for a full traceback.",
              file=sys.stderr)
        return 1

    # No --class given: rank the most-used car in every class.
    if args.car_class is None:
        by_class = count_cars_by_class(results)
        if not by_class:
            print("\nNo race entries found.", file=sys.stderr)
            return 1
        total = sum(sum(c.values()) for c in by_class.values())
        print(f"\nMost-used cars by class in {series_name} "
              f"(week {race_week_num + 1}{track_suffix}, {len(results)} subsessions, "
              f"{total} race entries):\n")
        print(format_all_classes(by_class, args.top))
        logger.info("Done in %.1fs", time.monotonic() - start)
        return 0

    counts = count_cars(results, args.car_class)
    if not counts:
        print(f"\nNo entries found for class {args.car_class!r}.", file=sys.stderr)
        hints = class_short_names(results)
        if hints:
            print("Class short-names seen this week (use one with --class):",
                  file=sys.stderr)
            for name, n in hints.most_common():
                print(f"  {name}  ({n} entries)", file=sys.stderr)
        return 1

    total = sum(counts.values())
    print(f"\nMost-used cars in {series_name} — class {args.car_class} "
          f"(week {race_week_num + 1}{track_suffix}, {len(results)} subsessions, "
          f"{total} race entries):\n")
    print(format_ranking(counts, args.top))
    logger.info("Done in %.1fs", time.monotonic() - start)
    return 0


if __name__ == "__main__":
    sys.exit(main())
