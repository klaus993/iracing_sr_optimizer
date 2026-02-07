"""SR scoring engine - calculates Safety Rating gaining potential."""

from __future__ import annotations

import logging
from typing import Optional

from .config import DIRT_CATEGORIES, LAP_TIME_ESTIMATES
from .models import Series, SRPotential, WeekSchedule
from .track_data import get_corners_for_track

logger = logging.getLogger(__name__)


def calculate_sr_potential(
    series: Series,
    week_num: int,
    api_tracks: Optional[dict[str, int]] = None,
) -> Optional[SRPotential]:
    """Calculate SR potential for a series in a given week."""
    week = series.get_week(week_num)
    if week is None:
        return None

    if not week.track:
        return None

    corners_per_lap = get_corners_for_track(
        week.track, series.category, api_tracks, track_id=week.track_id
    )
    effective_laps = _get_effective_laps(week, series)
    heat_detail = ""

    if effective_laps <= 0:
        return None

    if series.is_heat_racing and week.heat_laps and week.feature_laps:
        heat_detail = f"H:{week.heat_laps}+F:{week.feature_laps}"

    total_corners = corners_per_lap * effective_laps

    # SR score factors
    is_dirt = series.category in DIRT_CATEGORIES
    incident_severity = 0.5 if is_dirt else 1.0  # dirt heavy contact=2x vs pavement=4x
    dq_factor = (series.incident_dq or 17) / 17.0  # normalized to baseline 17

    sr_score = total_corners * dq_factor / incident_severity
    corners_per_hour = total_corners * series.races_per_hour
    farming_score = corners_per_hour * dq_factor / incident_severity

    return SRPotential(
        series_name=series.name,
        category=series.category,
        license_class=series.license_class,
        track=week.track,
        corners_per_lap=corners_per_lap,
        effective_laps=effective_laps,
        total_corners=total_corners,
        races_per_hour=series.races_per_hour,
        corners_per_hour=corners_per_hour,
        incident_dq=series.incident_dq,
        sr_score=sr_score,
        farming_score=farming_score,
        is_heat_racing=series.is_heat_racing,
        heat_detail=heat_detail,
    )


def _get_effective_laps(week: WeekSchedule, series: Series) -> float:
    """Calculate effective race laps for SR purposes."""
    # Heat racing: heat + feature both count for SR
    if series.is_heat_racing and week.heat_laps and week.feature_laps:
        return week.heat_laps + week.feature_laps

    # Lap-based races
    if week.race_laps:
        return week.race_laps

    # Time-based races: estimate laps from duration
    if week.race_minutes:
        category = series.category
        lap_time = LAP_TIME_ESTIMATES.get(category, 2.0)
        estimated_laps = week.race_minutes / lap_time
        logger.debug(
            f"Estimated {estimated_laps:.0f} laps for {week.track} "
            f"({week.race_minutes} min, {lap_time} min/lap)"
        )
        return estimated_laps

    return 0


def rank_series(
    all_series: list[Series],
    week_num: int,
    api_tracks: Optional[dict[str, int]] = None,
    category_filter: Optional[str] = None,
    license_filter: Optional[str] = None,
) -> list[SRPotential]:
    """Calculate and rank all series by SR farming potential for a given week."""
    from .config import LICENSE_ORDER

    results = []
    for series in all_series:
        # Apply category filter
        if category_filter and series.category != category_filter:
            continue

        # Apply license filter: show series accessible at or below the given license
        if license_filter:
            filter_level = LICENSE_ORDER.get(license_filter.upper(), 0)
            series_level = LICENSE_ORDER.get(series.license_class, 0)
            if series_level > filter_level:
                continue

        potential = calculate_sr_potential(series, week_num, api_tracks)
        if potential:
            results.append(potential)

    # Sort by farming score (corners per hour adjusted) descending
    results.sort(key=lambda x: x.farming_score, reverse=True)
    return results
