#!/usr/bin/env python3
"""Standalone tool: iRacing iRating distribution + percentile lookup.

Pulls the full per-category driver list (iRacing's `driver_stats_by_category`
endpoint — every active driver with their current iRating in a category), caches
it in a local SQLite database, and answers questions like "what top %% is 4000
iRating?" against the cached data — no API call needed after the first fetch.

The SQLite store is chosen over CSV because the dataset is large (~500k drivers
*per* road category, a few million rows across all six) and the useful queries
are percentile/count lookups that an indexed table answers in milliseconds.

Data source caveat: the distribution is over drivers who have *participated* in
that category (that's what the endpoint returns), which is the same basis the
community "iRating percentile" charts use. It is not the entire member base.

This tool reuses the package's OAuth client (iracing_sr_optimizer.iracing_api)
so credentials come from the same environment variables as the rest of the repo:
IRACING_EMAIL, IRACING_PASSWORD, IRACING_CLIENT_ID, IRACING_CLIENT_SECRET.

Usage:
    # First run fetches + caches the categories it needs, then answers:
    python irating_stats.py --target 4000

    # Refresh the cached data (do this when you want fresh numbers):
    python irating_stats.py --update --all
    python irating_stats.py --update --categories sports_car,oval

    # Show what's cached and how old it is:
    python irating_stats.py --status

Run `python irating_stats.py --help` for all options.
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import sqlite3
import sys
import time
from pathlib import Path
from typing import Iterable, Optional

# Reuse the repo's OAuth/client. The package client returns plain dicts (no
# pydantic wrapping), which is what we want for bulk rows — wrapping 500k rows in
# a pydantic model would be needlessly slow and memory-hungry.
from iracing_sr_optimizer.iracing_api import IRacingAPIError, get_client

logger = logging.getLogger("irating_stats")

# category_id -> (endpoint slug used on the CLI, human label). Ids match the
# iRacing Data API: 1 Oval, 2 Road (legacy, now split), 3 Dirt Oval,
# 4 Dirt Road, 5 Sports Car, 6 Formula Car. We expose the five current
# categories; "road" (2) is legacy and omitted from the default set.
CATEGORIES: dict[int, tuple[str, str]] = {
    1: ("oval", "Oval"),
    5: ("sports_car", "Sports Car"),
    6: ("formula_car", "Formula Car"),
    3: ("dirt_oval", "Dirt Oval"),
    4: ("dirt_road", "Dirt Road"),
}
SLUG_TO_ID = {slug: cid for cid, (slug, _) in CATEGORIES.items()}
DEFAULT_TARGET = 4000

DB_PATH = Path.home() / ".iracing_sr_cache" / "driver_stats.db"

# Consider cached data "stale" past this age (used to nudge on --status and to
# decide whether an auto-fetch is needed when answering a query).
DEFAULT_MAX_AGE_DAYS = 7


# --- Parsing (pure, unit-testable) -------------------------------------------

def _parse_int(value) -> Optional[int]:
    """Best-effort int parse of an API field (they arrive as strings)."""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _parse_float(value) -> Optional[float]:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def row_to_record(category_id: int, row: dict) -> Optional[tuple]:
    """Convert one API driver row into a DB tuple, or None to skip it.

    Rows with no parseable cust_id are dropped (nothing to key on). iRating is
    kept as-is (including <=0) so the store is faithful; percentile queries
    filter to irating > 0 themselves.
    """
    cust_id = _parse_int(row.get("custid"))
    if cust_id is None:
        return None
    return (
        category_id,
        cust_id,
        row.get("driver"),
        row.get("location"),
        row.get("club_name"),
        _parse_int(row.get("starts")),
        _parse_int(row.get("wins")),
        _parse_int(row.get("irating")),
        _parse_int(row.get("ttrating")),
        row.get("class"),          # license + SR, e.g. "P 4.26"
        _parse_float(row.get("avg_inc")),
    )


def rows_to_records(category_id: int, rows: Iterable[dict]) -> list[tuple]:
    """Map API rows to DB tuples, dropping unparseable ones."""
    out = []
    for row in rows:
        rec = row_to_record(category_id, row)
        if rec is not None:
            out.append(rec)
    return out


# --- Database ----------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS driver_stats (
    category_id   INTEGER NOT NULL,
    cust_id       INTEGER NOT NULL,
    driver        TEXT,
    location      TEXT,
    club_name     TEXT,
    starts        INTEGER,
    wins          INTEGER,
    irating       INTEGER,
    ttrating      INTEGER,
    license_class TEXT,
    avg_inc       REAL,
    PRIMARY KEY (category_id, cust_id)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_cat_irating ON driver_stats (category_id, irating);

CREATE TABLE IF NOT EXISTS category_meta (
    category_id  INTEGER PRIMARY KEY,
    name         TEXT,
    driver_count INTEGER,
    updated_at   TEXT
);
"""


