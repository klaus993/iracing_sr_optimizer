#!/usr/bin/env python3
"""Maintenance script: regenerate `race_report.FUEL_CAPACITY_L`.

Fuel tank capacity is the one number `race_report.py` needs that the iRacing Data API
does not expose — the car payload carries weight, hp and BoP ranges but no tank size.
So the table is hardcoded, and this script is how it gets refreshed when iRacing adds
cars or revises a spec.

Source is the iRacing Fandom wiki's spec boxes, read through its MediaWiki `api.php`
endpoint. The *rendered* pages return HTTP 402 to automated clients; the API endpoint
serves the raw wikitext and does not. Page titles are resolved by search first, because
the slugs are inconsistent ("BMW M4 EVO GT3" vs the in-sim "BMW M4 GT3 EVO").

Car ids come from the Data API so the output is keyed the way race_report needs it.
Anything the wiki doesn't yield is reported rather than guessed.

Usage:
    # Refresh the cars currently in the table
    python fetch_fuel_capacities.py

    # Look up specific cars by name (as shown in-sim)
    python fetch_fuel_capacities.py --cars "Porsche 911 GT3 R (992)" "Ferrari 499P"

    # Every GT3 the service knows about
    python fetch_fuel_capacities.py --car-type gt3
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import urllib.parse
import urllib.request
from typing import Optional

import car_usage as cu

logger = logging.getLogger("fetch_fuel_capacities")

WIKI_API = "https://iracing.fandom.com/api.php"
# The wiki blocks the default urllib agent; a browser UA gets the JSON.
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

# "Fuel Capacity: 110.15 liters (29.1 gallons)"
FUEL_RE = re.compile(r"(?i)fuel\s*capacity\s*:?\s*([\d.]+)\s*lit")


def _wiki_api(params: dict) -> dict:
    url = f"{WIKI_API}?{urllib.parse.urlencode(dict(params, format='json'))}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def find_page(name: str) -> Optional[str]:
    """Best-matching wiki page title for a car name, or None."""
    try:
        data = _wiki_api({"action": "query", "list": "search",
                          "srsearch": name, "srlimit": 5})
    except Exception as e:  # noqa: BLE001 - network/HTML errors are all "no page"
        logger.warning("Search failed for %r: %s", name, e)
        return None
    hits = [h["title"] for h in data.get("query", {}).get("search", [])]
    if not hits:
        return None

    def norm(s: str) -> set[str]:
        return set(re.sub(r"[^a-z0-9 ]", " ", s.lower()).split()) - {"evo", "gt3"}

    target = norm(name)
    exact = [h for h in hits if norm(h) == target]
    return (exact or hits)[0]


def fuel_capacity(title: str) -> Optional[float]:
    """Litres from a wiki page's spec box, or None if it doesn't state one."""
    try:
        data = _wiki_api({"action": "query", "prop": "revisions", "rvprop": "content",
                          "rvslots": "main", "titles": title})
    except Exception as e:  # noqa: BLE001
        logger.warning("Fetch failed for %r: %s", title, e)
        return None
    for page in data.get("query", {}).get("pages", {}).values():
        try:
            text = page["revisions"][0]["slots"]["main"]["*"]
        except (KeyError, IndexError):
            continue
        m = FUEL_RE.search(text)
        if m:
            return float(m.group(1))
    return None


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="fetch_fuel_capacities",
        description="Regenerate race_report.FUEL_CAPACITY_L from the iRacing wiki.")
    p.add_argument("--cars", nargs="+", default=None,
                   help="Car names to look up (default: the cars already in the table)")
    p.add_argument("--car-type", default=None,
                   help="Look up every car of this type instead, e.g. gt3, lmp2, gtp")
    p.add_argument("--verbose", "-v", action="count", default=0,
                   help="Verbose logging: -v INFO, -vv DEBUG")
    return p.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    level = (logging.DEBUG if args.verbose >= 2
             else logging.INFO if args.verbose == 1 else logging.WARNING)
    logging.basicConfig(level=level, format="%(levelname)s [%(name)s] %(message)s")
    for noisy in ("urllib3", "iracingdataapi", "requests"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):  # pragma: no cover
            pass

    try:
        client = cu.get_client()
        cars = [cu._to_dict(c) for c in client.cars]
    except cu.IRacingAPIError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    if args.car_type:
        want = args.car_type.strip().lower()
        selected = [c for c in cars
                    if any(str(t.get("car_type")).lower() == want
                           for t in (c.get("car_types") or []))]
    elif args.cars:
        by_name = {str(c.get("car_name")).lower(): c for c in cars}
        selected = []
        for name in args.cars:
            hit = by_name.get(name.lower())
            if hit:
                selected.append(hit)
            else:
                print(f"No car named {name!r} in the API car list", file=sys.stderr)
    else:
        try:
            from race_report import FUEL_CAPACITY_L
        except ImportError:
            print("Could not import race_report; pass --cars or --car-type",
                  file=sys.stderr)
            return 1
        selected = [c for c in cars if c.get("car_id") in FUEL_CAPACITY_L]

    if not selected:
        print("Nothing to look up.", file=sys.stderr)
        return 1

    found, missing = {}, []
    for car in sorted(selected, key=lambda c: c.get("car_id") or 0):
        name, car_id = str(car.get("car_name")), car.get("car_id")
        title = find_page(name)
        litres = fuel_capacity(title) if title else None
        if litres is None:
            missing.append((car_id, name, title))
            print(f"  ?? {car_id:<5} {name:<34} no capacity on the wiki "
                  f"({title or 'no page found'})", file=sys.stderr)
            continue
        found[car_id] = (litres, name)
        print(f"  ok {car_id:<5} {name:<34} {litres:>7.2f} L   [{title}]",
              file=sys.stderr)

    print()
    print("FUEL_CAPACITY_L: dict[int, float] = {")
    for car_id, (litres, name) in sorted(found.items()):
        print(f"    {car_id}: {litres:.2f},".ljust(20) + f"# {name}")
    print("}")
    if missing:
        print(f"\n{len(missing)} car(s) had no stated capacity — add by hand or leave "
              f"out; race_report reports no fuel bound for cars absent from the table.",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
