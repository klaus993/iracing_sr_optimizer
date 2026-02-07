"""Output formatting for SR optimizer results."""

from __future__ import annotations

import csv
import io
import json
from typing import Optional

from .config import CATEGORIES, DIRT_CATEGORIES
from .models import SRPotential


CATEGORY_DISPLAY = {
    "OVAL": "OVAL",
    "SPORTS_CAR": "SPORTS CAR",
    "FORMULA_CAR": "FORMULA CAR",
    "DIRT_OVAL": "DIRT OVAL",
    "DIRT_ROAD": "DIRT ROAD",
}

HEAVY_CONTACT_NOTE = {
    "OVAL": "Heavy contact: 4x",
    "SPORTS_CAR": "Heavy contact: 4x",
    "FORMULA_CAR": "Heavy contact: 4x",
    "DIRT_OVAL": "Heavy contact: 2x -- advantage!",
    "DIRT_ROAD": "Heavy contact: 2x -- advantage!",
}


def _truncate(s: str, maxlen: int) -> str:
    return s[:maxlen - 1] + "\u2026" if len(s) > maxlen else s


def format_table(
    results: list[SRPotential],
    week_num: int,
    category_filter: Optional[str] = None,
) -> str:
    """Format results as terminal tables grouped by category."""
    lines = []
    lines.append(f"iRacing SR Optimizer - Week {week_num}")
    lines.append("")

    categories_to_show = [category_filter] if category_filter else CATEGORIES

    for cat in categories_to_show:
        cat_results = [r for r in results if r.category == cat]
        if not cat_results:
            continue

        # Sort by farming_score within category
        cat_results.sort(key=lambda x: x.farming_score, reverse=True)

        display_name = CATEGORY_DISPLAY.get(cat, cat)
        contact_note = HEAVY_CONTACT_NOTE.get(cat, "")
        lines.append(f"{display_name} ({contact_note})")

        # Header
        lines.append(
            f"{'#':>2}  {'Series':<40} {'Track':<30} {'CpL':>3} {'Laps':>5} "
            f"{'Corners':>7} {'/hr':>6} {'Score':>7}"
        )
        lines.append("-" * 105)

        for i, r in enumerate(cat_results, 1):
            series_name = _truncate(r.series_name, 40)
            if r.heat_detail:
                series_name = _truncate(f"{r.series_name} ({r.heat_detail})", 40)
            track = _truncate(r.track, 30)
            laps_str = f"{r.effective_laps:.0f}" if r.effective_laps == int(r.effective_laps) else f"{r.effective_laps:.1f}"

            lines.append(
                f"{i:>2}  {series_name:<40} {track:<30} {r.corners_per_lap:>3} "
                f"{laps_str:>5} {r.total_corners:>7.0f} {r.corners_per_hour:>6.1f} "
                f"{r.farming_score:>7.0f}"
            )

        lines.append("")

    # Cross-category top 10
    if not category_filter:
        lines.append("CROSS-CATEGORY TOP 10 (SR Farming Rate)")
        lines.append(
            f"{'#':>2}  {'Cat':<12} {'Series':<35} {'Track':<25} "
            f"{'Corners/Hr':>10} {'Score':>7}"
        )
        lines.append("-" * 96)

        top10 = sorted(results, key=lambda x: x.farming_score, reverse=True)[:10]
        for i, r in enumerate(top10, 1):
            cat_display = CATEGORY_DISPLAY.get(r.category, r.category)
            series_name = _truncate(r.series_name, 35)
            track = _truncate(r.track, 25)
            lines.append(
                f"{i:>2}  {cat_display:<12} {series_name:<35} {track:<25} "
                f"{r.corners_per_hour:>10.1f} {r.farming_score:>7.0f}"
            )

        lines.append("")

    return "\n".join(lines)


def format_json(results: list[SRPotential]) -> str:
    """Format results as JSON."""
    data = []
    for r in results:
        data.append({
            "series_name": r.series_name,
            "category": r.category,
            "license_class": r.license_class,
            "track": r.track,
            "corners_per_lap": r.corners_per_lap,
            "effective_laps": r.effective_laps,
            "total_corners": r.total_corners,
            "races_per_hour": r.races_per_hour,
            "corners_per_hour": r.corners_per_hour,
            "incident_dq": r.incident_dq,
            "sr_score": r.sr_score,
            "farming_score": r.farming_score,
            "is_heat_racing": r.is_heat_racing,
            "heat_detail": r.heat_detail,
        })
    return json.dumps(data, indent=2)


def format_csv(results: list[SRPotential]) -> str:
    """Format results as CSV."""
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "series_name", "category", "license_class", "track",
        "corners_per_lap", "effective_laps", "total_corners",
        "races_per_hour", "corners_per_hour", "incident_dq",
        "sr_score", "farming_score", "is_heat_racing", "heat_detail",
    ])
    for r in results:
        writer.writerow([
            r.series_name, r.category, r.license_class, r.track,
            r.corners_per_lap, r.effective_laps, r.total_corners,
            r.races_per_hour, r.corners_per_hour, r.incident_dq,
            r.sr_score, r.farming_score, r.is_heat_racing, r.heat_detail,
        ])
    return output.getvalue()
