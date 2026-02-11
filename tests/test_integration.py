"""Integration tests — exercise the full pipeline from JSON to ranked output."""

import csv
import io
import json
from pathlib import Path

import pytest

from iracing_sr_optimizer import config
from iracing_sr_optimizer.models import Series, SeriesEmpirical, UserSR, load_schedule
from iracing_sr_optimizer.output_formatter import format_csv, format_json, format_table
from iracing_sr_optimizer.sr_calculator import calculate_sr_potential, rank_series
from iracing_sr_optimizer.main import main

# ---------------------------------------------------------------------------
# Fixture data — ~10 realistic series covering all categories and edge cases
# ---------------------------------------------------------------------------

FIXTURE_DATA = {"series": [
    # 1. OVAL — lap-based
    {
        "name": "NASCAR Legends Series",
        "category": "OVAL",
        "license_class": "D",
        "license_range": "R --> D",
        "cars": ["Legends Ford '34 Coupe"],
        "race_frequency": "Races every hour",
        "races_per_hour": 1.0,
        "min_entries": 6,
        "split_at": 20,
        "drops": 4,
        "is_heat_racing": False,
        "is_team_racing": False,
        "incident_dq": 17,
        "incident_penalty_threshold": None,
        "series_id": 101,
        "season_id": 1001,
        "weeks": [
            {"week": 1, "start_date": "2025-03-11", "track": "Charlotte Motor Speedway - Legends Oval", "track_id": 1, "race_laps": 40, "race_minutes": None, "heat_laps": None, "consolation_laps": None, "feature_laps": None},
            {"week": 2, "start_date": "2025-03-18", "track": "Langley Speedway", "track_id": 2, "race_laps": 35, "race_minutes": None, "heat_laps": None, "consolation_laps": None, "feature_laps": None},
        ],
    },
    # 2. OVAL — time-based
    {
        "name": "NASCAR iRacing Series Fixed",
        "category": "OVAL",
        "license_class": "C",
        "license_range": "D --> C",
        "cars": ["NASCAR Cup Series Next Gen Chevrolet Camaro ZL1"],
        "race_frequency": "Races at :00 and :30",
        "races_per_hour": 2.0,
        "min_entries": 6,
        "split_at": 40,
        "drops": 4,
        "is_heat_racing": False,
        "is_team_racing": False,
        "incident_dq": 17,
        "incident_penalty_threshold": None,
        "series_id": 102,
        "season_id": 1002,
        "weeks": [
            {"week": 1, "start_date": "2025-03-11", "track": "Daytona International Speedway - Oval", "track_id": 3, "race_laps": None, "race_minutes": 25, "heat_laps": None, "consolation_laps": None, "feature_laps": None},
        ],
    },
    # 3. SPORTS_CAR — D license
    {
        "name": "Mazda MX-5 Cup",
        "category": "SPORTS_CAR",
        "license_class": "D",
        "license_range": "R --> D",
        "cars": ["Global Mazda MX-5 Cup"],
        "race_frequency": "Races every hour",
        "races_per_hour": 1.0,
        "min_entries": 6,
        "split_at": 24,
        "drops": 4,
        "is_heat_racing": False,
        "is_team_racing": False,
        "incident_dq": 17,
        "incident_penalty_threshold": None,
        "series_id": 103,
        "season_id": 1003,
        "weeks": [
            {"week": 1, "start_date": "2025-03-11", "track": "Summit Point Raceway", "track_id": 10, "race_laps": 15, "race_minutes": None, "heat_laps": None, "consolation_laps": None, "feature_laps": None},
        ],
    },
    # 4. SPORTS_CAR — C license
    {
        "name": "IMSA Michelin Pilot Challenge",
        "category": "SPORTS_CAR",
        "license_class": "C",
        "license_range": "D --> C",
        "cars": ["Porsche 718 Cayman GT4 Clubsport MR"],
        "race_frequency": "Races every hour",
        "races_per_hour": 1.0,
        "min_entries": 6,
        "split_at": 30,
        "drops": 4,
        "is_heat_racing": False,
        "is_team_racing": False,
        "incident_dq": 17,
        "incident_penalty_threshold": None,
        "series_id": 104,
        "season_id": 1004,
        "weeks": [
            {"week": 1, "start_date": "2025-03-11", "track": "Watkins Glen International - Boot", "track_id": 11, "race_laps": 12, "race_minutes": None, "heat_laps": None, "consolation_laps": None, "feature_laps": None},
        ],
    },
    # 5. FORMULA_CAR — D license (key regression case)
    {
        "name": "Formula Vee",
        "category": "FORMULA_CAR",
        "license_class": "D",
        "license_range": "R --> D",
        "cars": ["Formula Vee"],
        "race_frequency": "Races every hour",
        "races_per_hour": 1.0,
        "min_entries": 6,
        "split_at": 24,
        "drops": 4,
        "is_heat_racing": False,
        "is_team_racing": False,
        "incident_dq": 17,
        "incident_penalty_threshold": None,
        "series_id": 105,
        "season_id": 1005,
        "weeks": [
            {"week": 1, "start_date": "2025-03-11", "track": "Summit Point Raceway", "track_id": 10, "race_laps": 12, "race_minutes": None, "heat_laps": None, "consolation_laps": None, "feature_laps": None},
        ],
    },
    # 6. FORMULA_CAR — C license
    {
        "name": "Skip Barber Race Series",
        "category": "FORMULA_CAR",
        "license_class": "C",
        "license_range": "D --> C",
        "cars": ["Skip Barber Formula 2000"],
        "race_frequency": "Races every hour",
        "races_per_hour": 1.0,
        "min_entries": 6,
        "split_at": 24,
        "drops": 4,
        "is_heat_racing": False,
        "is_team_racing": False,
        "incident_dq": 17,
        "incident_penalty_threshold": None,
        "series_id": 106,
        "season_id": 1006,
        "weeks": [
            {"week": 1, "start_date": "2025-03-11", "track": "Lime Rock Park", "track_id": 12, "race_laps": 18, "race_minutes": None, "heat_laps": None, "consolation_laps": None, "feature_laps": None},
        ],
    },
    # 7. DIRT_OVAL — heat racing with heat_laps + feature_laps
    {
        "name": "DIRTcar 360 Sprint Cars",
        "category": "DIRT_OVAL",
        "license_class": "D",
        "license_range": "R --> D",
        "cars": ["Dirt 360 Sprint Car"],
        "race_frequency": "Races every hour",
        "races_per_hour": 1.0,
        "min_entries": 6,
        "split_at": 20,
        "drops": 4,
        "is_heat_racing": True,
        "is_team_racing": False,
        "incident_dq": 17,
        "incident_penalty_threshold": None,
        "series_id": 107,
        "season_id": 1007,
        "weeks": [
            {"week": 1, "start_date": "2025-03-11", "track": "Knoxville Raceway", "track_id": 20, "race_laps": None, "race_minutes": None, "heat_laps": 8, "consolation_laps": 6, "feature_laps": 20},
        ],
    },
    # 8. DIRT_ROAD
    {
        "name": "iRacing Rallycross Series",
        "category": "DIRT_ROAD",
        "license_class": "D",
        "license_range": "R --> D",
        "cars": ["Volkswagen Beetle GRC"],
        "race_frequency": "Races at :00 and :30",
        "races_per_hour": 2.0,
        "min_entries": 6,
        "split_at": 16,
        "drops": 4,
        "is_heat_racing": False,
        "is_team_racing": False,
        "incident_dq": 17,
        "incident_penalty_threshold": None,
        "series_id": 108,
        "season_id": 1008,
        "weeks": [
            {"week": 1, "start_date": "2025-03-11", "track": "Daytona Rallycross and Dirt Road - Rallycross Long", "track_id": 30, "race_laps": 6, "race_minutes": None, "heat_laps": None, "consolation_laps": None, "feature_laps": None},
        ],
    },
    # 9. Edge case — no week data, high incident_dq
    {
        "name": "Empty Weeks Series",
        "category": "OVAL",
        "license_class": "A",
        "license_range": "B --> A",
        "cars": ["Some Car"],
        "race_frequency": "Races every hour",
        "races_per_hour": 1.0,
        "min_entries": 6,
        "split_at": 20,
        "drops": 4,
        "is_heat_racing": False,
        "is_team_racing": False,
        "incident_dq": 35,
        "incident_penalty_threshold": 15,
        "series_id": 109,
        "season_id": 1009,
        "weeks": [],
    },
    # 10. Team racing series
    {
        "name": "IMSA Endurance Series",
        "category": "SPORTS_CAR",
        "license_class": "B",
        "license_range": "C --> B",
        "cars": ["Porsche 911 GT3 R"],
        "race_frequency": "Scheduled times",
        "races_per_hour": 0.125,
        "min_entries": 6,
        "split_at": 40,
        "drops": 0,
        "is_heat_racing": False,
        "is_team_racing": True,
        "incident_dq": None,
        "incident_penalty_threshold": None,
        "series_id": 110,
        "season_id": 1010,
        "weeks": [
            {"week": 1, "start_date": "2025-03-11", "track": "Road America", "track_id": 40, "race_laps": None, "race_minutes": 120, "heat_laps": None, "consolation_laps": None, "feature_laps": None},
        ],
    },
]}


