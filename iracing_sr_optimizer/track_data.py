"""Track corner count lookup with hardcoded fallback and fuzzy matching."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Optional

from .config import (
    CACHE_DIR,
    DEFAULT_DIRT_ROAD_CORNERS,
    DEFAULT_OVAL_CORNERS,
    DEFAULT_ROAD_CORNERS,
)

logger = logging.getLogger(__name__)

# Hardcoded corner counts: track name (or partial) → corners per lap
# Sources: iRacing track guides, community data
TRACK_CORNERS: dict[str, int] = {
    # === OVALS ===
    "Charlotte Motor Speedway - Oval": 4,
    "Charlotte Motor Speedway - Legends Oval": 4,
    "Langley Speedway": 4,
    "USA International Speedway - Asphalt": 4,
    "Southern National Motorsports Park": 4,
    "South Boston Speedway": 4,
    "Concord Speedway": 4,
    "Oxford Plains Speedway": 4,
    "Lanier National Speedway - Asphalt": 4,
    "Thompson Speedway Motorsports Park - Oval": 4,
    "Nashville Superspeedway": 4,
    "Texas Motor Speedway - Oval": 4,
    "Myrtle Beach Speedway": 4,
    "World Wide Technology Raceway (Gateway) Oval": 4,
    "Pocono Raceway": 3,
    "Sonoma Raceway - Cup Short": 7,
    "Sonoma Raceway - Oval": 4,
    "Daytona International Speedway - Oval": 4,
    "Phoenix Raceway - Oval w/open dogleg": 4,
    "Five Flags Speedway": 4,
    "Kevin Harvick's Kern Raceway - Asphalt Track": 4,
    "Bristol Motor Speedway - Dual Pit Roads": 4,
    "Bristol Motor Speedway": 4,
    "Nashville Fairgrounds Speedway - Oval": 4,
    "Lucas Oil Indianapolis Raceway Park - Oval": 4,
    "Indianapolis Motor Speedway - Oval": 4,
    "Indianapolis Motor Speedway - IndyCar Oval": 4,
    "Darlington Raceway": 4,
    "Atlanta Motor Speedway": 4,
    "Michigan International Speedway": 4,
    "Kansas Speedway": 4,
    "Las Vegas Motor Speedway - Oval": 4,
    "New Hampshire Motor Speedway - Oval": 4,
    "Iowa Speedway - Oval": 4,
    "Richmond Raceway": 4,
    "Dover Motor Speedway": 4,
    "Martinsville Speedway": 4,
    "Talladega Superspeedway": 4,
    "Chicagoland Speedway": 4,
    "Auto Club Speedway": 4,
    "Homestead Miami Speedway - Oval": 4,
    "Kentucky Speedway": 4,
    "Rockingham Speedway - Oval": 4,
    "North Wilkesboro Speedway": 4,
    "Nashville Superspeedway - 2022": 4,
    "Irwindale Speedway - Outer": 4,
    "Irwindale Speedway - Inner": 4,
    "Milwaukee Mile": 4,
    "Stafford Motor Speedway": 4,
    "[Legacy] Texas Motor Speedway - 2009 - Oval": 4,
    "Charlotte Motor Speedway": 4,
    "Sonoma Raceway - Cup": 12,

    # === ROAD COURSES ===
    "Watkins Glen International - Boot": 11,
    "Watkins Glen International - Cup": 7,
    "Watkins Glen International": 11,
    "Summit Point Raceway": 10,
    "Summit Point Raceway - Short": 5,
    "Lime Rock Park": 7,
    "Lime Rock Park - Classic": 7,
    "Lime Rock Park - Grand Prix": 9,
    "Tsukuba Circuit - 2000 Full": 10,
    "Tsukuba Circuit - 2000 Short": 7,
    "Okayama International Circuit - Full": 13,
    "Okayama International Circuit - Short": 9,
    "Oulton Park Circuit - Fosters": 9,
    "Oulton Park Circuit - Island": 6,
    "Oulton Park Circuit - International": 12,
    "Brands Hatch Circuit - Indy": 6,
    "Brands Hatch Circuit - Grand Prix": 9,
    "Road America": 14,
    "Road Atlanta - Full": 12,
    "Sebring International Raceway": 17,
    "Virginia International Raceway - Full Course": 17,
    "Virginia International Raceway - Grand East": 11,
    "Virginia International Raceway - North": 9,
    "Virginia International Raceway - Patriot": 10,
    "Laguna Seca": 11,
    "WeatherTech Raceway at Laguna Seca": 11,
    "Mid-Ohio Sports Car Course - Full": 13,
    "Mid-Ohio Sports Car Course - Short": 9,
    "Barber Motorsports Park - Full": 15,
    "Circuit de Spa-Francorchamps": 20,
    "Circuit de Spa-Francorchamps - Endurance": 20,
    "Monza": 11,
    "Autodromo Nazionale Monza - GP": 11,
    "Autodromo Nazionale Monza - Combined": 11,
    "Autodromo Nazionale Monza - Combined without chicanes": 7,
    "Autodromo Nazionale Monza - Junior": 7,
    "Silverstone Circuit - Grand Prix": 18,
    "Silverstone Circuit - International": 12,
    "Silverstone Circuit - National": 8,
    "Silverstone Circuit - Arena GP": 10,
    "Suzuka International Racing Course - Grand Prix": 17,
    "Suzuka International Racing Course": 17,
    "Interlagos": 15,
    "Autodromo Jose Carlos Pace - Grand Prix": 15,
    "Nürburgring Grand-Prix-Strecke": 15,
    "Nürburgring Grand-Prix-Strecke - BES/WEC": 15,
    "Nürburgring - Nordschleife": 154,
    "Nürburgring Combined": 170,
    "Nürburgring Combined - Gesamtstrecke 24h": 170,
    "Nürburgring Combined - Gesamtstrecke Short w/out Arena": 160,
    "Circuit de Barcelona-Catalunya - Grand Prix": 16,
    "Circuit de Barcelona-Catalunya - National": 10,
    "Circuit de Barcelona-Catalunya - Club": 6,
    "Hungaroring": 14,
    "Red Bull Ring - Grand Prix": 10,
    "Red Bull Ring - National": 7,
    "Imola": 17,
    "Autodromo Enzo e Dino Ferrari": 17,
    "Phillip Island Circuit": 12,
    "Mount Panorama Circuit": 23,
    "Snetterton Circuit - 300": 12,
    "Snetterton Circuit - 200": 9,
    "Snetterton Circuit - 100": 5,
    "Donington Park Racing Circuit - Grand Prix": 12,
    "Donington Park Racing Circuit - National": 8,
    "Fuji International Speedway - Grand Prix": 16,
    "Fuji International Speedway - Short": 10,
    "Daytona International Speedway - Road Course": 12,
    "Long Beach Street Circuit": 11,
    "Circuit de la Sarthe - 24 Heures du Mans": 38,
    "Circuit Zandvoort - Grand Prix": 14,
    "Circuit Zandvoort - Club": 8,
    "Hockenheimring Baden-Württemberg - Grand Prix": 17,
    "Hockenheimring Baden-Württemberg - National": 11,
    "Hockenheimring Baden-Württemberg - Short": 7,
    "Mosport": 10,
    "Canadian Tire Motorsports Park - Grand Prix": 10,
    "Sebring International Raceway - Modified": 11,
    "Portland International Raceway": 12,
    "COTA": 20,
    "Circuit of the Americas - Grand Prix": 20,
    "Circuit of the Americas - East": 11,
    "Circuit of the Americas - West": 9,
    "Miami International Autodrome - Grand Prix": 19,
    "Miami International Autodrome - Extended MIA Loop": 14,
    "Circuit Gilles Villeneuve": 14,
    "Mugello Circuit": 15,
    "Motegi": 14,
    "Twin Ring Motegi - Grand Prix": 14,
    "Misano World Circuit Marco Simoncelli - Grand Prix": 16,
    "Detroit Grand Prix at Belle Isle": 14,
    "Indianapolis Motor Speedway - Road Course": 14,
    "Chicago Street Circuit": 12,
    "Circuit Park Zandvoort": 14,
    "Algarve International Circuit": 15,
    "Knockhill Racing Circuit": 8,
    "Knockhill Racing Circuit - International": 8,
    "Rudskogen Motorsenter": 14,
    "Rudskogen Motorsenter - Rallycross": 6,
    "Sandown Raceway": 11,
    "Sandown International Motor Raceway": 11,
    "The Bend Motorsport Park - GT Circuit": 10,
    "The Bend Motorsport Park - International": 18,
    "The Bend Motorsport Park - West Circuit": 8,
    "Winton Motor Raceway": 12,
    "Symmons Plains Raceway": 6,
    "Bathurst": 23,
    "Motorsport Arena Oschersleben - Grand Prix": 13,
    "Motorsport Arena Oschersleben - National A": 8,
    "Nürburgring Grand-Prix-Strecke - Short w/out Arena": 10,
    "Spielberg": 10,
    "Jeddah Corniche Circuit": 27,
    "Lusail International Circuit": 16,
    "Las Vegas Grand Prix Circuit": 17,
    "Albert Park": 16,
    "Circuit de Monaco": 19,
    "Baku City Circuit": 20,
    "Marina Bay Street Circuit": 23,
    "Yas Marina Circuit": 21,
    "Shanghai International Circuit": 16,
    "Miami International Autodrome": 19,
    "Sakhir": 15,
    "Bahrain International Circuit - Grand Prix": 15,
    "Bahrain International Circuit - Outer": 11,

    # === DIRT OVALS ===
    "The Dirt Track at Charlotte": 4,
    "Lucas Oil Speedway - Dirt Oval": 4,
    "Cedar Lake Speedway": 4,
    "Port Royal Speedway": 4,
    "Lanier National Speedway - Dirt": 4,
    "Bristol Motor Speedway - Dirt": 4,
    "Knoxville Raceway": 4,
    "Eldora Speedway": 4,
    "Volusia Speedway Park": 4,
    "USA International Speedway - Dirt": 4,
    "Federated Auto Parts Raceway at I-55": 4,
    "Lernerville Speedway": 4,
    "Williams Grove Speedway": 4,
    "Kokomo Speedway": 4,
    "Fairbury Speedway": 4,
    "Huset's Speedway": 4,
    "Weedsport Speedway": 4,
    "The Dirt Track at Charlotte Motor Speedway": 4,
    "Lincoln Speedway": 4,
    "Limaland Motorsports Park": 4,
    "Southern National Motorsports Park - Dirt": 4,
    "Lakeland Dirt Oval": 4,
    "Kevin Harvick's Kern Raceway - Dirt Track": 4,
    "DIRTcar Speedway": 4,
    "Oswego Speedway": 4,
    "Irwindale Speedway - Outer - Dirt": 4,
    "East Carolina Motor Speedway - Dirt Oval": 4,
    "Five Flags Speedway - Dirt": 4,
    "Oxford Plains Speedway - Dirt": 4,
    "Daytona International Speedway - Dirt": 4,
    "[Legacy] Lanier National Speedway - 2011 - Dirt": 4,

    # === RALLYCROSS / DIRT ROAD ===
    "Winton Motor Raceway - Rallycross": 6,
    "Daytona Rallycross and Dirt Road - Rallycross Long": 8,
    "Daytona Rallycross and Dirt Road - Rallycross Short": 6,
    "Charlotte Motor Speedway - Rallyx": 5,
    "[Legacy] Phoenix Raceway - 2008 - Rallycross": 5,
    "Brands Hatch Circuit - Rallycross": 5,
    "Lånkebanen (Hell RX) - Hell Rallycross": 7,
    "Sonoma Raceway - Rallycross": 5,
    "Iowa Speedway - Rallycross - 2017": 5,
    "Lucas Oil Indianapolis Raceway Park Rallycross": 5,
    "Lucas Oil Indianapolis Raceway Park - Rallycross": 5,
    "EchoPark Speedway (Atlanta) - Rallycross Short": 5,
    "Crandon International Off-Road Raceway": 4,
    "Crandon International Off-Road Raceway - Short": 4,
    "Bark River International Raceway - Short": 5,
    "Bark River International Raceway": 5,
    "Wild Horse Pass Motorsports Park": 6,
    "Wild West Motorsports Park": 5,
    "Wild West Motorsports Park - Short": 4,
    "Wild Horse Pass Motorsports Park - Short": 5,
    "EchoPark Speedway (Atlanta) - Rallycross": 5,
    "[Legacy] Phoenix Raceway - 2008": 5,
}


def _normalize_track_name(name: str) -> str:
    """Normalize track name for fuzzy matching."""
    name = name.strip()
    # Remove content in parentheses that's just dates/times
    name = re.sub(r"\(\d{4}-\d{2}-\d{2}.*?\)", "", name).strip()
    # Collapse whitespace
    name = re.sub(r"\s+", " ", name)
    return name


def _fuzzy_match(track_name: str, known_tracks: dict[str, int]) -> Optional[int]:
    """Try progressively looser matching, preferring the most specific hit."""
    normalized = _normalize_track_name(track_name)

    # Exact match
    if normalized in known_tracks:
        return known_tracks[normalized]

    # Case-insensitive exact match
    lower_map = {k.lower(): v for k, v in known_tracks.items()}
    if normalized.lower() in lower_map:
        return lower_map[normalized.lower()]

    # Substring match: prefer the longest (most specific) matching key
    input_norm = normalized.lower()
    best_match: Optional[str] = None
    best_len = 0
    for known in known_tracks:
        known_norm = known.lower()
        if known_norm in input_norm or input_norm in known_norm:
            if len(known) > best_len:
                best_match = known
                best_len = len(known)
    if best_match is not None:
        return known_tracks[best_match]

    # Try without trailing config (e.g., "Circuit - Grand Prix" -> "Circuit")
    base = normalized.split(" - ")[0].strip()
    if base != normalized:
        base_lower = base.lower()
        best_match = None
        best_len = 0
        for known in known_tracks:
            known_lower = known.lower()
            if known_lower.startswith(base_lower) or base_lower in known_lower:
                if len(known) > best_len:
                    best_match = known
                    best_len = len(known)
        if best_match is not None:
            return known_tracks[best_match]

    return None


def get_corners_for_track(
    track_name: str,
    category: str,
    api_tracks: Optional[dict[str, int]] = None,
    track_id: Optional[int] = None,
) -> int:
    """Get corner count for a track. Tries track_id, API cache, hardcoded, then default."""
    # Layer 0: Exact lookup by track_id (no ambiguity)
    if track_id is not None:
        corners = _lookup_by_track_id(track_id)
        if corners is not None:
            return corners

    if not track_name:
        return _default_corners(category)

    # Layer 1: API data by name (if available)
    if api_tracks:
        corners = _fuzzy_match(track_name, api_tracks)
        if corners is not None:
            return corners

    # Layer 2: Hardcoded fallback
    corners = _fuzzy_match(track_name, TRACK_CORNERS)
    if corners is not None:
        return corners

    # Layer 3: Category-based estimation
    logger.warning(f"No corner data for '{track_name}', using category default for {category}")
    return _default_corners(category)


def _default_corners(category: str) -> int:
    if category in ("OVAL", "DIRT_OVAL"):
        return DEFAULT_OVAL_CORNERS
    elif category == "DIRT_ROAD":
        return DEFAULT_DIRT_ROAD_CORNERS
    else:
        return DEFAULT_ROAD_CORNERS


# Module-level cache for track_id -> corners lookup
_track_id_cache: Optional[dict[int, int]] = None


def _lookup_by_track_id(track_id: int) -> Optional[int]:
    """Look up corners_per_lap by track_id from the API cache."""
    global _track_id_cache
    if _track_id_cache is None:
        _track_id_cache = _load_track_id_map()
    return _track_id_cache.get(track_id)


def _load_track_id_map() -> dict[int, int]:
    """Build track_id -> corners_per_lap map from the API cache file."""
    cache_file = CACHE_DIR / "tracks.json"
    if not cache_file.exists():
        return {}
    try:
        with open(cache_file) as f:
            data = json.load(f)
        return {
            t["track_id"]: t["corners_per_lap"]
            for t in data
            if t.get("track_id") and t.get("corners_per_lap", 0) > 0
        }
    except (json.JSONDecodeError, KeyError) as e:
        logger.warning(f"Failed to load track ID cache: {e}")
        return {}


def load_api_track_cache() -> Optional[dict[str, int]]:
    """Load cached API track data if available."""
    cache_file = CACHE_DIR / "tracks.json"
    if cache_file.exists():
        try:
            with open(cache_file) as f:
                data = json.load(f)
            track_map = {}
            for track in data:
                name = track.get("track_name", "")
                config = track.get("config_name", "")
                corners = track.get("corners_per_lap", 0)
                if corners and corners > 0:
                    full_name = f"{name} - {config}" if config else name
                    track_map[full_name] = corners
            return track_map
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning(f"Failed to load track cache: {e}")
    return None
