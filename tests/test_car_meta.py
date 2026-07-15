"""Tests for the pure logic in car_meta.py.

No network/OAuth — these exercise the pace/win metrics, split selection, and
iRating-tier logic against fixture dicts shaped like the iRacing Data API responses.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from car_meta import (  # noqa: E402
    _fmt_lap,
    _lap_seconds,
    _percentile,
    _row_irating,
    build_export_rows,
    compute_class_metrics,
    export_csv,
    select_by_tier,
    select_my_split,
    select_top_split,
)

GT3 = 4011   # short_name IMSA23
GTP = 4074   # short_name IMSAP

CAR_CLASSES = [
    {"car_class_id": GT3, "short_name": "IMSA23", "name": "GT3 Class"},
    {"car_class_id": GTP, "short_name": "IMSAP", "name": "GTP Class"},
]


def _row(car, cls=GT3, fic=None, bl=None, inc=None, ir=None):
    r = {"car_name": car, "car_class_id": cls}
    if fic is not None:
        r["finish_position_in_class"] = fic
    if bl is not None:
        r["best_lap_time"] = bl
    if inc is not None:
        r["incidents"] = inc
    if ir is not None:
        r["oldi_rating"] = ir
    return r


def _result(sid, start_time, sof, race_rows, qual_rows=None):
    sessions = []
    if qual_rows is not None:
        sessions.append({"simsession_name": "QUALIFY",
                         "simsession_type_name": "Open Qualify",
                         "results": qual_rows})
    sessions.append({"simsession_name": "RACE",
                     "simsession_type_name": "Race",
                     "results": race_rows})
    return {
        "subsession_id": sid,
        "start_time": start_time,
        "event_strength_of_field": sof,
        "car_classes": CAR_CLASSES,
        "session_results": sessions,
    }


# --- lap-time + percentile helpers -------------------------------------------

def test_fmt_lap_and_seconds():
    assert _fmt_lap(905000) == "1:30.500"   # 90.5s
    assert _fmt_lap(1234500) == "2:03.450"
    assert _lap_seconds(905000) == 90.5
    for bad in (0, -1, None):
        assert _fmt_lap(bad) == "—"
        assert _lap_seconds(bad) is None


def test_percentile_nearest_rank():
    assert _percentile([10, 20, 30, 40], 0.5) == 30
    assert _percentile([10, 20, 30, 40], 0.0) == 10
    assert _percentile([], 0.05) is None


def test_row_irating_falls_back_to_driver_results():
    assert _row_irating({"oldi_rating": 2000}) == 2000
    assert _row_irating({"oldi_rating": -1,
                         "driver_results": [{"oldi_rating": 1800}]}) == 1800
    assert _row_irating({}) is None


# --- metric computation -------------------------------------------------------

def test_compute_class_metrics_usage_wins_pace():
    result = _result(
        1, "T1", 3000,
        race_rows=[
            _row("Ferrari 296 GT3", fic=0, bl=1000000, inc=4),
            _row("Ferrari 296 GT3", fic=2, bl=1010000, inc=0),
            _row("BMW M4 GT3", fic=1, bl=1005000, inc=2),
            _row("Porsche 963 GTP", cls=GTP, fic=0, bl=900000, inc=0),  # other class
        ],
        qual_rows=[
            _row("Ferrari 296 GT3", bl=995000),
            _row("Ferrari 296 GT3", bl=990000),
            _row("BMW M4 GT3", bl=998000),
        ],
    )
    by_class = compute_class_metrics([result], "IMSA23")
    assert set(by_class) == {"IMSA23"}
    data = by_class["IMSA23"]
    assert data["n_splits"] == 1
    rows = {r["car"]: r for r in data["rows"]}

    ferrari = rows["Ferrari 296 GT3"]
    assert ferrari["entries"] == 2
    assert ferrari["usage_pct"] == 66.7
    assert ferrari["wins"] == 1
    assert ferrari["win_pct_splits"] == 100.0
    assert ferrari["podium_pct"] == 100.0        # fic 0 and 2 both <= 2
    assert ferrari["avg_finish"] == 2.0          # (1 + 3) / 2
    assert ferrari["avg_inc"] == 2.0             # (4 + 0) / 2
    assert ferrari["best_qual"] == 990000
    assert ferrari["p5_qual"] == 990000
    assert ferrari["best_race"] == 1000000

    bmw = rows["BMW M4 GT3"]
    assert bmw["wins"] == 0 and bmw["usage_pct"] == 33.3
    # rows are sorted by entries desc
    assert data["rows"][0]["car"] == "Ferrari 296 GT3"


def test_compute_all_classes_when_class_is_none():
    result = _result(1, "T1", 2000,
                     race_rows=[_row("Ferrari 296 GT3", fic=0),
                                _row("Porsche 963 GTP", cls=GTP, fic=0)])
    by_class = compute_class_metrics([result], None)
    assert set(by_class) == {"IMSA23", "IMSAP"}


# --- split selection ----------------------------------------------------------

def _slot(sid, start, sof, irs):
    # one race row per iRating; car name irrelevant for split selection
    return _result(sid, start, sof, race_rows=[_row("Car", ir=ir) for ir in irs])


def test_select_top_split_keeps_highest_sof_per_slot():
    a = _slot(1, "T1", 3000, [3000, 2500])
    b = _slot(2, "T1", 2000, [1500, 1400])
    c = _slot(3, "T2", 2800, [2900, 2400])
    kept = select_top_split([a, b, c])
    ids = {r["subsession_id"] for r in kept}
    assert ids == {1, 3}          # top split of each of the two slots


def test_select_by_tier_partitions_by_rank():
    a = _slot(1, "T1", 3000, [3000, 2500])
    b = _slot(2, "T1", 2000, [1500, 1400])
    c = _slot(3, "T2", 2800, [2900, 2400])
    d = _slot(4, "T2", 1900, [1600, 1450])
    tiers = select_by_tier([a, b, c, d])
    ranks = {rank: {r["subsession_id"] for r in subset} for rank, subset, _ in tiers}
    assert ranks[1] == {1, 3}     # both slots' split 1
    assert ranks[2] == {2, 4}     # both slots' split 2
    info1 = dict((rank, info) for rank, _, info in tiers)[1]
    assert info1["n_splits"] == 2


def test_select_my_split_picks_the_band_containing_irating():
    a = _slot(1, "T1", 3000, [3000, 2500])
    b = _slot(2, "T1", 2000, [1500, 1400])
    c = _slot(3, "T2", 2800, [2900, 2400])
    d = _slot(4, "T2", 1900, [1600, 1450])
    results = [a, b, c, d]

    high, info_high = select_my_split(results, 2600)
    assert {r["subsession_id"] for r in high} == {1, 3}   # top splits
    assert info_high["matched_slots"] == 2
    assert info_high["rank_median"] == 1

    low, info_low = select_my_split(results, 1450)
    assert {r["subsession_id"] for r in low} == {2, 4}    # second splits
    assert info_low["rank_median"] == 2


# --- export -------------------------------------------------------------------

def test_build_export_rows_and_csv_header():
    result = _result(1, "T1", 2000,
                     race_rows=[_row("Ferrari 296 GT3", fic=0, bl=1000000)],
                     qual_rows=[_row("Ferrari 296 GT3", bl=990000)])
    by_class = compute_class_metrics([result], "IMSA23")
    ctx = {"series_id": 447, "series_name": "IMSA iRacing Series",
           "season_year": 2026, "season_quarter": 3, "week": 5, "track": "Interlagos"}
    rows = build_export_rows("mine", by_class, ctx, top=None)
    assert rows[0]["scope"] == "mine"
    assert rows[0]["car"] == "Ferrari 296 GT3"
    assert rows[0]["best_qual_s"] == 99.0
    csv_text = export_csv(rows)
    assert csv_text.splitlines()[0].startswith("scope,series_id,series_name")