SCHEDULE_JSON = config.SCHEDULE_JSON


@pytest.fixture
def fixture_path(tmp_path):
    """Write FIXTURE_DATA to a temp file and return the path."""
    p = tmp_path / "schedule_data.json"
    p.write_text(json.dumps(FIXTURE_DATA))
    return p


@pytest.fixture
def fixture_series(fixture_path):
    """Load FIXTURE_DATA through the real load_schedule pipeline."""
    return load_schedule(fixture_path)


# ---------------------------------------------------------------------------
# TestLoadAndRank — full pipeline from JSON → models → ranking
# ---------------------------------------------------------------------------

class TestLoadAndRank:
    def test_load_fixture_parses_all_series(self, fixture_series):
        # 10 entries total, but one has no weeks — load_schedule still creates it
        assert len(fixture_series) == 10
        # Verify series_id and season_id are loaded
        for s in fixture_series:
            assert s.series_id is not None
            assert s.season_id is not None

    def test_all_five_categories_represented(self, fixture_series):
        results = rank_series(fixture_series, week_num=1)
        categories = {r.category for r in results}
        assert categories == {"OVAL", "SPORTS_CAR", "FORMULA_CAR", "DIRT_OVAL", "DIRT_ROAD"}

    def test_formula_car_series_ranked(self, fixture_series):
        results = rank_series(fixture_series, week_num=1, category_filter="FORMULA_CAR")
        assert len(results) >= 2
        assert all(r.category == "FORMULA_CAR" for r in results)
        # Verify sorted descending
        scores = [r.farming_score for r in results]
        assert scores == sorted(scores, reverse=True)

    def test_category_filter_excludes_others(self, fixture_series):
        results = rank_series(fixture_series, week_num=1, category_filter="OVAL")
        assert len(results) > 0
        categories = {r.category for r in results}
        assert categories == {"OVAL"}
        assert "SPORTS_CAR" not in categories
        assert "FORMULA_CAR" not in categories

    def test_license_filter_respects_ordering(self, fixture_series):
        results = rank_series(fixture_series, week_num=1, license_filter="D")
        license_classes = {r.license_class for r in results}
        # D filter should include R and D, exclude C/B/A
        assert license_classes <= {"R", "D"}

    def test_results_sorted_descending(self, fixture_series):
        results = rank_series(fixture_series, week_num=1)
        scores = [r.farming_score for r in results]
        assert scores == sorted(scores, reverse=True)

    def test_heat_racing_uses_heat_plus_feature(self, fixture_series):
        heat_series = [s for s in fixture_series if s.is_heat_racing][0]
        result = calculate_sr_potential(heat_series, week_num=1)
        assert result is not None
        # heat_laps=8 + feature_laps=20 = 28
        assert result.effective_laps == 28
        assert "H:8" in result.heat_detail
        assert "F:20" in result.heat_detail

    def test_dirt_category_gets_sr_advantage(self, fixture_series):
        # Compare a dirt series vs a pavement series with same parameters
        dirt_series = [s for s in fixture_series if s.category == "DIRT_OVAL"][0]
        dirt_result = calculate_sr_potential(dirt_series, week_num=1)
        assert dirt_result is not None
        # Dirt has incident_severity=0.5, so farming_score = corners_per_hour * dq_factor / 0.5
        # This means the score is 2x what it would be with severity=1.0
        expected_raw = dirt_result.corners_per_hour * (dirt_result.incident_dq or 17) / 17.0
        assert dirt_result.farming_score == pytest.approx(expected_raw / 0.5)

    def test_series_with_no_weeks_returns_none(self, fixture_series):
        empty = [s for s in fixture_series if s.name == "Empty Weeks Series"][0]
        result = calculate_sr_potential(empty, week_num=1)
        assert result is None

    def test_team_racing_series_loads(self, fixture_series):
        team = [s for s in fixture_series if s.is_team_racing][0]
        assert team.name == "IMSA Endurance Series"
        result = calculate_sr_potential(team, week_num=1)
        assert result is not None


