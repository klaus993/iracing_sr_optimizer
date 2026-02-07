"""Tests for SR scoring engine."""

import pytest

from iracing_sr_optimizer.models import Series, WeekSchedule, SRPotential
from iracing_sr_optimizer.sr_calculator import (
    calculate_sr_potential,
    _get_effective_laps,
    rank_series,
)


def _make_series(
    name="Test Series",
    category="OVAL",
    license_class="D",
    is_heat_racing=False,
    races_per_hour=2.0,
    incident_dq=17,
    weeks=None,
):
    """Helper to build a Series with minimal boilerplate."""
    return Series(
        name=name,
        category=category,
        license_class=license_class,
        license_range="D 1.0 --> A 4.0",
        cars=["Test Car"],
        race_frequency="Every 30 min",
        races_per_hour=races_per_hour,
        min_entries=6,
        split_at=20,
        drops=4,
        is_heat_racing=is_heat_racing,
        is_team_racing=False,
        incident_dq=incident_dq,
        incident_penalty_threshold=None,
        weeks=weeks or [],
    )


def _make_week(
    week=1,
    track="Daytona International Speedway - Oval",
    race_laps=40,
    race_minutes=None,
    heat_laps=None,
    feature_laps=None,
):
    return WeekSchedule(
        week=week,
        start_date="2026-01-01",
        track=track,
        race_laps=race_laps,
        race_minutes=race_minutes,
        heat_laps=heat_laps,
        feature_laps=feature_laps,
    )


class TestGetEffectiveLaps:
    def test_lap_based_race(self):
        week = _make_week(race_laps=50)
        series = _make_series()
        assert _get_effective_laps(week, series) == 50

    def test_time_based_race_oval(self):
        week = _make_week(race_laps=None, race_minutes=30)
        series = _make_series(category="OVAL")
        # OVAL lap time estimate = 0.5 min, so 30/0.5 = 60 laps
        assert _get_effective_laps(week, series) == 60

    def test_time_based_race_road(self):
        week = _make_week(race_laps=None, race_minutes=20)
        series = _make_series(category="SPORTS_CAR")
        # SPORTS_CAR lap time = 2.0 min, so 20/2.0 = 10 laps
        assert _get_effective_laps(week, series) == 10

    def test_heat_racing(self):
        week = _make_week(race_laps=None, heat_laps=8, feature_laps=20)
        series = _make_series(is_heat_racing=True)
        assert _get_effective_laps(week, series) == 28  # 8 + 20

    def test_no_laps_no_minutes(self):
        week = _make_week(race_laps=None, race_minutes=None)
        series = _make_series()
        assert _get_effective_laps(week, series) == 0


class TestCalculateSRPotential:
    def test_basic_calculation(self):
        week = _make_week(race_laps=40)
        series = _make_series(weeks=[week])
        result = calculate_sr_potential(series, 1)
        assert result is not None
        assert result.corners_per_lap == 4  # Daytona Oval
        assert result.effective_laps == 40
        assert result.total_corners == 160  # 4 * 40
        assert result.corners_per_hour == 320  # 160 * 2.0 races/hr

    def test_missing_week_returns_none(self):
        week = _make_week(week=1)
        series = _make_series(weeks=[week])
        assert calculate_sr_potential(series, 5) is None

    def test_empty_track_returns_none(self):
        week = _make_week(track="")
        series = _make_series(weeks=[week])
        result = calculate_sr_potential(series, 1)
        assert result is None

    def test_dirt_category_halves_incident_severity(self):
        week = _make_week(race_laps=40)
        paved = _make_series(category="OVAL", weeks=[week])
        dirt = _make_series(category="DIRT_OVAL", weeks=[week])

        paved_result = calculate_sr_potential(paved, 1)
        dirt_result = calculate_sr_potential(dirt, 1)

        # Same corners, but dirt sr_score should be 2x paved (halved severity)
        assert dirt_result.sr_score == paved_result.sr_score * 2

    def test_heat_racing_detail(self):
        week = _make_week(race_laps=None, heat_laps=8, feature_laps=20)
        series = _make_series(is_heat_racing=True, weeks=[week])
        result = calculate_sr_potential(series, 1)
        assert result is not None
        assert result.heat_detail == "H:8+F:20"
        assert result.is_heat_racing is True


class TestRankSeries:
    def test_sorts_by_farming_score(self):
        # Series A: 4 corners * 40 laps = 160 total, 2/hr
        # Series B: 20 corners * 10 laps = 200 total, 1/hr
        a = _make_series(
            name="Short Oval",
            category="OVAL",
            races_per_hour=2.0,
            weeks=[_make_week(track="Daytona International Speedway - Oval", race_laps=40)],
        )
        b = _make_series(
            name="Long Road",
            category="SPORTS_CAR",
            races_per_hour=1.0,
            weeks=[_make_week(track="Circuit de Spa-Francorchamps", race_laps=10)],
        )
        results = rank_series([a, b], 1)
        assert len(results) == 2
        # Higher farming score should be first
        assert results[0].farming_score >= results[1].farming_score

    def test_category_filter(self):
        oval = _make_series(
            name="Oval",
            category="OVAL",
            weeks=[_make_week()],
        )
        road = _make_series(
            name="Road",
            category="SPORTS_CAR",
            weeks=[_make_week()],
        )
        results = rank_series([oval, road], 1, category_filter="OVAL")
        assert len(results) == 1
        assert results[0].category == "OVAL"

    def test_license_filter(self):
        rookie = _make_series(name="Rookie", license_class="R", weeks=[_make_week()])
        b_class = _make_series(name="B Class", license_class="B", weeks=[_make_week()])
        results = rank_series([rookie, b_class], 1, license_filter="D")
        assert len(results) == 1
        assert results[0].license_class == "R"
