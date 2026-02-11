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


def _has_empirical(results: list[SRPotential]) -> bool:
    return any(r.avg_incidents is not None for r in results)


def _has_prediction(results: list[SRPotential]) -> bool:
    return any(r.predicted_sr_direction is not None for r in results)


def _format_sr_delta(delta: Optional[float]) -> str:
    if delta is None:
        return ""
    sign = "+" if delta >= 0 else ""
    return f"{sign}{delta / 100:.2f}"


def _format_prediction(direction: Optional[str]) -> str:
    if direction is None:
        return ""
    return {"UP": "+", "DOWN": "-", "NEUTRAL": "~"}.get(direction, "")


def format_table(
    results: list[SRPotential],
    week_num: int,
    category_filter: Optional[str] = None,
) -> str:
    """Format results as terminal tables grouped by category."""
    lines = []
    lines.append(f"iRacing SR Optimizer - Week {week_num}")
    lines.append("")

    has_emp = _has_empirical(results)
    has_pred = _has_prediction(results)

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
        header = (
            f"{'#':>2}  {'Series':<40} {'Track':<30} {'CpL':>3} {'Laps':>5} "
            f"{'Corners':>7} {'/hr':>6} {'Score':>7}"
        )
        if has_emp:
            header += f" {'AvgInc':>6} {'AvgSR':>6}"
        if has_pred:
            header += f" {'Pred':>4}"
        lines.append(header)
        sep_len = 105 + (13 if has_emp else 0) + (5 if has_pred else 0)
        lines.append("-" * sep_len)

        for i, r in enumerate(cat_results, 1):
            series_name = _truncate(r.series_name, 40)
            if r.heat_detail:
                series_name = _truncate(f"{r.series_name} ({r.heat_detail})", 40)
            track = _truncate(r.track, 30)
            laps_str = f"{r.effective_laps:.0f}" if r.effective_laps == int(r.effective_laps) else f"{r.effective_laps:.1f}"

            line = (
                f"{i:>2}  {series_name:<40} {track:<30} {r.corners_per_lap:>3} "
                f"{laps_str:>5} {r.total_corners:>7.0f} {r.corners_per_hour:>6.1f} "
                f"{r.farming_score:>7.0f}"
            )
            if has_emp:
                avg_inc = f"{r.avg_incidents:.1f}" if r.avg_incidents is not None else ""
                avg_sr = _format_sr_delta(r.avg_sr_delta) if r.avg_sr_delta is not None else ""
                line += f" {avg_inc:>6} {avg_sr:>6}"
            if has_pred:
                pred = _format_prediction(r.predicted_sr_direction)
                line += f" {pred:>4}"
            lines.append(line)

        lines.append("")

    # Cross-category top 10
    if not category_filter:
        lines.append("CROSS-CATEGORY TOP 10 (SR Farming Rate)")
        header = (
            f"{'#':>2}  {'Cat':<12} {'Series':<35} {'Track':<25} "
            f"{'Corners/Hr':>10} {'Score':>7}"
        )
        if has_emp:
            header += f" {'AvgInc':>6} {'AvgSR':>6}"
        if has_pred:
            header += f" {'Pred':>4}"
        lines.append(header)
        sep_len = 96 + (13 if has_emp else 0) + (5 if has_pred else 0)
        lines.append("-" * sep_len)

        top10 = sorted(results, key=lambda x: x.farming_score, reverse=True)[:10]
        for i, r in enumerate(top10, 1):
            cat_display = CATEGORY_DISPLAY.get(r.category, r.category)
            series_name = _truncate(r.series_name, 35)
            track = _truncate(r.track, 25)
            line = (
                f"{i:>2}  {cat_display:<12} {series_name:<35} {track:<25} "
                f"{r.corners_per_hour:>10.1f} {r.farming_score:>7.0f}"
            )
            if has_emp:
                avg_inc = f"{r.avg_incidents:.1f}" if r.avg_incidents is not None else ""
                avg_sr = _format_sr_delta(r.avg_sr_delta) if r.avg_sr_delta is not None else ""
                line += f" {avg_inc:>6} {avg_sr:>6}"
            if has_pred:
                pred = _format_prediction(r.predicted_sr_direction)
                line += f" {pred:>4}"
            lines.append(line)

        lines.append("")

    return "\n".join(lines)


def format_json(results: list[SRPotential]) -> str:
    """Format results as JSON."""
    data = []
    for r in results:
        entry = {
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
            "avg_incidents": r.avg_incidents,
            "avg_sr_delta": r.avg_sr_delta,
            "empirical_sample_size": r.empirical_sample_size,
            "predicted_sr_direction": r.predicted_sr_direction,
        }
        data.append(entry)
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
        "avg_incidents", "avg_sr_delta", "empirical_sample_size",
        "predicted_sr_direction",
    ])
    for r in results:
        writer.writerow([
            r.series_name, r.category, r.license_class, r.track,
            r.corners_per_lap, r.effective_laps, r.total_corners,
            r.races_per_hour, r.corners_per_hour, r.incident_dq,
            r.sr_score, r.farming_score, r.is_heat_racing, r.heat_detail,
            r.avg_incidents, r.avg_sr_delta, r.empirical_sample_size,
            r.predicted_sr_direction,
        ])
    return output.getvalue()