# ---------------------------------------------------------------------------
# TestCLIIntegration — call main(argv=[...]) and capture output
# ---------------------------------------------------------------------------

class TestCLIIntegration:
    @pytest.fixture(autouse=True)
    def _patch_schedule_path(self, fixture_path, monkeypatch):
        """Point SCHEDULE_JSON at the fixture file for all CLI tests."""
        monkeypatch.setattr(config, "SCHEDULE_JSON", fixture_path)

    def test_week_analysis_produces_output(self, capsys):
        main(["--week", "1", "--no-api"])
        output = capsys.readouterr().out
        assert "iRacing SR Optimizer - Week 1" in output
        assert "Series" in output
        assert "Track" in output
        assert "Score" in output

    def test_json_output_is_valid(self, capsys):
        main(["--week", "1", "--json", "--no-api"])
        output = capsys.readouterr().out
        data = json.loads(output)
        assert isinstance(data, list)
        assert len(data) > 0
        assert "series_name" in data[0]
        assert "farming_score" in data[0]
        assert "category" in data[0]

    def test_csv_output_has_header(self, capsys):
        main(["--week", "1", "--csv", "--no-api"])
        output = capsys.readouterr().out
        reader = csv.reader(io.StringIO(output))
        header = next(reader)
        assert "series_name" in header
        assert "farming_score" in header
        assert "category" in header
        rows = list(reader)
        assert len(rows) > 0

    def test_category_flag_filters_output(self, capsys):
        main(["--week", "1", "--category", "OVAL", "--no-api"])
        output = capsys.readouterr().out
        # OVAL results should be present
        assert "OVAL" in output
        # FORMULA CAR section header should NOT appear
        assert "FORMULA CAR" not in output


