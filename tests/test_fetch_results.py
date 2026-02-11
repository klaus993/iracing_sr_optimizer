"""Unit tests for fetch_results aggregation logic."""

import pytest

from iracing_sr_optimizer.fetch_results import aggregate_driver_results, fetch_results_for_series
from iracing_sr_optimizer.models import Series, WeekSchedule


def _driver(incidents, laps, old_sl, new_sl, corners_completed=0):
    return {
        "incidents": incidents,
        "laps_complete": laps,
        "old_sub_level": old_sl,
        "new_sub_level": new_sl,
        "old_cpi": 0.0,
        "new_cpi": 0.0,
        "corners_completed": corners_completed,
    }


class TestAggregateResults:
    def test_avg_incidents_calculation(self):
        drivers = [
            _driver(2, 10, 300, 310, 100),
            _driver(4, 10, 300, 305, 100),
            _driver(6, 10, 300, 295, 100),
            _driver(3, 10, 300, 308, 100),
            _driver(5, 10, 300, 300, 100),
        ]
        result = aggregate_driver_results(drivers)
        assert result is not None
        assert result.avg_incidents == pytest.approx(4.0)

    def test_median_incidents_excludes_outliers(self):
        # 4 clean drivers + 1 DQ with 25 incidents
        drivers = [
            _driver(1, 10, 300, 315, 100),
            _driver(2, 10, 300, 310, 100),
            _driver(2, 10, 300, 310, 100),
            _driver(3, 10, 300, 308, 100),
            _driver(25, 10, 300, 250, 100),
        ]
        result = aggregate_driver_results(drivers)
        assert result is not None
        # Median should be 2, mean should be 6.6
        assert result.median_incidents == 2
        assert result.avg_incidents == pytest.approx(6.6)

    def test_avg_sr_delta(self):
        drivers = [
            _driver(2, 10, 300, 320, 100),   # +20
            _driver(3, 10, 300, 310, 100),   # +10
            _driver(5, 10, 300, 290, 100),   # -10
        ]
        result = aggregate_driver_results(drivers)
        assert result is not None
        # mean of [20, 10, -10] = 20/3 ≈ 6.67
        assert result.avg_sr_delta == pytest.approx(20 / 3)

    def test_avg_cpi(self):
        drivers = [
            _driver(2, 10, 300, 310, 100),  # CPI = 100/2 = 50
            _driver(5, 10, 300, 305, 200),  # CPI = 200/5 = 40
        ]
        result = aggregate_driver_results(drivers)
        assert result is not None
        assert result.avg_cpi == pytest.approx(45.0)

    def test_empty_results_returns_none(self):
        result = aggregate_driver_results([])
        assert result is None

    def test_zero_incident_drivers_cpi(self):
        # Driver with 0 incidents should be excluded from CPI average
        drivers = [
            _driver(0, 10, 300, 320, 100),  # 0 incidents — excluded from CPI
            _driver(2, 10, 300, 310, 100),  # CPI = 50
        ]
        result = aggregate_driver_results(drivers)
        assert result is not None
        # Only the driver with incidents=2 should count for CPI
        assert result.avg_cpi == pytest.approx(50.0)
        # But avg_incidents includes all drivers
        assert result.avg_incidents == pytest.approx(1.0)


class TestFetchResultsTokenRefresh:
    def _make_series(self):
        week = WeekSchedule(
            week=1,
            start_date="2026-01-01",
            track="Daytona International Speedway - Oval",
            race_laps=10,
        )
        return Series(
            name="Test Series",
            category="OVAL",
            license_class="D",
            license_range="R --> D",
            cars=["Test Car"],
            race_frequency="Races every hour",
            races_per_hour=1.0,
            min_entries=6,
            split_at=20,
            drops=4,
            is_heat_racing=False,
            is_team_racing=False,
            incident_dq=17,
            incident_penalty_threshold=None,
            weeks=[week],
            series_id=1,
            season_id=1001,
        )

    def test_refresh_on_access_token_error(self, monkeypatch):
        series = self._make_series()

        class FakeClient:
            def __init__(self, fail_first=True):
                self.fail_first = fail_first

            def result_season_results(self, season_id, event_type=5, race_week_num=0):
                return {"results_list": [{"subsession_id": 123}]}

            def result(self, subsession_id):
                if self.fail_first:
                    self.fail_first = False
                    raise Exception("Access token not valid")
                return {
                    "corners_per_lap": 4,
                    "session_results": [{
                        "simsession_type": 6,
                        "results": [{
                            "incidents": 2,
                            "laps_complete": 10,
                            "old_sub_level": 300,
                            "new_sub_level": 310,
                            "old_cpi": 0.0,
                            "new_cpi": 0.0,
                        }],
                    }],
                }

        client = FakeClient()

        def _refresh_client():
            return FakeClient(fail_first=False)

        monkeypatch.setattr("iracing_sr_optimizer.fetch_results.refresh_client", _refresh_client)

        empirical = fetch_results_for_series(series, 1, client)
        assert empirical is not None
        assert empirical.sample_size == 1

    def test_refresh_on_json_decode_error(self, monkeypatch):
        import json

        series = self._make_series()

        class FakeClient:
            def __init__(self, fail_first=True):
                self.fail_first = fail_first

            def result_season_results(self, season_id, event_type=5, race_week_num=0):
                return {"results_list": [{"subsession_id": 456}]}

            def result(self, subsession_id):
                if self.fail_first:
                    self.fail_first = False
                    raise json.JSONDecodeError("Expecting value", "", 0)
                return {
                    "corners_per_lap": 4,
                    "session_results": [{
                        "simsession_type": 6,
                        "results": [{
                            "incidents": 1,
                            "laps_complete": 5,
                            "old_sub_level": 300,
                            "new_sub_level": 305,
                            "old_cpi": 0.0,
                            "new_cpi": 0.0,
                        }],
                    }],
                }

        client = FakeClient()

        def _refresh_client():
            return FakeClient(fail_first=False)

        monkeypatch.setattr("iracing_sr_optimizer.fetch_results.refresh_client", _refresh_client)

        empirical = fetch_results_for_series(series, 1, client)
        assert empirical is not None
        assert empirical.sample_size == 1
