"""Tests for the pure logic in car_usage.py.

These don't touch the network or OAuth — they exercise the tally + series-resolution
helpers against fixture dicts shaped like the iRacing Data API responses.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from car_usage import (  # noqa: E402
    _track_for_week,
    class_short_names,
    count_cars,
    count_cars_by_class,
    format_all_classes,
    resolve_series,
)

# Car class ids used in fixtures.
GT3 = 4011   # short_name IMSA23
GTP = 4074   # short_name IMSAP

CAR_CLASSES = [
    {"car_class_id": GT3, "short_name": "IMSA23", "name": "GT3 Class"},
    {"car_class_id": GTP, "short_name": "IMSAP", "name": "GTP Class"},
]


def _result(race_rows, qualify_rows=None):
    """A subsession result: top-level car_classes + a qualify and a race simsession.

    Rows carry only `car_class_id` (the realistic shape); the short name must be
    resolved via the top-level `car_classes` array.
    """
    sessions = []
    if qualify_rows is not None:
        sessions.append({"simsession_name": "QUALIFY",
                         "simsession_type_name": "Open Qualify",
                         "results": qualify_rows})
    sessions.append({"simsession_name": "RACE",
                     "simsession_type_name": "Race",
                     "results": race_rows})
    return {"car_classes": CAR_CLASSES, "session_results": sessions}


def test_counts_only_target_class_and_ranks_by_entries():
    results = [
        _result([
            {"car_name": "Ferrari 296 GT3", "car_class_id": GT3},
            {"car_name": "Ferrari 296 GT3", "car_class_id": GT3},
            {"car_name": "BMW M4 GT3", "car_class_id": GT3},
            {"car_name": "Porsche 963 GTP", "car_class_id": GTP},  # other class
        ]),
        _result([
            {"car_name": "Ferrari 296 GT3", "car_class_id": GT3},
            {"car_name": "BMW M4 GT3", "car_class_id": GT3},
        ]),
    ]

    counts = count_cars(results, "IMSA23")

    assert counts.most_common()[0] == ("Ferrari 296 GT3", 3)
    assert counts["BMW M4 GT3"] == 2
    assert "Porsche 963 GTP" not in counts
    assert sum(counts.values()) == 5


def test_qualify_session_rows_are_excluded():
    # Same drivers appear in QUALIFY; only RACE entries must be counted.
    results = [_result(
        race_rows=[
            {"car_name": "Ferrari 296 GT3", "car_class_id": GT3},
            {"car_name": "BMW M4 GT3", "car_class_id": GT3},
        ],
        qualify_rows=[
            {"car_name": "Ferrari 296 GT3", "car_class_id": GT3},
            {"car_name": "BMW M4 GT3", "car_class_id": GT3},
            {"car_name": "Audi R8 LMS GT3", "car_class_id": GT3},
        ],
    )]
    counts = count_cars(results, "IMSA23")
    assert sum(counts.values()) == 2  # race only, not the 3 qualify rows
    assert "Audi R8 LMS GT3" not in counts


def test_class_resolved_via_top_level_car_classes_map():
    # Row has no short-name field at all — must be resolved from car_classes by id.
    results = [_result([{"car_name": "Audi R8 LMS GT3", "car_class_id": GT3}])]
    assert count_cars(results, "IMSA23")["Audi R8 LMS GT3"] == 1
    assert count_cars(results, "IMSAP") == {}


def test_class_matching_is_case_insensitive():
    results = [_result([{"car_name": "Ferrari 296 GT3", "car_class_id": GT3}])]
    assert count_cars(results, "imsa23")["Ferrari 296 GT3"] == 1


def test_falls_back_to_row_short_name_when_no_car_classes():
    # No top-level car_classes -> use the per-row short-name field.
    results = [{"session_results": [
        {"simsession_name": "RACE", "results": [
            {"car_name": "McLaren 720S GT3", "car_class_short_name": "IMSA23"},
        ]},
    ]}]
    assert count_cars(results, "IMSA23")["McLaren 720S GT3"] == 1


def test_unknown_class_returns_empty_and_hints_list_available():
    results = [_result([{"car_name": "Ferrari 296 GT3", "car_class_id": GT3}])]
    assert count_cars(results, "NOPE") == {}
    hints = class_short_names(results)
    assert "IMSA23" in hints


def test_missing_car_name_falls_back_to_car_id():
    results = [_result([{"car_id": 173, "car_class_id": GT3}])]
    assert count_cars(results, "IMSA23")["car_id:173"] == 1


# --- per-class breakdown ------------------------------------------------------

def test_count_cars_by_class_groups_and_ranks_within_class():
    results = [_result([
        {"car_name": "Ferrari 296 GT3", "car_class_id": GT3},
        {"car_name": "Ferrari 296 GT3", "car_class_id": GT3},
        {"car_name": "BMW M4 GT3", "car_class_id": GT3},
        {"car_name": "Porsche 963 GTP", "car_class_id": GTP},
    ])]
    by_class = count_cars_by_class(results)

    assert set(by_class) == {"IMSA23", "IMSAP"}
    assert by_class["IMSA23"].most_common()[0] == ("Ferrari 296 GT3", 2)
    assert by_class["IMSA23"]["BMW M4 GT3"] == 1
    assert by_class["IMSAP"]["Porsche 963 GTP"] == 1


def test_count_cars_by_class_buckets_unknown_class():
    results = [{"session_results": [
        {"simsession_name": "RACE", "results": [
            {"car_name": "Mystery Car", "car_class_id": 99999},  # not in car_classes
        ]},
    ]}]
    by_class = count_cars_by_class(results)
    assert by_class["(unknown)"]["Mystery Car"] == 1


def test_format_all_classes_orders_largest_class_first():
    results = [_result([
        {"car_name": "Ferrari 296 GT3", "car_class_id": GT3},
        {"car_name": "BMW M4 GT3", "car_class_id": GT3},
        {"car_name": "Porsche 963 GTP", "car_class_id": GTP},
    ])]
    out = format_all_classes(count_cars_by_class(results), top=None)
    # GT3 has more entries (2) than GTP (1), so its header appears first.
    assert out.index("IMSA23 — 2 entries") < out.index("IMSAP — 1 entries")


# --- track for week -----------------------------------------------------------

_SEASON = {"schedules": [
    {"race_week_num": 0, "track": {"track_name": "Daytona", "config_name": "Road"}},
    {"race_week_num": 1, "track": {"track_name": "Road Atlanta", "config_name": ""}},
]}


def test_track_for_week_with_config():
    assert _track_for_week(_SEASON, 0) == "Daytona - Road"


def test_track_for_week_without_config():
    assert _track_for_week(_SEASON, 1) == "Road Atlanta"


def test_track_for_week_missing_returns_none():
    assert _track_for_week(_SEASON, 9) is None


# --- series resolution --------------------------------------------------------

class _FakeClient:
    def __init__(self, series):
        self._series = series

    def get_series(self):
        return self._series


_SERIES = [
    {"series_id": 539, "series_name": "IMSA iRacing Series - Fixed"},
    {"series_id": 447, "series_name": "IMSA iRacing Series"},
    {"series_id": 285, "series_name": "IMSA Vintage Series"},
]


def test_exact_series_name_wins_over_substring():
    # "IMSA iRacing Series" is a substring of the "- Fixed" name; exact match must win.
    sid, name = resolve_series(_FakeClient(_SERIES), "IMSA iRacing Series", None)
    assert sid == 447
    assert name == "IMSA iRacing Series"


def test_series_id_overrides_name():
    sid, name = resolve_series(_FakeClient(_SERIES), "IMSA Vintage Series", 447)
    assert sid == 447


def test_substring_fallback_when_no_exact_match():
    sid, _ = resolve_series(_FakeClient(_SERIES), "Vintage", None)
    assert sid == 285


if __name__ == "__main__":
    # Allow running without pytest installed.
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")