def connect(db_path: Path = DB_PATH) -> sqlite3.Connection:
    """Open (creating if needed) the SQLite store and ensure the schema exists."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.executescript(_SCHEMA)
    return conn


def replace_category(conn: sqlite3.Connection, category_id: int,
                     records: list[tuple], updated_at: Optional[str] = None) -> int:
    """Atomically replace all cached rows for a category with `records`.

    Returns the number of rows written. Delete+insert (rather than upsert) so a
    driver who dropped out of the category doesn't linger as a stale row.
    """
    updated_at = updated_at or dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    with conn:  # single transaction: either the whole category swaps or none of it
        conn.execute("DELETE FROM driver_stats WHERE category_id = ?", (category_id,))
        conn.executemany(
            "INSERT INTO driver_stats (category_id, cust_id, driver, location, "
            "club_name, starts, wins, irating, ttrating, license_class, avg_inc) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            records,
        )
        name = CATEGORIES.get(category_id, ("", str(category_id)))[1]
        conn.execute(
            "INSERT OR REPLACE INTO category_meta "
            "(category_id, name, driver_count, updated_at) VALUES (?,?,?,?)",
            (category_id, name, len(records), updated_at),
        )
    return len(records)


def update_category(conn: sqlite3.Connection, client, category_id: int) -> int:
    """Fetch a category's driver list from the API and cache it. Returns row count."""
    slug, label = CATEGORIES[category_id]
    logger.info("Fetching %s driver list from API...", label)
    t0 = time.monotonic()
    rows = client.driver_list(category_id)
    records = rows_to_records(category_id, rows)
    n = replace_category(conn, category_id, records)
    logger.info("Cached %d %s drivers in %.1fs", n, label, time.monotonic() - t0)
    return n


def category_count(conn: sqlite3.Connection, category_id: int) -> int:
    """How many rows are cached for a category (0 if none)."""
    row = conn.execute(
        "SELECT driver_count FROM category_meta WHERE category_id = ?", (category_id,)
    ).fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def category_updated_at(conn: sqlite3.Connection, category_id: int) -> Optional[str]:
    row = conn.execute(
        "SELECT updated_at FROM category_meta WHERE category_id = ?", (category_id,)
    ).fetchone()
    return row[0] if row else None


def _age_days(iso_ts: Optional[str]) -> Optional[float]:
    """Age in days of an ISO timestamp, or None if unparseable/missing."""
    if not iso_ts:
        return None
    try:
        ts = dt.datetime.fromisoformat(iso_ts)
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=dt.timezone.utc)
    return (dt.datetime.now(dt.timezone.utc) - ts).total_seconds() / 86400.0


# --- Queries -----------------------------------------------------------------

