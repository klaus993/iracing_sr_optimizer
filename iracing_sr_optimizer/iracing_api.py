"""iRacing Data API client using OAuth password_limited grant."""

from __future__ import annotations

import hashlib
import json
import logging
import time
import urllib.parse
import urllib.request
from typing import Optional

from iracingdataapi import irDataClient

from .config import (
    CACHE_DIR,
    IRACING_CLIENT_ID,
    IRACING_CLIENT_SECRET,
    IRACING_EMAIL,
    IRACING_PASSWORD,
)

logger = logging.getLogger(__name__)

IRACING_TOKEN_URL = "https://oauth.iracing.com/oauth2/token"
TOKEN_CACHE_FILE = CACHE_DIR / "oauth_token.json"


class IRacingAPIError(Exception):
    pass


def _mask_secret(secret: str, identifier: str) -> str:
    """Mask a secret using iRacing's masking algorithm.

    SHA-256 hash of secret + normalized identifier, then base64 encoded.
    Used for both password (identifier=username) and client_secret (identifier=client_id).
    See https://oauth.iracing.com/oauth2/book/token_endpoint.html
    """
    import base64
    normalized_id = identifier.strip().lower()
    combined = f"{secret}{normalized_id}"
    return base64.b64encode(
        hashlib.sha256(combined.encode("utf-8")).digest()
    ).decode("utf-8")


def _get_oauth_token(force_refresh: bool = False) -> str:
    """Get access token via OAuth password_limited grant.

    Raises IRacingAPIError if credentials are missing or auth fails.
    """
    if not IRACING_CLIENT_ID or not IRACING_CLIENT_SECRET:
        raise IRacingAPIError(
            "Missing OAuth credentials. Set IRACING_CLIENT_ID and IRACING_CLIENT_SECRET.\n"
            "Register at https://oauth.iracing.com/oauth2/book/client_registration.html\n"
            "Legacy cookie-based auth was retired by iRacing on Dec 9, 2025."
        )

    if not IRACING_EMAIL or not IRACING_PASSWORD:
        raise IRacingAPIError(
            "Missing iRacing credentials. Set IRACING_EMAIL and IRACING_PASSWORD."
        )

    # Check cached token
    if not force_refresh and TOKEN_CACHE_FILE.exists():
        try:
            with open(TOKEN_CACHE_FILE) as f:
                cached = json.load(f)
            if cached.get("expires_at", 0) > time.time() + 30:
                logger.info("Using cached OAuth token")
                return cached["access_token"]
        except (json.JSONDecodeError, KeyError):
            pass

    # Mask password and client_secret per iRacing's masking algorithm
    masked_pw = _mask_secret(IRACING_PASSWORD, IRACING_EMAIL)
    masked_secret = _mask_secret(IRACING_CLIENT_SECRET, IRACING_CLIENT_ID)

    data = urllib.parse.urlencode({
        "grant_type": "password_limited",
        "username": IRACING_EMAIL,
        "password": masked_pw,
        "client_id": IRACING_CLIENT_ID,
        "client_secret": masked_secret,
        "scope": "iracing.auth",
    }).encode()

    req = urllib.request.Request(
        IRACING_TOKEN_URL,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            token_data = json.loads(resp.read())
    except Exception as e:
        raise IRacingAPIError(f"OAuth token request failed: {e}")

    access_token = token_data.get("access_token")
    if not access_token:
        raise IRacingAPIError(f"No access_token in OAuth response: {token_data}")

    # Cache token with expiry
    expires_in = token_data.get("expires_in", 600)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with open(TOKEN_CACHE_FILE, "w") as f:
        json.dump({
            "access_token": access_token,
            "expires_at": time.time() + expires_in,
        }, f)

    logger.info("Got new OAuth access token")
    return access_token


def get_client() -> irDataClient:
    """Create an authenticated iRacing API client via OAuth password_limited grant."""
    token = _get_oauth_token()
    try:
        logger.info("Authenticating via OAuth password_limited grant")
        return irDataClient(access_token=token)
    except Exception as e:
        raise IRacingAPIError(f"Failed to create client with OAuth token: {e}")


def refresh_client() -> irDataClient:
    """Create a client with a freshly fetched OAuth token (bypass cache)."""
    token = _get_oauth_token(force_refresh=True)
    try:
        logger.info("Refreshing OAuth token and creating new client")
        return irDataClient(access_token=token)
    except Exception as e:
        raise IRacingAPIError(f"Failed to create client with refreshed OAuth token: {e}")


def fetch_tracks(use_cache: bool = True) -> Optional[dict[str, int]]:
    """Fetch track data from iRacing API. Returns track_name -> corners_per_lap map."""
    cache_file = CACHE_DIR / "tracks.json"

    # Try cache first
    if use_cache and cache_file.exists():
        try:
            with open(cache_file) as f:
                tracks = json.load(f)
            logger.info(f"Loaded {len(tracks)} tracks from cache")
            return _build_track_map(tracks)
        except (json.JSONDecodeError, KeyError):
            logger.warning("Cache file corrupt, fetching from API")

    # Fetch from API
    try:
        client = get_client()
        tracks = client.get_tracks()
        tracks_data = [_to_dict(t) for t in tracks]

        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        with open(cache_file, "w") as f:
            json.dump(tracks_data, f, indent=2)
        logger.info(f"Fetched and cached {len(tracks_data)} tracks from API")

        return _build_track_map(tracks_data)
    except IRacingAPIError:
        raise
    except Exception as e:
        logger.warning(f"API fetch failed: {e}")
        return None


def _to_dict(obj) -> dict:
    """Convert a pydantic model or dict to a plain dict."""
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    if hasattr(obj, "dict"):
        return obj.dict()
    return dict(obj)


def _build_track_map(tracks: list[dict]) -> dict[str, int]:
    """Build track name -> corners per lap mapping from API data."""
    track_map = {}
    for track in tracks:
        name = track.get("track_name", "")
        config = track.get("config_name", "")
        corners = track.get("corners_per_lap", 0)
        if corners and corners > 0:
            full_name = f"{name} - {config}" if config else name
            track_map[full_name] = corners
    return track_map
