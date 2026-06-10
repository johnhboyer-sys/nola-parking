"""
Step 2 — Ingest road centerlines and generate curb lines.

Fetches official NOLA Street Centerlines from the data.nola.gov Socrata API,
filters to the FQ/CBD bounding box, then derives left-side and right-side curb
lines by applying a parallel offset of CURB_OFFSET_FEET to each segment.

Cached on disk at CENTERLINES_CACHE / CURB_LINES_FILE so repeated runs avoid
hitting the API.  Delete those files to force a fresh fetch.

Output: GeoDataFrame of LineString curb segments in LOCAL_CRS, with columns:
    street, block_from, block_to, side, centerline_id
"""

import logging
import time
from typing import Optional

import geopandas as gpd
import pandas as pd
import requests
from shapely.geometry import MultiLineString, LineString

from .config import (
    BBOX,
    CENTERLINES_CACHE,
    CURB_LINES_FILE,
    CURB_OFFSET_FEET,
    LOCAL_CRS,
    NOLA_BASE_URL,
    REQUEST_HEADERS,
    REQUEST_TIMEOUT,
    SOCRATA_PAGE_LIMIT,
    STREET_CENTERLINES_DATASET,
    WGS84,
)

log = logging.getLogger(__name__)

# ── helpers ───────────────────────────────────────────────────────────────────

def _find_col(gdf: gpd.GeoDataFrame, *candidates: str) -> Optional[str]:
    """Return the first matching column name (case-insensitive), or None."""
    lower_map = {c.lower(): c for c in gdf.columns}
    for name in candidates:
        if name.lower() in lower_map:
            return lower_map[name.lower()]
    return None


