"""Tests for the pure logic in race_report.py.

No network/OAuth — these exercise stint reconstruction, pit-stop collapsing, pit loss,
the wet-transition detector, tyre inference, fuel bounds and the weather-offset mapping
against fixture dicts shaped like the iRacing Data API responses.

The collapse rule is the load-bearing one: iRacing flags both the in-lap and the out-lap
of a pit stop, so laps [13, 14, 16, 17] must read as two stops, not four.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from race_report import (  # noqa: E402
    FUEL_CAPACITY_L,
    _car_number_from_message,
    _fmt,
    _penalty_kind,
    build_entries,
    build_stints,
    class_median_by_lap,
    clean_lap_baseline,
    collapse_stops,
    detect_wet_phase,
    forecast_in_session,
    fuel_bound,
    fuel_fill_limits,
    infer_tyre_call,
    lap_seconds_map,
    penalty_summary,
    pitted_laps,
    pool_fuel_bounds,
    reconcile,
    stop_time_loss,
)

MCLAREN = 188
FERRARI = 173


def _lap(n, seconds=None, events=None, incident=False, position=None):
    """A lap-chart row. Lap times in the API are ten-thousandths of a second."""
    return {
        "lap_number": n,
        "lap_time": -1 if seconds is None else int(round(seconds * 10000)),
        "lap_events": events or [],
        "incident": incident,
        "lap_position": position,
        "session_time": n * 1250000,
    }


# --- Pit stop detection and collapsing ---------------------------------------

def test_pitted_laps_reads_the_event_flag():
    laps = [_lap(1, 125), _lap(2, 125, ["off track"]), _lap(3, 133, ["pitted"])]
    assert pitted_laps(laps) == [3]


def test_pitted_laps_is_case_insensitive_and_deduped():
    laps = [_lap(1, 125, ["Pitted"]), _lap(1, 125, ["pitted"])]
    assert pitted_laps(laps) == [1]


def test_pitted_laps_ignores_a_lap_zero_flag():
    """Lap 0 means a pit-lane start, not a strategy stop."""
    laps = [_lap(0, None, ["pitted"]), _lap(1, 125), _lap(2, 133, ["pitted"])]
    assert pitted_laps(laps) == [2]


def test_collapse_stops_pairs_in_lap_and_out_lap():
    # The real shape from subsession 87466883: two stops, four flagged laps.
    assert collapse_stops([13, 14, 16, 17]) == [[13, 14], [16, 17]]


def test_collapse_stops_keeps_a_lone_flagged_lap_as_a_stop():
    assert collapse_stops([17]) == [[17]]


def test_collapse_stops_handles_no_stops():
    assert collapse_stops([]) == []


def test_collapse_stops_groups_a_long_run_of_flagged_laps():
    assert collapse_stops([5, 6, 7, 12]) == [[5, 6, 7], [12]]


# --- Stints -------------------------------------------------------------------

def test_build_stints_tiles_the_race_exactly():
    """Every lap belongs to exactly one stint and the counts sum to the last lap."""
    laps = [_lap(n, 125) for n in range(1, 22)]
    stops = [[13, 14], [16, 17]]
    stints = build_stints(laps, stops)
    assert [s["laps"] for s in stints] == [13, 3, 5]
    assert sum(s["laps"] for s in stints) == 21
    assert [(s["first_lap"], s["last_lap"]) for s in stints] == [(1, 13), (14, 16), (17, 21)]
    assert [s["ended_with_stop"] for s in stints] == [True, True, False]


def test_build_stints_with_no_stops_is_one_stint():
    laps = [_lap(n, 125) for n in range(1, 13)]
    stints = build_stints(laps, [])
    assert [s["laps"] for s in stints] == [12]
    assert stints[0]["ended_with_stop"] is False


def test_build_stints_counts_a_final_lap_pit_stop():
    """A driver who pits on the last lap still ends a stint there, with none after."""
    laps = [_lap(n, 125) for n in range(1, 13)]
    stints = build_stints(laps, [[12]])
    assert [s["laps"] for s in stints] == [12]
    assert stints[0]["ended_with_stop"] is True


def test_build_stints_includes_a_final_lap_without_a_valid_time():
    """Retiring in the pits leaves a flagged final lap with no lap time; it still counts."""
    laps = [_lap(n, 125) for n in range(1, 12)] + [_lap(12, None, ["pitted"])]
    stints = build_stints(laps, [[12]])
    assert sum(s["laps"] for s in stints) == 12


def test_build_stints_with_no_racing_laps_is_empty():
    assert build_stints([_lap(0, None)], []) == []


# --- Lap times and pit loss ---------------------------------------------------

def test_lap_seconds_map_skips_invalid_times():
    laps = [_lap(0, None), _lap(1, 125.5), _lap(2, None)]
    assert lap_seconds_map(laps) == {1: 125.5}


def test_clean_lap_baseline_ignores_laps_with_events_or_incidents():
    laps = [_lap(1, 125), _lap(2, 126), _lap(3, 200, ["pitted"]),
            _lap(4, 180, None, True)]
    assert clean_lap_baseline(laps) == 125.5


def test_clean_lap_baseline_is_none_without_clean_laps():
    assert clean_lap_baseline([_lap(1, 200, ["pitted"])]) is None


def test_stop_time_loss_sums_excess_over_the_baseline():
    laps = [_lap(13, 133, ["pitted"]), _lap(14, 165, ["pitted"])]
    # (133-125) + (165-125) = 48
    assert stop_time_loss(laps, [13, 14], 125.0) == 48.0


def test_stop_time_loss_needs_a_baseline():
    laps = [_lap(13, 133, ["pitted"])]
    assert stop_time_loss(laps, [13], None) is None


# --- Class median and the wet transition --------------------------------------

def test_class_median_excludes_pit_laps():
    """Pit laps must not enter the median, or a dry pit window looks like rain."""
    by = {
        1: [_lap(2, 125), _lap(3, 125)],
        2: [_lap(2, 126), _lap(3, 400, ["pitted"])],
        3: [_lap(2, 127), _lap(3, 127)],
    }
    med = class_median_by_lap(by)
    assert med[2] == 126.0
    assert med[3] == 126.0  # the 400s pit lap is gone, not median-ing to 127


def test_class_median_skips_lap_one():
    by = {1: [_lap(1, 200), _lap(2, 125)]}
    assert 1 not in class_median_by_lap(by)


def test_detect_wet_phase_finds_a_sustained_slowdown():
    med = {n: 125.0 for n in range(2, 13)}
    med.update({13: 150.0, 14: 158.0, 15: 152.0})
    wet = detect_wet_phase(med)
    assert wet["onset_lap"] == 13
    assert wet["dry_baseline"] == 125.0
    assert wet["peak_lap"] == 14


def test_detect_wet_phase_ignores_a_single_slow_lap():
    """One slow lap is a spin or a thin sample, not rain — rain does not un-fall."""
    med = {n: 125.0 for n in range(2, 20)}
    med[13] = 145.0
    assert detect_wet_phase(med)["onset_lap"] is None


def test_detect_wet_phase_on_a_dry_race_reports_no_onset():
    med = {n: 125.0 + (n % 3) for n in range(2, 24)}
    wet = detect_wet_phase(med)
    assert wet["onset_lap"] is None
    assert wet["wet_median"] is None


def test_detect_wet_phase_needs_enough_laps():
    assert detect_wet_phase({2: 125.0})["onset_lap"] is None


def test_detect_wet_phase_threshold_is_configurable():
    med = {n: 125.0 for n in range(2, 12)}
    med.update({12: 132.0, 13: 132.0})  # +5.6%, under the 8% default
    assert detect_wet_phase(med)["onset_lap"] is None
    assert detect_wet_phase(med, onset_pct=5.0)["onset_lap"] == 12


# --- Tyre inference -----------------------------------------------------------

def test_infer_tyre_call_reads_a_pace_gain_in_the_wet_as_wets():
    laps = [_lap(11, 130), _lap(12, 132), _lap(13, 140, ["pitted"]),
            _lap(14, 165, ["pitted"]), _lap(15, 145), _lap(16, 146)]
    med = {11: 126.0, 12: 126.0, 15: 155.0, 16: 156.0}
    wet = {"onset_lap": 13}
    out = infer_tyre_call(laps, [13, 14], med, wet)
    assert out["judged_in_wet"] is True
    assert out["label"].startswith("wets")


def test_infer_tyre_call_reads_a_pace_loss_in_the_wet_as_staying_on_drys():
    laps = [_lap(11, 126), _lap(12, 126), _lap(13, 140, ["pitted"]),
            _lap(14, 165, ["pitted"]), _lap(15, 170), _lap(16, 172)]
    med = {11: 126.0, 12: 126.0, 15: 155.0, 16: 156.0}
    out = infer_tyre_call(laps, [13, 14], med, {"onset_lap": 13})
    assert "stayed on drys" in out["label"]


def test_infer_tyre_call_in_the_dry_is_labelled_a_dry_stop():
    laps = [_lap(11, 126), _lap(12, 126), _lap(13, 140, ["pitted"]), _lap(14, 126)]
    med = {11: 126.0, 12: 126.0, 14: 126.0}
    out = infer_tyre_call(laps, [13], med, {"onset_lap": None})
    assert out["judged_in_wet"] is False
    assert "dry stop" in out["label"]


def test_infer_tyre_call_is_unclear_without_laps_after_the_stop():
    laps = [_lap(11, 126), _lap(12, 140, ["pitted"])]
    out = infer_tyre_call(laps, [12], {11: 126.0}, {"onset_lap": 11})
    assert out["label"] == "unclear"
    assert out["confidence"] == "low"


# --- Fuel ---------------------------------------------------------------------

def _week(car_id=MCLAREN, pct=50):
    return {"car_restrictions": [
        {"car_id": car_id, "max_pct_fuel_fill": pct, "power_adjust_pct": -0.75,
         "weight_penalty_kg": 8, "max_dry_tire_sets": 0}]}


def test_fuel_fill_limits_reads_the_week_restrictions():
    limits = fuel_fill_limits(_week())
    assert limits[MCLAREN]["max_pct_fuel_fill"] == 50
    assert limits[MCLAREN]["weight_penalty_kg"] == 8


def test_fuel_fill_limits_of_an_empty_week_is_empty():
    assert fuel_fill_limits({}) == {}


def test_fuel_bound_divides_usable_fuel_by_the_opening_stint():
    stints = [{"laps": 13, "ended_with_stop": True},
              {"laps": 8, "ended_with_stop": False}]
    f = fuel_bound(MCLAREN, fuel_fill_limits(_week()), stints)
    # 110.15 L capacity x 50% = 55.08 usable, over 13 laps
    assert f["usable_l"] == 55.08
    assert f["opening_stint_laps"] == 13
    assert f["l_per_lap_max"] == round(55.08 / 13, 3)
    assert f["opening_stint_ended_with_stop"] is True


def test_fuel_bound_without_a_known_capacity_is_none():
    f = fuel_bound(999999, fuel_fill_limits(_week(car_id=999999)),
                   [{"laps": 10, "ended_with_stop": True}])
    assert f["l_per_lap_max"] is None


def test_fuel_bound_without_stints_is_none():
    assert fuel_bound(MCLAREN, fuel_fill_limits(_week()), [])["l_per_lap_max"] is None


def test_pool_fuel_bounds_takes_the_longest_opening_stint_per_car():
    rows = [
        {"name": "short", "car_name": "McLaren 720S GT3 EVO",
         "fuel": {"l_per_lap_max": 5.5, "usable_l": 55.08, "opening_stint_laps": 10,
                  "opening_stint_ended_with_stop": True}},
        {"name": "long", "car_name": "McLaren 720S GT3 EVO",
         "fuel": {"l_per_lap_max": 4.24, "usable_l": 55.08, "opening_stint_laps": 13,
                  "opening_stint_ended_with_stop": True}},
    ]
    pooled = pool_fuel_bounds(rows)["McLaren 720S GT3 EVO"]
    assert pooled["l_per_lap_max"] == 4.24
    assert pooled["by"] == "long"
    assert pooled["drivers"] == 2


def test_pool_fuel_bounds_ignores_stints_that_ended_at_the_flag():
    """A stint that ended at the chequered flag may have had fuel spare — no bound."""
    rows = [{"name": "x", "car_name": "Ferrari 296 GT3",
             "fuel": {"l_per_lap_max": 3.2, "usable_l": 52.05, "opening_stint_laps": 16,
                      "opening_stint_ended_with_stop": False}}]
    assert pool_fuel_bounds(rows) == {}


def test_capacity_table_covers_the_gt3_field():
    for car_id in (132, 156, 173, 185, 188, 206):
        assert FUEL_CAPACITY_L[car_id] > 90


def test_capacity_table_covers_the_whole_imsa_field():
    """Every car_id in the IMSA week-5 car_restrictions payload, so all three classes
    get a fuel bound rather than just GT3."""
    imsa_field = (128, 132, 133, 156, 159, 168, 169, 170, 173, 174, 184, 185, 188,
                  194, 196, 206)
    missing = [c for c in imsa_field if c not in FUEL_CAPACITY_L]
    assert not missing, f"no capacity for car_ids {missing}"


def test_lmdh_cars_share_one_tank():
    """GTP/LMDh is a spec chassis — a divergence here means a bad wiki scrape."""
    lmdh = [FUEL_CAPACITY_L[c] for c in (159, 168, 170, 174, 196)]
    assert len(set(lmdh)) == 1, f"LMDh capacities disagree: {lmdh}"


# --- Weather forecast mapping -------------------------------------------------

def _forecast_row(offset, precip_chance=0, air_temp=2200, affects=True):
    return {"time_offset": offset, "timestamp": "2026-07-25T14:00:00",
            "precip_chance": precip_chance, "precip_amount": 0,
            "cloud_cover": 400, "air_temp": air_temp, "rel_humidity": 10000,
            "wind_speed": 558, "affects_session": affects}


def test_forecast_in_session_flags_rows_inside_the_session():
    rows = [_forecast_row(-40), _forecast_row(20), _forecast_row(80)]
    out = forecast_in_session(rows, 56.0)
    assert [r["in_session"] for r in out] == [False, True, False]


def test_forecast_in_session_scales_the_integer_fields():
    out = forecast_in_session([_forecast_row(20, precip_chance=2400, air_temp=2230)], 56.0)
    assert out[0]["precip_chance_pct"] == 24.0
    assert out[0]["air_temp_c"] == 22.3
    assert out[0]["rel_humidity_pct"] == 100.0


def test_forecast_in_session_drops_far_away_rows():
    out = forecast_in_session([_forecast_row(-400), _forecast_row(500)], 56.0)
    assert out == []


def test_forecast_in_session_tolerates_missing_offsets():
    assert forecast_in_session([{"precip_chance": 0}], 56.0) == []


# --- Penalties ----------------------------------------------------------------

def test_penalty_summary_splits_penalties_from_lead_changes():
    events = [
        {"description": "Penalty Applied", "lap_number": 14,
         "message": "Penalty: Too many incident points, Drive-through, Lap 13, #35 Kern"},
        {"description": "Lead Change", "lap_number": 15,
         "message": "Lead change: #33 Burgess takes over from #40 Pakula"},
    ]
    out = penalty_summary(events)
    assert len(out["penalties"]) == 1
    assert len(out["lead_changes"]) == 1
    assert out["penalty_kinds"] == {"Too many incident points": 1}
    assert out["penalties"][0]["car_number"] == "35"


def test_penalty_summary_of_an_empty_log():
    out = penalty_summary([])
    assert out["penalties"] == [] and out["penalty_kinds"] == {}


def test_car_number_from_message():
    assert _car_number_from_message("Penalty: Repair, Lap 7, #44 Vladimir") == "44"
    assert _car_number_from_message("no car here") is None
    assert _car_number_from_message(None) is None


def test_penalty_kind_strips_the_prefix_and_detail():
    assert _penalty_kind("Penalty: Speeding in pits, Hold for 15 seconds, Lap 14") \
        == "Speeding in pits"
    assert _penalty_kind("") == "unknown"


# --- Entries and rendering helpers -------------------------------------------

def _result(rows):
    return {"subsession_id": 1, "session_results": [
        {"simsession_number": -1, "results": []},
        {"simsession_number": 0, "results": rows}]}


def test_build_entries_filters_by_class_and_unzeroes_positions():
    result = _result([
        {"cust_id": 1, "display_name": "A", "car_class_short_name": "IMSA23",
         "car_id": MCLAREN, "car_name": "McLaren 720S GT3 EVO",
         "finish_position_in_class": 0, "starting_position_in_class": 4,
         "finish_position": 11, "laps_complete": 21},
        {"cust_id": 2, "display_name": "B", "car_class_short_name": "GTP",
         "car_id": 174, "car_name": "Porsche 963 GTP",
         "finish_position_in_class": 0, "starting_position_in_class": 0,
         "finish_position": 0, "laps_complete": 23},
    ])
    entries = build_entries(result, "IMSA23")
    assert list(entries) == [1]
    # iRacing positions are 0-indexed; the report shows them 1-indexed.
    assert entries[1]["finish_pic"] == 1
    assert entries[1]["start_pic"] == 5
    assert entries[1]["finish_overall"] == 12


def test_build_entries_without_a_class_keeps_everyone():
    result = _result([
        {"cust_id": 1, "car_class_short_name": "IMSA23", "finish_position_in_class": 0},
        {"cust_id": 2, "car_class_short_name": "GTP", "finish_position_in_class": 0},
    ])
    assert set(build_entries(result)) == {1, 2}


def test_fmt_pads_the_missing_value_dash_to_the_field_width():
    """Unpadded dashes shift every following column and break the tables."""
    assert _fmt(None, ">7.1f") == "      —"
    assert _fmt(None, "<9") == "—        "
    assert _fmt(12.34, ">7.1f") == "   12.3"


def test_reconcile_flags_stints_that_do_not_tile():
    report = {
        "drivers": [{"name": "bad", "stint_laps": [5, 5], "last_lap": 12,
                     "stop_count": 1, "pitted_laps": [5], "finish_pic": 1}],
        "wet": {"onset_lap": None, "dry_baseline": 125.0, "onset_pct": 8.0},
        "median_by_lap": {}, "weather": {},
    }
    checks = {c["check"]: c for c in reconcile(report)}
    assert checks["stint laps tile the race (sum == last lap run)"]["ok"] is False


def test_reconcile_passes_on_consistent_data():
    report = {
        "drivers": [{"name": "ok", "stint_laps": [13, 3, 5], "last_lap": 21,
                     "stop_count": 2, "pitted_laps": [13, 14, 16, 17], "finish_pic": 1}],
        "wet": {"onset_lap": None, "dry_baseline": 125.0, "onset_pct": 8.0},
        "median_by_lap": {}, "weather": {},
    }
    assert all(c["ok"] for c in reconcile(report))
