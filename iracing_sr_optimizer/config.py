import os
from pathlib import Path

# Paths
PROJECT_ROOT = Path(__file__).parent.parent
DATA_DIR = PROJECT_ROOT / "data"
SCHEDULE_JSON = DATA_DIR / "schedule_data.json"
CACHE_DIR = Path.home() / ".iracing_sr_cache"

# iRacing API credentials (from environment)
IRACING_EMAIL = os.environ.get("IRACING_EMAIL", "")
IRACING_PASSWORD = os.environ.get("IRACING_PASSWORD", "")

# OAuth client credentials (required — legacy auth was retired Dec 2025)
# Register at https://oauth.iracing.com/oauth2/book/client_registration.html
IRACING_CLIENT_ID = os.environ.get("IRACING_CLIENT_ID", "")
IRACING_CLIENT_SECRET = os.environ.get("IRACING_CLIENT_SECRET", "")
IRACING_CUST_ID = os.environ.get("IRACING_CUST_ID", "")

# Results cache
RESULTS_CACHE_DIR = DATA_DIR / "results_cache"

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

# SR rolling window estimate (community-approximated)
SR_ROLLING_WINDOW = 2600

# License class ordering for filtering
LICENSE_ORDER = {"R": 0, "D": 1, "C": 2, "B": 3, "A": 4}
