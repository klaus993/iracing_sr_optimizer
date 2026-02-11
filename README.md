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
