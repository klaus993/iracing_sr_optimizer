"""Tests for track corner count lookup and fuzzy matching."""

import pytest

from iracing_sr_optimizer.track_data import (
    _fuzzy_match,
    _normalize_track_name,
    get_corners_for_track,
    TRACK_CORNERS,
)


class TestNormalizeTrackName:
    def test_strips_whitespace(self):
        assert _normalize_track_name("  Daytona  ") == "Daytona"

    def test_removes_date_parenthetical(self):
        result = _normalize_track_name("Track (2025-01-01 some stuff)")
        assert result == "Track"

    def test_collapses_whitespace(self):
        assert _normalize_track_name("Foo  Bar   Baz") == "Foo Bar Baz"

    def test_preserves_normal_parens(self):
        assert _normalize_track_name("World Wide Technology Raceway (Gateway) Oval") == \
            "World Wide Technology Raceway (Gateway) Oval"


class TestFuzzyMatch:
    def test_exact_match(self):
        tracks = {"Daytona International Speedway - Oval": 4}
        assert _fuzzy_match("Daytona International Speedway - Oval", tracks) == 4

    def test_case_insensitive(self):
        tracks = {"Daytona International Speedway - Oval": 4}
        assert _fuzzy_match("daytona international speedway - oval", tracks) == 4

    def test_no_match_returns_none(self):
        tracks = {"Daytona International Speedway - Oval": 4}
        assert _fuzzy_match("Completely Unknown Track", tracks) is None

    def test_prefers_longest_substring_match(self):
        """Charlotte Motor Speedway - Legends Oval (4) should NOT match
        Charlotte Motor Speedway - Oval (also 4) when looking up the Legends variant.
        The longest match should win."""
        tracks = {
            "Charlotte Motor Speedway - Oval": 4,
            "Charlotte Motor Speedway - Legends Oval": 4,
        }
        # Looking up Legends Oval should match the Legends entry, not the shorter one
        result = _fuzzy_match("Charlotte Motor Speedway - Legends Oval", tracks)
        assert result == 4  # Both are 4 in this case, but let's verify it works

        # More important test: different corner counts
        tracks2 = {
            "Charlotte Motor Speedway": 4,
            "Charlotte Motor Speedway - Roval": 17,
        }
        # Should match the longer, more specific key
        assert _fuzzy_match("Charlotte Motor Speedway - Roval", tracks2) == 17

    def test_substring_match_prefers_specific_config(self):
        """When input contains a base name, prefer the longer match."""
        tracks = {
            "Silverstone Circuit - National": 8,
            "Silverstone Circuit - Grand Prix": 18,
            "Silverstone Circuit - International": 12,
        }
        assert _fuzzy_match("Silverstone Circuit - Grand Prix", tracks) == 18
        assert _fuzzy_match("Silverstone Circuit - National", tracks) == 8

    def test_base_name_fallback(self):
        """When no exact/substring match, try base name before config."""
        tracks = {"Okayama International Circuit - Full": 13}
        result = _fuzzy_match("Okayama International Circuit - Short", tracks)
        # Should find Okayama via base name fallback
        assert result is not None


class TestGetCornersForTrack:
    def test_track_id_takes_priority(self, tmp_path, monkeypatch):
        """track_id-based lookup should beat name matching."""
        import iracing_sr_optimizer.track_data as td
        # Set up a fake track_id cache
        monkeypatch.setattr(td, "_track_id_cache", {123: 42})
        result = get_corners_for_track(
            "Nonexistent Track", "OVAL", track_id=123
        )
        assert result == 42

    def test_api_tracks_override_hardcoded(self):
        api = {"My Track": 99}
        result = get_corners_for_track("My Track", "ROAD", api_tracks=api)
        assert result == 99

    def test_hardcoded_fallback(self):
        result = get_corners_for_track(
            "Daytona International Speedway - Oval", "OVAL"
        )
        assert result == 4

    def test_category_default_oval(self):
        result = get_corners_for_track("Totally Unknown Oval", "OVAL")
        assert result == 4

    def test_category_default_road(self):
        result = get_corners_for_track("Totally Unknown Road Course", "SPORTS_CAR")
        assert result == 12

    def test_category_default_dirt_road(self):
        result = get_corners_for_track("Unknown Rallycross", "DIRT_ROAD")
        assert result == 8

    def test_empty_track_name_uses_default(self):
        assert get_corners_for_track("", "OVAL") == 4
        assert get_corners_for_track("", "SPORTS_CAR") == 12