def percentile_of(conn: sqlite3.Connection, category_id: int, target: int) -> Optional[dict]:
    """Where an iRating of `target` falls in a category's cached distribution.

    Returns a dict with total drivers and the share below / at-or-above `target`,
    plus the "top %%" (share at or above). None if the category has no data.
    Only drivers with irating > 0 are counted.
    """
    total = conn.execute(
        "SELECT COUNT(*) FROM driver_stats WHERE category_id = ? AND irating > 0",
        (category_id,),
    ).fetchone()[0]
    if not total:
        return None
    below = conn.execute(
        "SELECT COUNT(*) FROM driver_stats WHERE category_id = ? AND irating > 0 "
        "AND irating < ?",
        (category_id, target),
    ).fetchone()[0]
    at_or_above = total - below
    return {
        "total": total,
        "target": target,
        "below": below,
        "at_or_above": at_or_above,
        "pct_below": below / total * 100.0,
        "top_pct": at_or_above / total * 100.0,
    }


def _quantile(conn: sqlite3.Connection, category_id: int, total: int, p: float) -> Optional[int]:
    """The iRating at percentile `p` (0-100) via an indexed OFFSET scan."""
    offset = min(total - 1, max(0, int(round(p / 100.0 * total))))
    row = conn.execute(
        "SELECT irating FROM driver_stats WHERE category_id = ? AND irating > 0 "
        "ORDER BY irating LIMIT 1 OFFSET ?",
        (category_id, offset),
    ).fetchone()
    return int(row[0]) if row else None


def distribution_summary(conn: sqlite3.Connection, category_id: int) -> Optional[dict]:
    """Percentile breakdown (p10..p99), min/median/max, and count for a category."""
    total = conn.execute(
        "SELECT COUNT(*) FROM driver_stats WHERE category_id = ? AND irating > 0",
        (category_id,),
    ).fetchone()[0]
    if not total:
        return None
    lo, hi = conn.execute(
        "SELECT MIN(irating), MAX(irating) FROM driver_stats "
        "WHERE category_id = ? AND irating > 0",
        (category_id,),
    ).fetchone()
    pcts = [10, 25, 50, 75, 90, 95, 99]
    return {
        "total": total,
        "min": lo,
        "max": hi,
        "percentiles": {p: _quantile(conn, category_id, total, p) for p in pcts},
    }


# --- Formatting --------------------------------------------------------------

def format_summary(label: str, summary: dict, pct: Optional[dict]) -> str:
    lines = [f"=== {label} ==="]
    lines.append(f"active drivers : {summary['total']:,}")
    p = summary["percentiles"]
    lines.append(f"iRating range  : {summary['min']:,} - {summary['max']:,}  "
                 f"(median {p[50]:,})")
    lines.append(
        "percentiles    : "
        f"p10 {p[10]:,}   p25 {p[25]:,}   p50 {p[50]:,}   "
        f"p75 {p[75]:,}   p90 {p[90]:,}   p95 {p[95]:,}   p99 {p[99]:,}"
    )
    if pct:
        lines.append(
            f"-> {pct['target']:,} iR is higher than {pct['pct_below']:.1f}% of "
            f"drivers  ==>  TOP {pct['top_pct']:.1f}%  "
            f"({pct['at_or_above']:,} of {pct['total']:,} at or above)"
        )
    return "\n".join(lines)


def format_status(conn: sqlite3.Connection) -> str:
    lines = [f"{'Category':<14} {'Drivers':>10}  {'Updated (UTC)':<20} {'Age':>8}"]
    lines.append("-" * 56)
    any_row = False
    for cid, (_slug, label) in CATEGORIES.items():
        count = category_count(conn, cid)
        if not count:
            continue
        any_row = True
        ts = category_updated_at(conn, cid) or "?"
        age = _age_days(ts)
        age_str = f"{age:.1f}d" if age is not None else "?"
        lines.append(f"{label:<14} {count:>10,}  {ts:<20} {age_str:>8}")
    if not any_row:
        return "No categories cached yet. Run: python irating_stats.py --update --all"
    return "\n".join(lines)


# --- CLI ---------------------------------------------------------------------

