"""Tests for schedule fetch helper functions."""

import pytest

from iracing_sr_optimizer.fetch_schedule import (
    _calc_races_per_hour,
    _format_race_frequency,
    _format_track_name,
    _map_category,
    _map_license_class,
)


class TestMapCategory:
    def test_oval(self):
        assert _map_category("oval", []) == "OVAL"

    def test_dirt_oval(self):
        assert _map_category("dirt_oval", []) == "DIRT_OVAL"

    def test_dirt_road(self):
        assert _map_category("dirt_road", []) == "DIRT_ROAD"

    def test_road_sports_car_default(self):
        assert _map_category("road", []) == "SPORTS_CAR"

    def test_road_formula_car(self):
        car_types = [{"car_type": "formula_car"}]
        assert _map_category("road", car_types) == "FORMULA_CAR"

    def test_road_sports_car_explicit(self):
        car_types = [{"car_type": "sports_car"}]
        assert _map_category("road", car_types) == "SPORTS_CAR"

    def test_case_insensitive(self):
        assert _map_category("Oval", []) == "OVAL"
        assert _map_category("DIRT_OVAL", []) == "DIRT_OVAL"

    def test_space_to_underscore(self):
        assert _map_category("dirt oval", []) == "DIRT_OVAL"


class TestMapLicenseClass:
    def test_all_groups(self):
        assert _map_license_class(1) == "R"
        assert _map_license_class(2) == "D"
        assert _map_license_class(3) == "C"
        assert _map_license_class(4) == "B"
        assert _map_license_class(5) == "A"

    def test_unknown_defaults_to_rookie(self):
        assert _map_license_class(99) == "R"
        assert _map_license_class(0) == "R"


class TestCalcRacesPerHour:
    def test_repeating_every_30_min(self):
        rtds = [{"repeating": True, "repeat_minutes": 30}]
        assert _calc_races_per_hour(rtds) == 2.0

    def test_repeating_every_60_min(self):
        rtds = [{"repeating": True, "repeat_minutes": 60}]
        assert _calc_races_per_hour(rtds) == 1.0

    def test_repeating_every_15_min(self):
        rtds = [{"repeating": True, "repeat_minutes": 15}]
        assert _calc_races_per_hour(rtds) == 4.0

    def test_non_repeating_with_session_times(self):
        rtds = [{"repeating": False, "session_times": ["12:00", "14:00", "16:00"]}]
        result = _calc_races_per_hour(rtds)
        assert result == pytest.approx(3 / 24.0, abs=0.01)

    def test_empty_returns_default(self):
        assert _calc_races_per_hour([]) == 1.0

    def test_no_descriptors(self):
        assert _calc_races_per_hour(None or []) == 1.0


class TestFormatRaceFrequency:
    def test_every_hour(self):
        rtds = [{"repeating": True, "repeat_minutes": 60}]
        assert _format_race_frequency(rtds) == "Races every hour"

    def test_every_30_min(self):
        rtds = [{"repeating": True, "repeat_minutes": 30}]
        assert "Races at" in _format_race_frequency(rtds)
        assert ":00" in _format_race_frequency(rtds)
        assert ":30" in _format_race_frequency(rtds)

    def test_every_120_min(self):
        rtds = [{"repeating": True, "repeat_minutes": 120}]
        assert "120 minutes" in _format_race_frequency(rtds)

    def test_empty(self):
        assert _format_race_frequency([]) == "Unknown"


class TestFormatTrackName:
    def test_with_config(self):
        track = {"track_name": "Daytona", "config_name": "Oval"}
        assert _format_track_name(track) == "Daytona - Oval"

    def test_without_config(self):
        track = {"track_name": "Daytona", "config_name": ""}
        assert _format_track_name(track) == "Daytona"

    def test_none_config(self):
        track = {"track_name": "Daytona", "config_name": None}
        assert _format_track_name(track) == "Daytona"

    def test_missing_name(self):
        track = {}
        assert _format_track_name(track) == "Unknown"
