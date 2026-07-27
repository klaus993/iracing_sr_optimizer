# CLAUDE.md

Guidance for working in this repo. Everything here was learned by hitting it — the
iRacing Data API section in particular is a list of things that silently produce wrong
numbers rather than errors.

## What this repo is

A set of standalone CLI tools over the iRacing Data API, plus one package
(`iracing_sr_optimizer/`) for the original SR-optimizer feature. Each tool is a
self-contained script at the repo root with its own tests in `tests/`.

| Tool | Question it answers |
|------|--------------------|
| `iracing_sr_optimizer/` (`python -m iracing_sr_optimizer`) | Which week/track maximises Safety Rating gain |
| `car_usage.py` | Which car is most *used* in a series this week |
| `car_meta.py` | Which car is actually *good* (usage + win rate + pace), sliceable by your split |
| `irating_stats.py` | iRating distribution and percentile lookup, cached in SQLite |
| `race_report.py` | What the field *did* in one race: stints, pit stops, tyre calls, fuel bounds, weather |

## Conventions

- **`car_usage.py` is the API layer.** It owns OAuth, the rate-limit retry wrapper
  (`_api_call`), `_to_dict`, and the immutable subsession cache (`fetch_result`, cached
  under `~/.iracing_sr_cache/results/`). New tools import it (`import car_usage as cu`)
  rather than re-implementing auth. `iracing_sr_optimizer/iracing_api.py` is the older,
  thinner version — prefer `car_usage`'s.
- **`car_meta.py` owns shared analysis helpers**: `_fmt_lap`, `_lap_seconds`,
  `_iter_sim_rows`, `select_my_split`, `select_top_split`. Reuse via `import car_meta as cm`.
  Cross-module use of underscore-prefixed helpers is the established pattern here.
- **CLI shape**: argparse with `--csv` / `--json` mutually exclusive, `-v/-vv` verbosity,
  logs and progress on **stderr** so stdout stays pipeable. Third-party loggers get
  silenced to WARNING.
- **Tests are pure-function tests against fixture dicts** shaped like API responses. No
  network, no OAuth. Run `python -m pytest -q`.
- **Windows**: consoles are cp1252 and die on driver names like "Pakuła". Any tool that
  prints API text should `sys.stdout.reconfigure(encoding="utf-8", errors="replace")`.

## iRacing Data API gotchas

These are the ones that cost real debugging time.

- **OAuth tokens expire in ~10 minutes.** A long fetch that builds one client and reuses
  it starts failing partway through with `Access token not valid` — and if you catch
  exceptions per-item you get a *silently partial* dataset. One bulk fetch here lost 258
  of 597 subsessions this way. Rebuild the client on auth failure and every N calls; see
  `race_report.RefreshingClient`.
- **Positions are 0-indexed.** `finish_position_in_class: 0` is a **win**. Add 1 for display.
- **`stats_member_recent_races` positions are different again**: 1-indexed and
  *in-class*, while `winner_name` on the same record is the *overall* winner. In a
  multiclass race that combination reads as "finished 2nd behind X" when the driver
  actually won their class.
- **Team/endurance events**: the team entry carries `oldi_rating`/`newi_rating` of `-1`;
  the driver's real values are nested in `driver_results`. Reading the team's field
  silently drops those races from any rating chain.
- **`member_chart_data` is sampled (~weekly), not per-race.** It is the wrong source for
  a peak or any extreme — it reported a career peak of 3070 where the per-race data says
  3167. For extremes, walk `oldi_rating`/`newi_rating` from `client.result(subsession_id)`.
- **`result_search_series` needs every season year queried separately** and does not
  return iRating at all — only the subsession index. Ratings come from the per-subsession
  `result` call.
- **Lap times are ten-thousandths of a second** (`1238364` = 2:03.836). Use
  `car_meta._lap_seconds` / `_fmt_lap`.
- **Pit stops: both the in-lap and the out-lap carry the `pitted` lap event.** Consecutive
  flagged laps are ONE stop — raw flag counts of 1–6 per driver collapse to 1–3 real
  stops. A `pitted` flag on lap 0 means a pit-lane start, not a strategy stop.
- **Series rules live in the schedule week, not the season**: `car_restrictions[]` per
  week carries `max_pct_fuel_fill` (IMSA caps GT3 at 50% in sprints, ~96% in the 6h),
  `power_adjust_pct` and `weight_penalty_kg`. This is the real BoP.
- **There is a weather forecast**: schedule week → `weather.weather_url` is a signed S3
  JSON with hourly rows (`precip_chance`, `precip_amount`, `air_temp`, `affects_session`,
  `time_offset` in minutes from session start). Integer fields are scaled ×100.
- **Not in the API at all**: fuel tank capacity in litres, tyre compound fitted, and
  litres added per stop. Capacities in `race_report.FUEL_CAPACITY_L` were scraped from
  the iRacing Fandom wiki via its `api.php` endpoint (the rendered pages 402 for bots,
  the MediaWiki API does not).

## Analysis pitfalls found the hard way

- **Excluding pit laps matters.** A class-median pace curve that includes pit in/out laps
  spikes exactly where the field stops, and a *dry* race's pit window then reads as rain
  arriving. `race_report.class_median_by_lap` excludes them, and wet onset additionally
  requires the slowdown to persist for two laps.
- **Say what is measured vs inferred.** Stint timing is measured; tyre compound is
  inferred from post-stop pace against the class median and is often inconclusive — in
  one race only 9 of 22 wet-phase stops moved pace enough to call either way. Report
  confidence, not a guess.
- **Fuel is a bound, not an estimate.** Nobody can start above
  `capacity × max_pct_fuel_fill`, so over an opening stint of N laps consumption is
  ≤ usable/N. Underfilling only lowers the true figure, so the inequality holds. Later
  stints have unknown fills and get no bound. A bound far above the class-best one means
  that stint was cut short (repairs, a spin) — weak, not a different consumption.
- **Pit-phase time is not pit-service time.** It bundles transit, service, penalties
  served, repairs, box waiting, and any off-track excursion on the in/out lap. It cannot
  be decomposed into fuel vs tyres.
- **Reconcile and print it.** `race_report.reconcile()` checks that stints tile the race,
  collapsed stops ≤ flagged pit laps, class order has no gaps, and wet-phase lap share
  agrees with the session's `precip_time_pct`. These caught three real bugs.

## Repo state

Branch `claude/awesome-curie-glc6bu` is 12 commits ahead of `main` and tracks
`origin/claude/awesome-curie-glc6bu`. `main` has not been updated since `be4c692`.
