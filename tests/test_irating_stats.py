"""Tests for the pure logic in irating_stats.py.

No network or OAuth: these exercise the row parsing, category resolution, and the
SQLite-backed distribution/percentile queries against a temporary database seeded
with fixture rows shaped like the iRacing `driver_stats_by_category` response.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from irating_stats import (  # noqa: E402
    CATEGORIES,
    category_count,
    connect,
    distribution_summary,
    percentile_of,
    replace_category,
    resolve_categories,
    row_to_record,
    rows_to_records,
)

SPORTS_CAR = 5


def _api_row(custid, irating, **extra):
    """A driver row shaped like the API (all values are strings, as the API sends)."""
    row = {
        "driver": f"Driver {custid}",
        "custid": str(custid),
        "location": "US",
        "club_name": "Carolina",
        "starts": "10",
        "wins": "1",
        "irating": str(irating),
        "ttrating": "1350",
        "class": "A 3.50",
        "avg_inc": "4.2",
    }
    row.update(extra)
    return row


class TestRowParsing:
    def test_row_to_record_maps_fields(self):
        rec = row_to_record(SPORTS_CAR, _api_row(123, 4200))
        # (category_id, cust_id, driver, location, club, starts, wins,
        #  irating, ttrating, license_class, avg_inc)
        assert rec == (SPORTS_CAR, 123, "Driver 123", "US", "Carolina",
                       10, 1, 4200, 1350, "A 3.50", 4.2)

    def test_row_without_custid_is_dropped(self):
        assert row_to_record(SPORTS_CAR, _api_row(123, 4200) | {"custid": "n/a"}) is None

    def test_negative_irating_is_preserved(self):
        # -1 is iRacing's "no established iRating" sentinel; keep it faithfully in
        # the store (percentile queries filter it out, not the parser).
        rec = row_to_record(SPORTS_CAR, _api_row(9, -1))
        assert rec[7] == -1

    def test_unparseable_numbers_become_none(self):
        rec = row_to_record(SPORTS_CAR, _api_row(1, 5) | {"starts": "", "avg_inc": "x"})
        assert rec[5] is None and rec[10] is None

    def test_rows_to_records_skips_bad_rows(self):
        rows = [_api_row(1, 100), {"custid": "bad", "irating": "5"}, _api_row(2, 200)]
        recs = rows_to_records(SPORTS_CAR, rows)
        assert [r[1] for r in recs] == [1, 2]


class TestResolveCategories:
    def test_default_is_all(self):
        assert resolve_categories(None, want_all=False) == list(CATEGORIES)

    def test_all_flag(self):
        assert resolve_categories("oval", want_all=True) == list(CATEGORIES)

    def test_slugs(self):
        assert resolve_categories("sports_car,oval", want_all=False) == [5, 1]

    def test_numeric_ids(self):
        assert resolve_categories("5,6", want_all=False) == [5, 6]

    def test_unknown_raises(self):
        with pytest.raises(SystemExit):
            resolve_categories("moto_gp", want_all=False)


class TestQueries:
    @pytest.fixture
    def conn(self, tmp_path):
        conn = connect(tmp_path / "t.db")
        # 100 rated drivers at iRatings 1..100, plus some -1 (unrated) that must
        # be excluded from every percentile computation.
        recs = [row_to_record(SPORTS_CAR, _api_row(i, i)) for i in range(1, 101)]
        recs += [row_to_record(SPORTS_CAR, _api_row(1000 + i, -1)) for i in range(5)]
        replace_category(conn, SPORTS_CAR, recs)
        return conn

    def test_count_reflects_all_rows(self, conn):
        # meta count includes the unrated rows (faithful row count of the fetch).
        assert category_count(conn, SPORTS_CAR) == 105

    def test_percentile_excludes_unrated(self, conn):
        # 40 of the 100 rated drivers are below 41; 60 are at-or-above.
        pct = percentile_of(conn, SPORTS_CAR, 41)
        assert pct["total"] == 100
        assert pct["below"] == 40
        assert pct["at_or_above"] == 60
        assert pct["pct_below"] == pytest.approx(40.0)
        assert pct["top_pct"] == pytest.approx(60.0)

    def test_percentile_above_everyone_is_top_zero(self, conn):
        pct = percentile_of(conn, SPORTS_CAR, 1000)
        assert pct["at_or_above"] == 0
        assert pct["top_pct"] == pytest.approx(0.0)

    def test_percentile_empty_category_is_none(self, conn):
        assert percentile_of(conn, 1, 4000) is None

    def test_distribution_summary(self, conn):
        s = distribution_summary(conn, SPORTS_CAR)
        assert s["total"] == 100
        assert s["min"] == 1 and s["max"] == 100
        # p50 is the value at rank round(0.5*100)=50 (0-based) in sorted 1..100.
        assert s["percentiles"][50] == 51
        assert s["percentiles"][90] == 91

    def test_replace_is_idempotent(self, conn):
        # Re-writing a category swaps rows rather than accumulating them.
        replace_category(conn, SPORTS_CAR,
                         [row_to_record(SPORTS_CAR, _api_row(1, 1500))])
        assert category_count(conn, SPORTS_CAR) == 1
        assert percentile_of(conn, SPORTS_CAR, 1000)["total"] == 1