def _request_with_retry(
    session: requests.Session,
    url: str,
    params: dict,
    max_retries: int = 4,
) -> requests.Response:
    """GET with exponential-backoff retry on transient errors."""
    for attempt in range(max_retries):
        try:
            resp = session.get(url, params=params, headers=REQUEST_HEADERS, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            return resp
        except requests.RequestException as exc:
            if attempt == max_retries - 1:
                raise
            wait = 2 ** attempt
            log.warning("Request failed (%s) — retrying in %ds", exc, wait)
            time.sleep(wait)
    raise RuntimeError("Unreachable")  # pragma: no cover


# ── API fetch ─────────────────────────────────────────────────────────────────

def fetch_centerlines_from_api(bbox: dict) -> gpd.GeoDataFrame:
    """
    Fetch street centerlines from the Socrata .geojson endpoint with pagination.

    Falls back to a client-side bounding-box clip if the server-side
    within_box filter is not supported for LineString geometry columns.
    """
    url = f"{NOLA_BASE_URL}/{STREET_CENTERLINES_DATASET}.geojson"
    # within_box(col, top_lat, left_lon, bottom_lat, right_lon)
    where = (
        f"within_box(the_geom, {bbox['ymax']}, {bbox['xmin']}, "
        f"{bbox['ymin']}, {bbox['xmax']})"
    )

    all_features: list = []
    offset = 0

    with requests.Session() as session:
        while True:
            params = {"$where": where, "$limit": SOCRATA_PAGE_LIMIT, "$offset": offset}
            try:
                resp = _request_with_retry(session, url, params)
            except requests.RequestException as exc:
                log.error("Centerlines API request failed: %s", exc)
                break

            data = resp.json()
            features = data.get("features", data) if isinstance(data, dict) else data
            if not features:
                break

            all_features.extend(features)
            log.debug("Fetched %d centerline features (offset=%d)", len(features), offset)

            if len(features) < SOCRATA_PAGE_LIMIT:
                break
            offset += SOCRATA_PAGE_LIMIT

    if not all_features:
        log.warning("No centerline features returned from API")
        return gpd.GeoDataFrame(geometry=[], crs=WGS84)

    if len(all_features) % SOCRATA_PAGE_LIMIT == 0:
        log.warning(
            "Response hit page limit (%d) — dataset may be truncated. "
            "Consider fetching without spatial filter and clipping locally.",
            SOCRATA_PAGE_LIMIT,
        )

    collection = {"type": "FeatureCollection", "features": all_features}
    gdf = gpd.GeoDataFrame.from_features(collection, crs=WGS84)
    log.info("Fetched %d centerline segments from API", len(gdf))
    return gdf


def _clip_to_bbox(gdf: gpd.GeoDataFrame, bbox: dict) -> gpd.GeoDataFrame:
    """Client-side clip to bounding box using the .cx accessor."""
    return gdf.cx[bbox["xmin"]:bbox["xmax"], bbox["ymin"]:bbox["ymax"]].copy()


# ── curb-line generation ──────────────────────────────────────────────────────

def _offset_line(line: LineString, distance_ft: float) -> Optional[LineString]:
    """
    Return a parallel-offset copy of *line* at *distance_ft* feet.
    Positive = left of the line's travel direction; negative = right.
    Returns None for degenerate results.
    """
    try:
        result = line.offset_curve(distance_ft)  # Shapely >= 2.0
    except Exception:
        try:
            side = "left" if distance_ft >= 0 else "right"
            result = line.parallel_offset(abs(distance_ft), side)  # Shapely 1.x
        except Exception as exc:
            log.debug("offset_curve failed for line: %s", exc)
            return None

    if result is None or result.is_empty:
        return None
    # offset_curve can return a MultiLineString for self-intersecting input
    if isinstance(result, MultiLineString):
        geoms = [g for g in result.geoms if not g.is_empty]
        return geoms[0] if len(geoms) == 1 else result
    return result


def generate_curb_lines(centerlines_gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """
    Derive left-side and right-side curb LineStrings from road centerlines.

    Each input segment produces two output rows:
      • side="left"  — offset +CURB_OFFSET_FEET (left of digitizing direction)
      • side="right" — offset -CURB_OFFSET_FEET

    Preserves street name and address-range block metadata.
    """
    proj = centerlines_gdf.to_crs(LOCAL_CRS).copy()
    proj = proj[proj.geometry.geom_type.isin(["LineString", "MultiLineString"])]
    proj = proj[proj.geometry.is_valid & ~proj.geometry.is_empty]

    street_col   = _find_col(proj, "fullname", "full_name", "streetname", "name", "street_nam")
    lf_add_col   = _find_col(proj, "l_f_add", "lfadd", "from_left")
    lt_add_col   = _find_col(proj, "l_t_add", "ltadd", "to_left")
    rf_add_col   = _find_col(proj, "r_f_add", "rfadd", "from_right")
    rt_add_col   = _find_col(proj, "r_t_add", "rtadd", "to_right")
    id_col       = _find_col(proj, "objectid", "id", "gid", "fid")

    def _addr(row, col):
        return str(int(float(row[col]))) if col and col in row.index and pd.notna(row[col]) else ""

    rows = []
    for _, seg in proj.iterrows():
        geom = seg.geometry

        # Flatten MultiLineString → individual LineStrings
        parts = list(geom.geoms) if isinstance(geom, MultiLineString) else [geom]

        street = str(seg[street_col]).strip().title() if street_col else ""
        seg_id = str(seg[id_col]) if id_col else ""

        for part in parts:
            for side, dist in (("left", CURB_OFFSET_FEET), ("right", -CURB_OFFSET_FEET)):
                curb = _offset_line(part, dist)
                if curb is None:
                    continue

                # Pick the address range for this side
                if side == "left":
                    block_from = _addr(seg, lf_add_col)
                    block_to   = _addr(seg, lt_add_col)
                else:
                    block_from = _addr(seg, rf_add_col)
                    block_to   = _addr(seg, rt_add_col)

                rows.append({
                    "geometry":       curb,
                    "street":         street,
                    "block_from":     block_from,
                    "block_to":       block_to,
                    "side":           side,
                    "centerline_id":  seg_id,
                })

    if not rows:
        log.warning("generate_curb_lines produced no output — check centerline geometries")
        return gpd.GeoDataFrame(
            columns=["geometry", "street", "block_from", "block_to", "side", "centerline_id"],
            geometry="geometry",
            crs=LOCAL_CRS,
        )

    result = gpd.GeoDataFrame(rows, crs=LOCAL_CRS)
    result = result[result.geometry.is_valid & ~result.geometry.is_empty].reset_index(drop=True)
    log.info("Generated %d curb-line segments (%d streets)", len(result), result["street"].nunique())
    return result


# ── main entry point ──────────────────────────────────────────────────────────

def ingest_road_centerlines(force_fetch: bool = False) -> gpd.GeoDataFrame:
    """
    Return curb-line GeoDataFrame in LOCAL_CRS.

    Uses CURB_LINES_FILE cache when available.  Pass force_fetch=True to
    bypass both the curb-lines cache and the raw centerlines cache.
    """
    if not force_fetch and CURB_LINES_FILE.exists():
        log.info("Loading curb lines from cache %s", CURB_LINES_FILE.name)
        return gpd.read_file(CURB_LINES_FILE).to_crs(LOCAL_CRS)

    # ── raw centerlines ──────────────────────────────────────────────────────
    if not force_fetch and CENTERLINES_CACHE.exists():
        log.info("Loading raw centerlines from cache %s", CENTERLINES_CACHE.name)
        raw = gpd.read_file(CENTERLINES_CACHE)
    else:
        log.info("Fetching centerlines from data.nola.gov ...")
        raw = fetch_centerlines_from_api(BBOX)

        if raw.empty:
            raise RuntimeError(
                "Could not fetch centerlines from the API and no local cache exists. "
                f"Download the NOLA Street Centerlines dataset manually and save it to "
                f"{CENTERLINES_CACHE}."
            )

        raw.to_file(CENTERLINES_CACHE, driver="GeoJSON")
        log.info("Centerlines cached to %s", CENTERLINES_CACHE.name)

    # Clip in case the API returned features outside the bbox
    raw = _clip_to_bbox(raw.to_crs(WGS84), BBOX).to_crs(LOCAL_CRS)
    log.info("Centerlines after bbox clip: %d segments", len(raw))

    # ── generate curb offsets ────────────────────────────────────────────────
    curb_lines = generate_curb_lines(raw)

    curb_lines.to_crs(WGS84).to_file(CURB_LINES_FILE, driver="GeoJSON")
    log.info("Curb lines cached to %s", CURB_LINES_FILE.name)
    return curb_lines
