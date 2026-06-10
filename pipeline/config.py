"""
Central configuration for the NOLA Free Parking pipeline.
All geometry distances are in US survey feet (matching LOCAL_CRS units).
"""

from pathlib import Path

# ── Bounding box: French Quarter / CBD ──────────────────────────────────────
BBOX = {
    "xmin": -90.080,
    "ymin":  29.940,
    "xmax": -90.055,
    "ymax":  29.968,
}

# ── Coordinate reference systems ─────────────────────────────────────────────
WGS84     = "EPSG:4326"
LOCAL_CRS = "EPSG:3452"   # NAD83 / Louisiana South — units are US survey feet

# ── Geometry tuning (feet) ───────────────────────────────────────────────────
CURB_OFFSET_FEET       = 9.0   # road centerline → curb face (half lane + gutter)
CAPSULE_BUFFER_FEET    = 3.0   # slop buffer on paid capsules before difference
MIN_GAP_FEET           = 22.0  # shortest useful parking gap (≈ one space)
ONE_SPACE_HALF_LEN_FT  = 11.0  # half-length used to size single-space capsules

# ── Loading-zone DBSCAN (feet) ───────────────────────────────────────────────
DBSCAN_EPS_FEET          = 50.0  # neighbourhood radius ≈ one loading zone
DBSCAN_MIN_SAMPLES       = 3     # minimum citations to form a cluster
LOADING_ZONE_BUFFER_FEET = 30.0  # expand cluster hull before subtracting from gaps

# ── data.nola.gov Socrata API ────────────────────────────────────────────────
NOLA_BASE_URL = "https://data.nola.gov/resource"

# Verify dataset IDs at https://data.nola.gov
# "Street Centerlines" — replace if the portal ID changes
STREET_CENTERLINES_DATASET = "vdeh-g3jq"
# "Parking Violations" — replace if the portal ID changes
PARKING_CITATIONS_DATASET  = "wkzm-7h8b"

SOCRATA_PAGE_LIMIT = 5_000
REQUEST_TIMEOUT    = 30  # seconds per request
REQUEST_HEADERS    = {
    "Accept": "application/json",
    # Uncomment and set SOCRATA_APP_TOKEN env var to raise rate limits:
    # "X-App-Token": os.environ.get("SOCRATA_APP_TOKEN", ""),
}

# ── Violation text patterns (case-insensitive substring match) ───────────────
LOADING_ZONE_KEYWORDS = ["LOADING ZONE", "LOADING  ZONE", "LOAD ZONE"]

# ── File paths ────────────────────────────────────────────────────────────────
DATA_DIR = Path(__file__).parent.parent / "data"

PAID_CAPSULES_FILE    = DATA_DIR / "nola_paid_capsules.geojson"
METERS_FILE           = DATA_DIR / "meters.geojson"
RPP_FILE              = DATA_DIR / "rpp_segments.geojson"

# Intermediate cache files (can be deleted to force re-fetch)
CENTERLINES_CACHE     = DATA_DIR / "cache_centerlines.geojson"
CURB_LINES_FILE       = DATA_DIR / "cache_curb_lines.geojson"
GAPS_FILE             = DATA_DIR / "cache_parking_gaps.geojson"

# Final output
FINAL_OUTPUT_FILE     = DATA_DIR / "nola_free_parking_lanes.geojson"