# ---------------------------------------------------------------------------
# TestRealDataValidation — run against data/schedule_data.json if it exists
# ---------------------------------------------------------------------------

class TestRealDataValidation:
    @pytest.fixture(autouse=True)
    def _skip_if_no_data(self):
        if not SCHEDULE_JSON.exists():
            pytest.skip("No schedule data at " + str(SCHEDULE_JSON))

    def test_schedule_data_schema(self):
        all_series = load_schedule(SCHEDULE_JSON)
        valid_categories = {"OVAL", "SPORTS_CAR", "FORMULA_CAR", "DIRT_OVAL", "DIRT_ROAD"}
        valid_licenses = {"R", "D", "C", "B", "A"}
        for s in all_series:
            assert s.name, f"Series missing name: {s}"
            assert s.category in valid_categories, f"Bad category {s.category} in {s.name}"
            assert s.license_class in valid_licenses, f"Bad license {s.license_class} in {s.name}"

    def test_all_weeks_have_tracks(self):
        all_series = load_schedule(SCHEDULE_JSON)
        for s in all_series:
            for w in s.weeks:
                assert w.track, f"Week {w.week} of {s.name} has no track"
                assert w.track_id is not None, f"Week {w.week} of {s.name} has no track_id"

    def test_ranking_produces_results(self):
        all_series = load_schedule(SCHEDULE_JSON)
        results = rank_series(all_series, week_num=1)
        assert len(results) > 0

    def test_formula_car_present(self):
        all_series = load_schedule(SCHEDULE_JSON)
        results = rank_series(all_series, week_num=1, category_filter="FORMULA_CAR")
        assert len(results) > 0, "No FORMULA_CAR series found — data may need re-fetching with the category fix"


