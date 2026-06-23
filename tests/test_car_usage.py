"""Tests for the pure logic in car_usage.py.

These don't touch the network or OAuth — they exercise the tally + series-resolution
helpers against fixture dicts shaped like the iRacing Data API responses.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from car_usage import (  # noqa: E402
    ROW_FIELDS,
    _to_dict,
    _normalize_series_name,
    _track_for_week,
    _track_from_results,
    build_rows,
    class_short_names,
    count_cars,
    count_cars_by_class,
    format_all_classes,
    format_csv,
    format_json,
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


# --- _to_dict JSON safety -----------------------------------------------------

class _FakeModel:
    """Stub pydantic-style model: mode='json' yields JSON-safe data, default doesn't."""
    def model_dump(self, mode=None):
        if mode == "json":
            return {"at": "2026-06-23T00:00:00Z"}  # serialized
        return {"at": object()}  # non-serializable, like a datetime


def test_to_dict_uses_json_mode_so_result_is_serializable():
    import json
    out = _to_dict(_FakeModel())
    assert out == {"at": "2026-06-23T00:00:00Z"}
    json.dumps(out)  # must not raise


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


# --- track from results (ground truth for any season) -------------------------

def test_track_from_results_picks_most_common_with_config():
    results = [
        {"track": {"track_name": "Spa", "config_name": "Grand Prix"}},
        {"track": {"track_name": "Spa", "config_name": "Grand Prix"}},
        {"track": {"track_name": "Monza", "config_name": "GP"}},
    ]
    assert _track_from_results(results) == "Spa - Grand Prix"


def test_track_from_results_without_config():
    results = [{"track": {"track_name": "Road Atlanta", "config_name": ""}}]
    assert _track_from_results(results) == "Road Atlanta"


def test_track_from_results_none_when_no_track():
    assert _track_from_results([{}, {"track": {}}]) is None
    assert _track_from_results([]) is None


# --- export (build_rows / csv / json) -----------------------------------------

_CONTEXT = {
    "series_id": 447, "series_name": "IMSA iRacing Series",
    "season_year": 2026, "season_quarter": 3, "week": 1,
    "track": "Daytona - Road",
}


def _two_class_by_class():
    return {
        "IMSA23": __import__("collections").Counter(
            {"Ferrari 296 GT3": 3, "BMW M4 GT3": 2}),
        "IMSAP": __import__("collections").Counter({"Porsche 963 GTP": 1}),
    }


def test_build_rows_shape_ordering_and_share():
    rows = build_rows(_two_class_by_class(), _CONTEXT, top=None)
    # Larger class (IMSA23, 5 entries) comes before IMSAP (1).
    assert [r["car_class"] for r in rows] == ["IMSA23", "IMSA23", "IMSAP"]
    # Ranks reset per class; share is within-class.
    first = rows[0]
    assert first["rank"] == 1 and first["car"] == "Ferrari 296 GT3"
    assert first["entries"] == 3 and first["share_pct"] == 60.0
    assert first["series_id"] == 447 and first["track"] == "Daytona - Road"
    assert first["week"] == 1
    # Every row carries exactly the declared columns.
    assert set(first) == set(ROW_FIELDS)


def test_build_rows_respects_top():
    rows = build_rows(_two_class_by_class(), _CONTEXT, top=1)
    # Top 1 per class -> one row each.
    assert [r["car"] for r in rows] == ["Ferrari 296 GT3", "Porsche 963 GTP"]


def test_format_csv_has_header_and_rows():
    rows = build_rows(_two_class_by_class(), _CONTEXT, top=None)
    out = format_csv(rows)
    lines = out.splitlines()
    assert lines[0] == ",".join(ROW_FIELDS)
    assert len(lines) == 1 + len(rows)
    assert "Ferrari 296 GT3" in out


def test_format_json_round_trips():
    import json
    rows = build_rows(_two_class_by_class(), _CONTEXT, top=None)
    assert json.loads(format_json(rows)) == rows


def test_format_csv_empty_is_header_only():
    assert format_csv([]).splitlines() == [",".join(ROW_FIELDS)]


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


def test_normalize_series_name_strips_punctuation_and_case():
    assert _normalize_series_name("Formula C - Dallara F3 Series") == \
        "formula c dallara f3 series"
    assert _normalize_series_name("  IMSA  iRacing   Series!! ") == "imsa iracing series"
    assert _normalize_series_name("") == ""


# Non-Fixed API name uses different punctuation/spacing than the UI title; the
# normalized query still matches it exactly and must win over the "- Fixed" one.
_DALLARA = [
    {"series_id": 456, "series_name": "Formula C - Dallara F3 Series - Fixed"},
    {"series_id": 123, "series_name": "Formula C  Dallara F3 Series"},
]


def test_normalized_exact_beats_fixed_variant():
    sid, _ = resolve_series(_FakeClient(_DALLARA), "Formula C - Dallara F3 Series", None)
    assert sid == 123


def test_substring_prefers_shortest_name():
    # A bare query that substring-matches both: the base (shorter) name wins.
    sid, _ = resolve_series(_FakeClient(_DALLARA), "Dallara", None)
    assert sid == 123


if __name__ == "__main__":
    # Allow running without pytest installed.
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")
