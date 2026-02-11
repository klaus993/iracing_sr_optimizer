"""Unit tests for SR predictor — parsing and prediction logic."""

import pytest

from iracing_sr_optimizer.models import SeriesEmpirical, UserSR
from iracing_sr_optimizer.sr_predictor import (
    _license_class_from_level,
    parse_sr_string,
    predict_sr_change,
)


class TestParseSR:
    def test_parse_c345(self):
        result = parse_sr_string("C3.45")
        assert result.license_class == "C"
        assert result.sub_level == 345
        assert result.sr_display == pytest.approx(3.45)

    def test_parse_r250(self):
        result = parse_sr_string("R2.50")
        assert result.license_class == "R"
        assert result.sub_level == 250

    def test_parse_a499(self):
        result = parse_sr_string("A4.99")
        assert result.license_class == "A"
        assert result.sub_level == 499

    def test_parse_lowercase(self):
        result = parse_sr_string("d1.50")
        assert result.license_class == "D"

    def test_parse_invalid(self):
        with pytest.raises(ValueError):
            parse_sr_string("X5.00")


class TestPredictSRChange:
    def _make_user_sr(self, cpi):
        return UserSR(
            sub_level=345, license_class="C", sr_display=3.45,
            cpi=cpi, category="SPORTS_CAR",
        )

    def _make_empirical(self, avg_incidents, sample_size=50):
        return SeriesEmpirical(
            series_name="Test Series",
            avg_incidents=avg_incidents,
            median_incidents=avg_incidents,
            avg_sr_delta=10.0,
            avg_cpi=0.0,
            sample_size=sample_size,
            subsessions_fetched=5,
        )

    def test_session_cpi_above_user_cpi_predicts_up(self):
        # User CPI is low (10), series has low incidents → high session CPI
        user_sr = self._make_user_sr(cpi=10.0)
        empirical = self._make_empirical(avg_incidents=1.0)
        # 10 corners/lap * 15 laps = 150 corners / 1 incident = CPI 150
        result = predict_sr_change(user_sr, empirical, corners_per_lap=10, effective_laps=15)
        assert result.predicted_direction == "UP"

    def test_session_cpi_below_user_cpi_predicts_down(self):
        # User CPI is very high (200), series has high incidents
        user_sr = self._make_user_sr(cpi=200.0)
        empirical = self._make_empirical(avg_incidents=10.0)
        # 10 corners/lap * 15 laps = 150 corners / 10 incidents = CPI 15
        result = predict_sr_change(user_sr, empirical, corners_per_lap=10, effective_laps=15)
        assert result.predicted_direction == "DOWN"

    def test_similar_cpi_predicts_neutral(self):
        # Session CPI ≈ user CPI → neutral
        user_sr = self._make_user_sr(cpi=50.0)
        empirical = self._make_empirical(avg_incidents=3.0)
        # 10 * 15 = 150 / 3 = 50 — very close to user CPI of 50
        result = predict_sr_change(user_sr, empirical, corners_per_lap=10, effective_laps=15)
        assert result.predicted_direction == "NEUTRAL"

    def test_confidence_high_with_large_sample(self):
        user_sr = self._make_user_sr(cpi=50.0)
        empirical = self._make_empirical(avg_incidents=3.0, sample_size=150)
        result = predict_sr_change(user_sr, empirical, corners_per_lap=10, effective_laps=15)
        assert result.confidence == "high"

    def test_confidence_low_with_small_sample(self):
        user_sr = self._make_user_sr(cpi=50.0)
        empirical = self._make_empirical(avg_incidents=3.0, sample_size=10)
        result = predict_sr_change(user_sr, empirical, corners_per_lap=10, effective_laps=15)
        assert result.confidence == "low"


class TestSubLevelParsing:
    def test_sublevel_ranges(self):
        assert _license_class_from_level(1) == "R"
        assert _license_class_from_level(4) == "R"
        assert _license_class_from_level(5) == "D"
        assert _license_class_from_level(8) == "D"
        assert _license_class_from_level(9) == "C"
        assert _license_class_from_level(12) == "C"
        assert _license_class_from_level(13) == "B"
        assert _license_class_from_level(16) == "B"
        assert _license_class_from_level(17) == "A"
        assert _license_class_from_level(20) == "A"
