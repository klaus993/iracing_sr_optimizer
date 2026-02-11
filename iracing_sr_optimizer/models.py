from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class WeekSchedule:
    week: int
    start_date: str
    track: str
    race_laps: Optional[int] = None
    race_minutes: Optional[int] = None
    heat_laps: Optional[int] = None
    consolation_laps: Optional[int] = None
    feature_laps: Optional[int] = None
    track_id: Optional[int] = None

    @classmethod
    def from_dict(cls, d: dict) -> WeekSchedule:
        return cls(
            week=d["week"],
            start_date=d["start_date"],
            track=d["track"],
            race_laps=d.get("race_laps"),
            race_minutes=d.get("race_minutes"),
            heat_laps=d.get("heat_laps"),
            consolation_laps=d.get("consolation_laps"),
            feature_laps=d.get("feature_laps"),
            track_id=d.get("track_id"),
        )


@dataclass
class Series:
    name: str
    category: str
    license_class: str
    license_range: str
    cars: list[str]
    race_frequency: str
    races_per_hour: float
    min_entries: int
    split_at: int
    drops: int
    is_heat_racing: bool
    is_team_racing: bool
    incident_dq: Optional[int]
    incident_penalty_threshold: Optional[int]
    weeks: list[WeekSchedule] = field(default_factory=list)
    series_id: Optional[int] = None
    season_id: Optional[int] = None

    @classmethod
    def from_dict(cls, d: dict) -> Series:
        weeks = [WeekSchedule.from_dict(w) for w in d.get("weeks", [])]
        return cls(
            name=d["name"],
            category=d["category"],
            license_class=d["license_class"],
            license_range=d["license_range"],
            cars=d.get("cars", []),
            race_frequency=d.get("race_frequency", ""),
            races_per_hour=d.get("races_per_hour", 1.0),
            min_entries=d.get("min_entries", 6),
            split_at=d.get("split_at", 20),
            drops=d.get("drops", 4),
            is_heat_racing=d.get("is_heat_racing", False),
            is_team_racing=d.get("is_team_racing", False),
            incident_dq=d.get("incident_dq"),
            incident_penalty_threshold=d.get("incident_penalty_threshold"),
            weeks=weeks,
            series_id=d.get("series_id"),
            season_id=d.get("season_id"),
        )

    def get_week(self, week_num: int) -> Optional[WeekSchedule]:
        for w in self.weeks:
            if w.week == week_num:
                return w
        return None


@dataclass
class SRPotential:
    series_name: str
    category: str
    license_class: str
    track: str
    corners_per_lap: int
    effective_laps: float
    total_corners: float
    races_per_hour: float
    corners_per_hour: float
    incident_dq: Optional[int]
    sr_score: float
    farming_score: float
    is_heat_racing: bool
    heat_detail: str = ""
    avg_incidents: Optional[float] = None
    avg_sr_delta: Optional[float] = None
    empirical_sample_size: Optional[int] = None
    predicted_sr_direction: Optional[str] = None


@dataclass
class SeriesEmpirical:
    series_name: str
    avg_incidents: float
    median_incidents: float
    avg_sr_delta: float
    avg_cpi: float
    sample_size: int
    subsessions_fetched: int


@dataclass
class UserSR:
    sub_level: int
    license_class: str
    sr_display: float
    cpi: float
    category: str


@dataclass
class SRPrediction:
    predicted_direction: str
    session_cpi: float
    user_cpi: float
    empirical_avg_delta: float
    confidence: str


def load_schedule(path: Path) -> list[Series]:
    with open(path) as f:
        data = json.load(f)
    return [Series.from_dict(s) for s in data["series"]]
