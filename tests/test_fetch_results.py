"""Unit tests for fetch_results aggregation logic."""

import pytest

from iracing_sr_optimizer.fetch_results import aggregate_driver_results


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
