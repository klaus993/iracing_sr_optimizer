# iRacing SR Optimizer

Ranks iRacing series by Safety Rating (SR) farming potential. Calculates corners per hour for each series/week combination to find the fastest way to gain SR.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Configuration

iRacing requires OAuth for all API access. The legacy cookie-based `/auth` endpoint was [retired on Dec 9, 2025](https://support.iracing.com/support/solutions/articles/31000177717-2026-season-1-initial-release-notes-2025-12-08-03-) with the 2026 Season 1 release. Username/password login no longer works for any account, regardless of 2FA status. See [iRacing's legacy auth notice](https://support.iracing.com/support/solutions/articles/31000173894-enabling-or-disabling-legacy-read-only-authentication) for details.

This tool uses the [`password_limited` OAuth grant](https://oauth.iracing.com/oauth2/book/password_limited_flow.html), which is designed for headless/script use.

1. Register for OAuth client credentials at [oauth.iracing.com](https://oauth.iracing.com/oauth2/book/client_registration.html) — email iRacing from your account email, takes up to 10 days
2. Set these env vars:

```bash
export IRACING_EMAIL="your@email.com"
export IRACING_PASSWORD="your_password"
export IRACING_CLIENT_ID="your_client_id"
export IRACING_CLIENT_SECRET="your_client_secret"
```

## Usage

### Fetch schedule data from iRacing API

First run (or to refresh data):

```bash
python -m iracing_sr_optimizer --fetch
```

This authenticates with iRacing, downloads all current season schedules, track data, and series metadata, then saves everything to `data/schedule_data.json`.

### Analyze a specific week

```bash
# Week 8, all categories
python -m iracing_sr_optimizer --week 8

# With verbose logging
python -m iracing_sr_optimizer --week 8 -v

# Fetch fresh data and analyze in one command
python -m iracing_sr_optimizer --fetch --week 8
```

### Filter by category

```bash
python -m iracing_sr_optimizer --week 8 --category OVAL
python -m iracing_sr_optimizer --week 8 --category DIRT_OVAL
python -m iracing_sr_optimizer --week 8 --category SPORTS_CAR
python -m iracing_sr_optimizer --week 8 --category FORMULA_CAR
python -m iracing_sr_optimizer --week 8 --category DIRT_ROAD
```

### Filter by license class

Show only series accessible at a given license level or below:

```bash
python -m iracing_sr_optimizer --week 8 --license R   # Rookie only
python -m iracing_sr_optimizer --week 8 --license D   # Rookie + D class
python -m iracing_sr_optimizer --week 8 --license C   # R + D + C
```

### Show all weeks

```bash
python -m iracing_sr_optimizer --all-weeks
python -m iracing_sr_optimizer --all-weeks --category OVAL --license D
```

### Output formats

```bash
# Default: terminal table
python -m iracing_sr_optimizer --week 8

# JSON output
python -m iracing_sr_optimizer --week 8 --json

# CSV output
python -m iracing_sr_optimizer --week 8 --csv > results.csv
```

### Skip API track lookup

Use only hardcoded corner counts (no iRacing API call for track data):

```bash
python -m iracing_sr_optimizer --week 8 --no-api
```

## Output

The default output is a terminal table grouped by category (Oval, Sports Car, Formula Car, Dirt Oval, Dirt Road), sorted by farming score within each group. When no category filter is applied, a cross-category top 10 is shown at the end.

Each row shows:

| Column | Description |
|--------|-------------|
| Series | Series name (with heat detail if applicable) |
| Track | Track name for that week |
| CpL | Corners per lap |
| Laps | Effective laps per race (heat + feature for heat racing) |
| Corners | Total corners per race (CpL x Laps) |
| /hr | Corners per hour (Corners x races per hour) |
| Score | Farming score — corners/hr adjusted for incident severity (dirt categories get a 2x bonus since heavy contact is 2x instead of 4x) |

Higher score = faster SR gain (assuming clean racing).

`--json` and `--csv` output the same data with additional fields like `license_class`, `races_per_hour`, `incident_dq`, and `is_heat_racing`.

## Running tests

```bash
source .venv/bin/activate
python -m pytest tests/ -v
```

## How it works

1. **Schedule data** is fetched from the iRacing API (`--fetch`) and cached locally as JSON
2. **Track corner counts** come from the iRacing API (cached in `~/.iracing_sr_cache/`) with hardcoded fallbacks. Tracks are matched by `track_id` first (exact), then by name with fuzzy matching
3. **SR potential** is calculated as corners per lap x laps per race x races per hour, adjusted for incident severity (dirt gets 2x advantage since heavy contact is 2x instead of 4x)
4. Series are ranked by **farming score** (corners per hour adjusted for incident factors)

## Project structure

```
iracing_sr_optimizer/
  __init__.py
  __main__.py          # python -m entry point
  main.py              # CLI argument parsing and orchestration
  config.py            # Paths, credentials, constants
  iracing_api.py       # iRacing API client (OAuth password_limited)
  fetch_schedule.py    # Fetches seasons/schedules/tracks -> schedule_data.json
  models.py            # Series, WeekSchedule, SRPotential dataclasses
  sr_calculator.py     # SR scoring engine
  track_data.py        # Corner count lookup (track_id, name, fuzzy match)
  output_formatter.py  # Table, JSON, CSV output
data/
  schedule_data.json   # Cached schedule (generated by --fetch)
tests/
  test_track_data.py   # Fuzzy matching edge cases
  test_sr_calculator.py # SR calculation for lap/time/heat races
  test_fetch_schedule.py # Category mapping, frequency calc, parsing
```

## Most-used car tool (`car_usage.py`)

A separate, self-contained script that answers a different question: **which car is most used in a series this week?** It aggregates the week's official race results and counts **race entries** per car — one tally per driver who started a race, summed across every split/session. Qualifying and practice are excluded.

It uses the same OAuth login and the same four `IRACING_*` environment variables as the main tool (see [Configuration](#configuration)).

### Usage

```bash
# Default: IMSA iRacing Series, current week, most-used car per class
python car_usage.py

# List series (id + name) to find the one you want; optionally filter by name
python car_usage.py --list-series
python car_usage.py --list-series dallara

# Pick a series by exact id (most reliable) or by name
python car_usage.py --series-id 447
python car_usage.py --series "iRacing GT3 Regional Tour - Americas"

# Restrict to one car class (omit to break down every class)
python car_usage.py --series-id 447 --class IMSA23

# A specific week instead of the current one
python car_usage.py --series-id 447 --week 8

# Machine-readable output (logs/progress stay on stderr, so stdout stays clean)
python car_usage.py --series-id 447 --csv  > usage.csv
python car_usage.py --series-id 447 --json > usage.json

# Top N cars per class; verbose logging (-v info, -vv debug)
python car_usage.py --series-id 447 --top 5 -v
```

Series names are matched ignoring punctuation/spacing/case, and a base series (e.g. `… Series`) is preferred over its `… Series - Fixed` variant. If a query still matches more than one series, the tool prints the candidates and uses the shortest match — run `--list-series` (or `-v`) to see the ids and pass `--series-id` for an exact pick. If `--class` matches nothing, the tool lists the class short-names it actually saw that week so you can correct it.

### Output

The default is a human-readable table per class (cars ranked by entries, with within-class share), headed by the series, week, and the track raced that week. `--csv` / `--json` emit one row per car with these columns:

| Column | Meaning |
|--------|---------|
| `series_id`, `series_name` | The resolved series |
| `season_year`, `season_quarter`, `week` | Season and 1-indexed race week |
| `track` | Track raced that week |
| `car_class` | Car class short name (e.g. `IMSA23`) |
| `rank`, `car`, `entries` | Rank within the class, car name, race-entry count |
| `share_pct` | Entries as a percentage of that class |

### Caching & performance

Subsession results are immutable once final, so each is cached under `~/.iracing_sr_cache/results/`. The **first** run of a busy week can be hundreds of API calls (a few minutes, throttled and rate-limit-aware); **re-runs are near-instant** from cache. Use `--max-sessions N` to sample a subset for a quick check.

## Meta car tool (`car_meta.py`)

Where `car_usage.py` answers "which car is most *used*", this answers "which car is actually *good*". For each car in a class it combines **usage share** with **win rate**, **average finish**, and **raw pace** — the fastest qualifying lap (clean, low-fuel), a robust 5th‑percentile qual lap, and the gap to the fastest car. Crucially, it can slice those metrics by the split you'd actually race in, so the picture reflects your competition rather than the aliens.

It reuses `car_usage.py` for auth, caching, and series/season resolution, so it needs the same four `IRACING_*` environment variables and shares the same results cache.

### Scopes

Pass one or more of these to `--scope` (default `all`):

| Scope | What it analyzes |
|-------|------------------|
| `all` | Every split of the week (the whole field) |
| `top` | Only split 1 — the highest‑SoF split per race slot (the aliens). `--slots N` keeps the top N per slot |
| `mine` | Only the split your `--irating` would be placed in, each slot (needs `--irating`) |
| `tiers` | Breaks the meta down across **every** iRating tier (split 1, split 2, …), so you can see how the pick shifts by skill level |

Passing `mine` without `--irating` falls back to `tiers` (i.e. "show me all iRatings").

### Usage

```bash
# Meta for the open IMSA GT3 class this week, whole field
python car_meta.py --series-id 447 --class IMSA23

# The aliens' pick vs. yours, side by side (447=IMSA open, 539=IMSA fixed)
python car_meta.py --series-id 447 --class IMSA23 --week 5 --scope top mine --irating 2652

# How the meta shifts across every iRating tier
python car_meta.py --series-id 447 --class IMSA23 --week 5 --scope tiers

# Everything at once, machine-readable (one row per car per scope/tier)
python car_meta.py --series-id 447 --class IMSA23 --week 5 \
    --scope all top mine --irating 2652 --csv > meta.csv
```

### Output

Human‑readable tables per scope (and per class), each car showing entries, usage %, wins, win %, podium %, average finish, and best / p5 qualifying lap with the gap to the fastest car. The `mine` and `tiers` headings report the split's SoF and iRating band so you can see exactly which field the numbers describe. `--csv` / `--json` emit one row per car with a `scope` column (`all`, `top`, `mine`, or `tier1`, `tier2`, …); lap times are in seconds.

The same caching applies — the first run of a live, uncached week fetches and throttles; re-runs are instant.

## Race strategy report (`race_report.py`)

Where `car_meta.py` looks at a whole week, this dissects **one race**: what every car in a
class actually did. Stint structure, pit stops and what they cost, inferred tyre calls,
fuel bounds, the weather timeline, penalties, and how positions moved through it all.

Built for wet and mixed races, where the result is usually decided by *when* people
stopped rather than raw pace. On a dry race the weather and tyre sections report "no
transition detected" and the stint / pit / fuel analysis still stands.

It reuses `car_usage.py` for auth, retry and caching, and `car_meta.py` for lap-time
formatting, so it needs the same four `IRACING_*` environment variables.

### Usage

```bash
# Full report for one class, highlighting a driver with *
python race_report.py --subsession 87466883 --class IMSA23 --cust-id 837530

# Every class in the session
python race_report.py --subsession 87466883

# Machine-readable (logs stay on stderr)
python race_report.py --subsession 87466883 --class IMSA23 --csv  > race.csv
python race_report.py --subsession 87466883 --json > race.json

# Skip the signed weather-forecast fetch; verbose logging
python race_report.py --subsession 87466883 --no-forecast -v
```

### What it can and cannot know

The iRacing Data API does not expose fuel tank capacity, tyre compound, or litres added
per stop, so the tool is explicit about which numbers are measured and which are not:

| Quantity | Source |
|----------|--------|
| Stints, pit stops, lap times, positions | Measured — lap chart + `pitted` lap events |
| Max fuel fill %, BoP, weather forecast | Measured — season schedule `car_restrictions` and `weather_url` |
| Fuel consumption | **Upper bound** — `capacity × max fill ÷ opening-stint laps`. A car cannot start above the cap, so underfilling only lowers the true figure |
| Tyre compound | **Inferred** from post-stop pace vs the class median, reported with a confidence, and called "unclear" when the evidence is thin |
| Litres per stop | **Not derivable** — pit time bundles transit, service, penalties, repairs and any off-track on the in/out lap |

Tank capacities are hardcoded in `FUEL_CAPACITY_L` (from the iRacing wiki); the fill
percentage that multiplies them comes from the API, per car, per week.

### Output

Terminal tables: the class strategy table (grid, stops, stint laps, pit loss, dry/wet
median pace, best lap, incidents, iRating delta), a weather timeline, per-stop tyre
inferences, fuel bounds per car model, the week's BoP, penalties from the event log, and
a **reconciliation block** that re-checks the analysis against the raw data (stints tile
the race, collapsed stops ≤ flagged pit laps, wet-phase share agrees with the session's
`precip_time_pct`). Read that block before trusting the numbers.

One subtlety worth knowing: iRacing flags **both the in-lap and the out-lap** of a pit
stop, so consecutive flagged laps are one stop — raw flag counts of 1–6 per driver
collapse to 1–3 real stops.