# ---------------------------------------------------------------------------
# TestEmpiricalEnrichment — test full pipeline with empirical data
# ---------------------------------------------------------------------------

class TestEmpiricalEnrichment:
    @pytest.fixture
    def empirical_data(self):
        """Mock empirical data matching some fixture series."""
        return {
            "Mazda MX-5 Cup": SeriesEmpirical(
                series_name="Mazda MX-5 Cup",
                avg_incidents=3.5,
                median_incidents=3.0,
                avg_sr_delta=12.0,
                avg_cpi=45.0,
                sample_size=120,
                subsessions_fetched=8,
            ),
            "Formula Vee": SeriesEmpirical(
                series_name="Formula Vee",
                avg_incidents=5.0,
                median_incidents=4.0,
                avg_sr_delta=-5.0,
                avg_cpi=30.0,
                sample_size=80,
                subsessions_fetched=6,
            ),
        }

    @pytest.fixture
    def user_sr(self):
        return UserSR(
            sub_level=345, license_class="C", sr_display=3.45,
            cpi=40.0, category="SPORTS_CAR",
        )

    def test_rank_series_with_empirical_data(self, fixture_series, empirical_data):
        results = rank_series(fixture_series, week_num=1, empirical_data=empirical_data)
        mazda = [r for r in results if r.series_name == "Mazda MX-5 Cup"]
        assert len(mazda) == 1
        assert mazda[0].avg_incidents == pytest.approx(3.5)
        assert mazda[0].avg_sr_delta == pytest.approx(12.0)
        assert mazda[0].empirical_sample_size == 120

    def test_rank_series_without_empirical_graceful(self, fixture_series):
        results = rank_series(fixture_series, week_num=1)
        for r in results:
            assert r.avg_incidents is None
            assert r.avg_sr_delta is None
            assert r.empirical_sample_size is None
            assert r.predicted_sr_direction is None

    def test_table_output_includes_empirical_columns(self, fixture_series, empirical_data):
        results = rank_series(fixture_series, week_num=1, empirical_data=empirical_data)
        output = format_table(results, week_num=1)
        assert "AvgInc" in output
        assert "AvgSR" in output

    def test_json_output_includes_empirical_fields(self, fixture_series, empirical_data):
        results = rank_series(fixture_series, week_num=1, empirical_data=empirical_data)
        output = format_json(results)
        data = json.loads(output)
        assert any(d["avg_incidents"] is not None for d in data)
        assert any(d["avg_sr_delta"] is not None for d in data)
        assert "empirical_sample_size" in data[0]
        assert "predicted_sr_direction" in data[0]

    def test_csv_output_includes_empirical_header(self, fixture_series, empirical_data):
        results = rank_series(fixture_series, week_num=1, empirical_data=empirical_data)
        output = format_csv(results)
        reader = csv.reader(io.StringIO(output))
        header = next(reader)
        assert "avg_incidents" in header
        assert "avg_sr_delta" in header
        assert "empirical_sample_size" in header
        assert "predicted_sr_direction" in header

    def test_personal_prediction_in_output(self, fixture_series, empirical_data, user_sr):
        results = rank_series(
            fixture_series, week_num=1,
            empirical_data=empirical_data, user_sr=user_sr,
        )
        output = format_table(results, week_num=1)
        assert "Pred" in output
        # At least one result should have a prediction
        mazda = [r for r in results if r.series_name == "Mazda MX-5 Cup"]
        assert mazda[0].predicted_sr_direction is not None


# ---------------------------------------------------------------------------
# TestCLIWithSR — CLI tests with --my-sr
# ---------------------------------------------------------------------------

class TestCLIWithSR:
    @pytest.fixture(autouse=True)
    def _patch_schedule_path(self, fixture_path, monkeypatch):
        monkeypatch.setattr(config, "SCHEDULE_JSON", fixture_path)

    def test_my_sr_flag_accepted(self, capsys):
        main(["--week", "1", "--no-api", "--my-sr", "C3.45"])
        output = capsys.readouterr().out
        assert "iRacing SR Optimizer" in output

    def test_my_sr_invalid_exits_with_error(self):
        with pytest.raises(SystemExit):
            main(["--week", "1", "--no-api", "--my-sr", "X9.99"])