def resolve_categories(spec: Optional[str], want_all: bool) -> list[int]:
    """Resolve the --categories / --all selection into a list of category ids."""
    if want_all or not spec:
        return list(CATEGORIES.keys())
    ids = []
    for token in spec.split(","):
        token = token.strip().lower()
        if not token:
            continue
        if token in SLUG_TO_ID:
            ids.append(SLUG_TO_ID[token])
        elif token.isdigit() and int(token) in CATEGORIES:
            ids.append(int(token))
        else:
            raise SystemExit(
                f"Unknown category {token!r}. Choose from: "
                f"{', '.join(SLUG_TO_ID)}"
            )
    return ids


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="irating_stats",
        description="iRacing iRating distribution + percentile lookup, cached in "
                    "SQLite so the API is only hit when you refresh.",
    )
    p.add_argument("--target", type=int, nargs="?", const=DEFAULT_TARGET, default=None,
                   metavar="IRATING",
                   help=f"Show what top %% this iRating is (default {DEFAULT_TARGET} "
                        "if given with no value)")
    p.add_argument("--update", action="store_true",
                   help="Refresh the selected categories from the API before answering")
    p.add_argument("--all", dest="want_all", action="store_true",
                   help="Apply to all categories (default when none specified)")
    p.add_argument("--categories", default=None, metavar="LIST",
                   help="Comma-separated categories to use, e.g. "
                        "'sports_car,oval,formula_car' (default: all)")
    p.add_argument("--status", action="store_true",
                   help="Show what's cached (row counts + age) and exit")
    p.add_argument("--db", type=Path, default=DB_PATH,
                   help=f"SQLite database path (default: {DB_PATH})")
    p.add_argument("--max-age-days", type=float, default=DEFAULT_MAX_AGE_DAYS,
                   help="Auto-fetch a category if its cache is older than this "
                        f"(default {DEFAULT_MAX_AGE_DAYS}); only when not using --update")
    p.add_argument("--verbose", "-v", action="count", default=0,
                   help="Verbose logging: -v for INFO, -vv for DEBUG")
    return p.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    level = logging.DEBUG if args.verbose >= 2 else (
        logging.INFO if args.verbose == 1 else logging.WARNING)
    logging.basicConfig(
        level=level, format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    logger.setLevel(level)
    for noisy in ("urllib3", "iracingdataapi", "requests"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    conn = connect(args.db)

    if args.status:
        print(format_status(conn))
        return 0

    category_ids = resolve_categories(args.categories, args.want_all)
    target = args.target

    # A lazily-created client so a purely-cached query never authenticates.
    _client = {"c": None}

    def client():
        if _client["c"] is None:
            _client["c"] = get_client()
        return _client["c"]

    try:
        for cid in category_ids:
            label = CATEGORIES[cid][1]
            have = category_count(conn, cid)
            age = _age_days(category_updated_at(conn, cid))
            stale = age is None or age > args.max_age_days
            need_fetch = args.update or not have or (stale and target is not None)

            if need_fetch:
                if not args.update and have:
                    print(f"{label}: cache is {age:.1f} days old - refreshing...",
                          file=sys.stderr)
                elif not have and not args.update:
                    print(f"{label}: not cached yet - fetching...", file=sys.stderr)
                update_category(conn, client(), cid)
    except IRacingAPIError as e:
        logger.debug("IRacingAPIError", exc_info=True)
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001 - surface unexpected errors cleanly
        logger.debug("Unexpected error", exc_info=True)
        print(f"Unexpected error: {e}\nRe-run with -vv for a full traceback.",
              file=sys.stderr)
        return 1

    # Report.
    blocks = []
    for cid in category_ids:
        label = CATEGORIES[cid][1]
        summary = distribution_summary(conn, cid)
        if summary is None:
            blocks.append(f"=== {label} ===\n(no data cached)")
            continue
        pct = percentile_of(conn, cid, target) if target is not None else None
        blocks.append(format_summary(label, summary, pct))
    print("\n\n".join(blocks))
    return 0


if __name__ == "__main__":
    sys.exit(main())
