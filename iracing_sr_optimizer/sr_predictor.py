"""Personal SR prediction — fetch user SR state and predict SR changes."""

from __future__ import annotations

import logging
import re
from typing import Optional

from .config import SR_ROLLING_WINDOW
from .iracing_api import _to_dict
from .models import SeriesEmpirical, SRPrediction, UserSR

logger = logging.getLogger(__name__)

# License class letter by license_level ranges
# license_level: 1-4=R, 5-8=D, 9-12=C, 13-16=B, 17-20=A
LICENSE_LEVEL_MAP = {
    range(1, 5): "R",
    range(5, 9): "D",
    range(9, 13): "C",
    range(13, 17): "B",
    range(17, 21): "A",
}

VALID_LICENSE_CLASSES = {"R", "D", "C", "B", "A"}


def _license_class_from_level(license_level: int) -> str:
    """Map license_level integer to license class letter."""
    for level_range, letter in LICENSE_LEVEL_MAP.items():
        if license_level in level_range:
            return letter
    return "R"


def parse_sr_string(sr_str: str) -> UserSR:
    """Parse a manual SR string like 'C3.45' into a UserSR.

    Format: <license_class><sr_value>, e.g. "C3.45", "D1.50", "A4.99"

    Raises ValueError if format is invalid.
    """
    match = re.match(r"^([A-Da-dRr])(\d+\.\d{2})$", sr_str)
    if not match:
        raise ValueError(
            f"Invalid SR format: '{sr_str}'. Expected format like 'C3.45', 'D1.50', 'A4.99'"
        )

    license_class = match.group(1).upper()
    sr_value = float(match.group(2))

    if license_class not in VALID_LICENSE_CLASSES:
        raise ValueError(
            f"Invalid license class: '{license_class}'. Must be R, D, C, B, or A"
        )

    sub_level = int(round(sr_value * 100))

    return UserSR(
        sub_level=sub_level,
        license_class=license_class,
        sr_display=sr_value,
        cpi=0.0,  # not available from manual input
        category="",
    )


def get_user_sr(client, cust_id: int, category: str) -> Optional[UserSR]:
    """Fetch user's current SR state from recent races.

    Args:
        client: Authenticated iRacing API client.
        cust_id: iRacing customer ID.
        category: Category to filter for (e.g. "SPORTS_CAR").

    Returns:
        UserSR with current sub_level and CPI, or None if not found.
    """
    try:
        recent = client.stats_member_recent_races(cust_id=cust_id)
    except Exception as e:
        logger.warning(f"Failed to fetch recent races for cust_id {cust_id}: {e}")
        return None

    if not recent:
        return None

    # API returns {"races": [...], "cust_id": N}
    if isinstance(recent, dict):
        races = recent.get("races", [])
    else:
        races = list(recent)

    races = [_to_dict(r) if not isinstance(r, dict) else r for r in races]

    if not races:
        return None

    # Use the most recent race (first in list)
    latest = races[0]

    new_sub_level = latest.get("new_sub_level", 0)
    license_level = latest.get("license_level", 1)

    # CPI is not in recent races — fetch from subsession result
    new_cpi = 0.0
    subsession_id = latest.get("subsession_id")
    if subsession_id:
        try:
            sub_data = client.result(subsession_id)
            sub_dict = _to_dict(sub_data) if not isinstance(sub_data, dict) else sub_data
            for session_result in sub_dict.get("session_results", []):
                if session_result.get("simsession_type") != 6:
                    continue
                for driver in session_result.get("results", []):
                    if driver.get("cust_id") == cust_id:
                        new_cpi = driver.get("new_cpi", 0.0)
                        break
                break
        except Exception as e:
            logger.warning(f"Failed to fetch subsession {subsession_id} for CPI: {e}")

    license_class = _license_class_from_level(license_level)
    sr_display = new_sub_level / 100.0

    return UserSR(
        sub_level=new_sub_level,
        license_class=license_class,
        sr_display=sr_display,
        cpi=new_cpi,
        category=category,
    )


def predict_sr_change(
    user_sr: UserSR,
    empirical: SeriesEmpirical,
    corners_per_lap: int,
    effective_laps: float,
) -> SRPrediction:
    """Predict SR change for a user racing in a given series.

    Uses the community-approximated CPI rolling window formula.
    """
    session_corners = corners_per_lap * effective_laps
    assumed_incidents = empirical.avg_incidents

    session_cpi = session_corners / max(assumed_incidents, 0.01)
    user_cpi = user_sr.cpi

    # Determine direction
    threshold = session_cpi * 0.05  # 5% tolerance for "neutral"
    if user_cpi <= 0:
        # No CPI data — fall back to empirical avg_sr_delta
        if empirical.avg_sr_delta > 5:
            direction = "UP"
        elif empirical.avg_sr_delta < -5:
            direction = "DOWN"
        else:
            direction = "NEUTRAL"
    elif session_cpi > user_cpi + threshold:
        direction = "UP"
    elif session_cpi < user_cpi - threshold:
        direction = "DOWN"
    else:
        direction = "NEUTRAL"

    # Confidence based on sample size
    if empirical.sample_size >= 100:
        confidence = "high"
    elif empirical.sample_size >= 20:
        confidence = "medium"
    else:
        confidence = "low"

    return SRPrediction(
        predicted_direction=direction,
        session_cpi=session_cpi,
        user_cpi=user_cpi,
        empirical_avg_delta=empirical.avg_sr_delta,
        confidence=confidence,
    )
