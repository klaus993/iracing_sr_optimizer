import os
from pathlib import Path

# Paths
PROJECT_ROOT = Path(__file__).parent.parent
DATA_DIR = PROJECT_ROOT / "data"
SCHEDULE_JSON = DATA_DIR / "schedule_data.json"
CACHE_DIR = Path.home() / ".iracing_sr_cache"

# Invisible characters that frequently sneak into credentials via copy-paste or
# files saved with a BOM. They survive a print() unchanged (so the value "looks"
# correct when echoed) but make iRacing's OAuth endpoint reject the request with
# invalid_request "invalid character at index 0". Note str.strip() does NOT
# remove the BOM, so we strip these explicitly. Defined by codepoint to keep the
# source pure-ASCII and reviewable:
#   FEFF BOM/zero-width no-break  200B zero-width space  200C/200D zero-width
#   (non-)joiner  00A0 non-breaking space
_INVISIBLE_EDGE_CHARS = "".join(
    chr(c) for c in (0xFEFF, 0x200B, 0x200C, 0x200D, 0x00A0)
) + " \t\r\n"


def _strip_edges(value: str) -> str:
    return value.strip().strip(_INVISIBLE_EDGE_CHARS).strip()


def _clean_credential(name: str) -> str:
    """Read an env var and normalize it for use in the OAuth request.

    Removes surrounding whitespace + invisible edge chars, then strips one layer
    of matching surrounding quotes. Quote-wrapping is a common mistake — the
    shell or a .env loader captures the quotes into the value (e.g. the value is
    literally "your@email.com", quotes included). The quotes survive a print()
    so the value looks right when echoed, but iRacing's OAuth endpoint rejects
    the request with invalid_request "invalid character at index 0".
    """
    value = _strip_edges(os.environ.get(name, ""))
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("\"", "'"):
        value = _strip_edges(value[1:-1])
    return value


# iRacing API credentials (from environment)
IRACING_EMAIL = _clean_credential("IRACING_EMAIL")
IRACING_PASSWORD = _clean_credential("IRACING_PASSWORD")

# OAuth client credentials (required — legacy auth was retired Dec 2025)
# Register at https://oauth.iracing.com/oauth2/book/client_registration.html
IRACING_CLIENT_ID = _clean_credential("IRACING_CLIENT_ID")
IRACING_CLIENT_SECRET = _clean_credential("IRACING_CLIENT_SECRET")

# SR calculation defaults
DEFAULT_OVAL_CORNERS = 4
DEFAULT_ROAD_CORNERS = 12
DEFAULT_DIRT_ROAD_CORNERS = 8

# Lap time estimates (minutes) for time-based races
LAP_TIME_ESTIMATES = {
    "OVAL": 0.5,        # short ovals ~30s, superspeedways ~45s
    "SPORTS_CAR": 2.0,  # road courses ~2 min average
    "FORMULA_CAR": 1.5, # formula cars are faster
    "DIRT_OVAL": 0.4,   # dirt ovals are short
    "DIRT_ROAD": 1.0,   # rallycross ~1 min
}

# Categories
CATEGORIES = ["OVAL", "SPORTS_CAR", "FORMULA_CAR", "DIRT_OVAL", "DIRT_ROAD"]

# Dirt categories get 2x SR advantage (heavy contact = 2x instead of 4x)
DIRT_CATEGORIES = {"DIRT_OVAL", "DIRT_ROAD"}

# License class ordering for filtering
LICENSE_ORDER = {"R": 0, "D": 1, "C": 2, "B": 3, "A": 4}
