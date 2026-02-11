# Handoff: Empirical SR Data + Personal SR Predictor

## What was done

All code for the feature has been implemented and tests pass (99/99). The following files were created or modified:

### Modified
- `iracing_sr_optimizer/config.py` — added `IRACING_CUST_ID`, `RESULTS_CACHE_DIR`, `SR_ROLLING_WINDOW`
- `iracing_sr_optimizer/models.py` — added `series_id`/`season_id` to `Series`, new dataclasses: `SeriesEmpirical`, `UserSR`, `SRPrediction`, empirical fields on `SRPotential`
- `iracing_sr_optimizer/fetch_schedule.py` — stores `series_id` and `season_id` in output JSON
- `iracing_sr_optimizer/sr_calculator.py` — `rank_series()` accepts optional `empirical_data` and `user_sr`, enriches results
- `iracing_sr_optimizer/output_formatter.py` — conditionally shows `AvgInc`, `AvgSR`, `Pred` columns; always includes empirical fields in JSON/CSV
- `iracing_sr_optimizer/main.py` — added `--fetch-results`, `--cust-id`, `--my-sr` flags with full wiring
- `README.md` — documented new features

### New files
- `iracing_sr_optimizer/fetch_results.py` — fetches past race results, aggregates empirical SR data, caches to `data/results_cache/`
- `iracing_sr_optimizer/sr_predictor.py` — parses `--my-sr`, fetches user SR from API, predicts SR direction
- `tests/test_fetch_results.py` — 6 unit tests for aggregation
- `tests/test_sr_predictor.py` — 11 unit tests for parsing/prediction
- `tests/test_integration.py` — updated fixtures with `series_id`/`season_id`, added `TestEmpiricalEnrichment` (6 tests) and `TestCLIWithSR` (2 tests)

## What remains to fix

### Bug 1: `get_user_sr` crash (ALREADY FIXED in sr_predictor.py)

The `stats_member_recent_races` API returns `{"races": [...], "cust_id": N}` (a dict wrapping a list), not a bare list. The code was trying to iterate the dict directly. This has been fixed — the function now unwraps `recent.get("races", [])`.

Additionally, `new_cpi` is NOT available in the recent races response. It's only in subsession `result()` data. The fix fetches CPI from the latest subsession by calling `client.result(subsession_id)` and finding the driver by `cust_id`.

### Bug 2: `fetch_results` returns 0/144 series — schedule data needs re-fetch

The existing `data/schedule_data.json` was generated before `series_id`/`season_id` were added to `fetch_schedule.py`. All 144 series have `season_id: None`, so `fetch_results` skips them all.

**Fix:** User needs to run `python -m iracing_sr_optimizer --fetch` to regenerate schedule data with the new fields. Then `--fetch-results` will work.

### Bug 3 (potential): `result_season_results` API response format

The `fetch_results.py` code at line ~82 does:
```python
season_results = client.result_season_results(season_id, event_type=5, race_week_num=api_week)
season_data = _to_dict(season_results) if not isinstance(season_results, dict) else season_results
results_list = season_data.get("results_list", [])
```

This may have the same dict-wrapping issue. **Needs live testing.** If `result_season_results` returns a dict, the key containing subsession IDs might not be `"results_list"`. Debug by running:

```python
from iracing_sr_optimizer.iracing_api import get_client
client = get_client()
# Use a known season_id from re-fetched schedule
result = client.result_season_results(SEASON_ID, event_type=5, race_week_num=7)  # week 8, 0-indexed
print(type(result))
if isinstance(result, dict):
    print(list(result.keys()))
```

Check what keys it has and adjust `fetch_results.py` accordingly. The subsession entries should have a `subsession_id` field.

### Bug 4 (potential): `result()` API response format for driver data

In `fetch_results.py` at line ~100, the code expects:
```python
sub_dict.get("session_results", [])  # list of sessions
  -> session_result.get("simsession_type") == 6  # race session
    -> session_result.get("results", [])  # list of drivers
      -> driver.get("incidents"), driver.get("old_sub_level"), etc.
```

This was confirmed working via the debug session above — the `result()` API returns a dict with `session_results`, and race sessions have `simsession_type: 6` with `results` containing per-driver data including `incidents`, `old_sub_level`, `new_sub_level`, `old_cpi`, `new_cpi`, `laps_complete`, `cust_id`.

The `corners_per_lap` field is at the top level of the subsession result dict.

## How to test end-to-end

```bash
# 1. Re-fetch schedule (adds series_id/season_id)
python -m iracing_sr_optimizer --fetch

# 2. Verify series_id/season_id are populated
python -c "
import json
with open('data/schedule_data.json') as f:
    data = json.load(f)
has = sum(1 for s in data['series'] if s.get('season_id'))
print(f'{has}/{len(data[\"series\"])} series have season_id')
"

# 3. Fetch empirical results for week 8
python -m iracing_sr_optimizer --fetch-results --week 8

# 4. View enriched output
python -m iracing_sr_optimizer --week 8 --no-api

# 5. With personal SR prediction
python -m iracing_sr_optimizer --week 8 --no-api --my-sr C3.45
python -m iracing_sr_optimizer --week 8 --cust-id 837530

# 6. JSON output should include new fields
python -m iracing_sr_optimizer --week 8 --json --no-api

# 7. All tests pass
python -m pytest tests/ -v
```

## Key API response formats (confirmed via live testing)

### `stats_member_recent_races(cust_id=N)`
```json
{
  "races": [
    {
      "season_id": 5922,
      "series_id": 476,
      "series_name": "...",
      "license_level": 14,
      "subsession_id": 83454348,
      "old_sub_level": 257,
      "new_sub_level": 248,
      "incidents": 9,
      "laps": 15,
      "track": {"track_id": 192, "track_name": "..."},
      "race_week_num": 8
    }
  ],
  "cust_id": 837530
}
```
Note: NO `new_cpi`/`old_cpi` fields here. CPI must be fetched from `result(subsession_id)`.

### `result(subsession_id)`
```json
{
  "subsession_id": 83454348,
  "corners_per_lap": 12,
  "session_results": [
    {
      "simsession_type": 6,
      "results": [
        {
          "cust_id": 837530,
          "incidents": 9,
          "laps_complete": 15,
          "old_sub_level": 257,
          "new_sub_level": 248,
          "old_cpi": 36.10,
          "new_cpi": 38.32
        }
      ]
    }
  ]
}
```

### `result_season_results(season_id, event_type=5, race_week_num=N)` — NOT YET CONFIRMED
Assumed to return something with a `results_list` key containing entries with `subsession_id`. Needs verification.
