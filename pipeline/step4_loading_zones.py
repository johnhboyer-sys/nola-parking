"""
Step 4 — Filter loading zones via citation mining.

Queries the data.nola.gov Socrata API for "Parking in a Loading Zone"
citations within the FQ/CBD bounding box, clusters the citation coordinates
using DBSCAN, and subtracts the resulting loading-zone polygons from the gap
layer produced in Step 3.

Algorithm
─────────
1. Fetch citations filtered by violation text and bounding box.
2. Project citation points to LOCAL_CRS for Euclidean distance clustering.
3. Run DBSCAN (eps=DBSCAN_EPS_FEET, min_samples=DBSCAN_MIN_SAMPLES) to
   identify spatially coherent loading-zone locations.
4. For each cluster: compute the convex hull and buffer it by
   LOADING_ZONE_BUFFER_FEET to get a conservative loading-zone polygon.
5. Subtract the union of loading-zone polygons from the gap segments.
6. Drop any resulting fragments shorter than MIN_GAP_FEET.

If the citations API is unavailable, Step 4 logs a warning and returns the
gaps unchanged so the pipeline can still produce output.

Output: GeoDataFrame of filtered LineString gaps in LOCAL_CRS.
"""

import logging
import time
from typing import Optional

import geopandas as gpd
import numpy as np
import pandas as pd
import requests
from shapely.geometry import MultiPoint, MultiLineString, LineString
from shapely.ops import unary_union
from sklearn.cluster import DBSCAN

from .config import (
    BBOX,
    DBSCAN_EPS_FEET,
    DBSCAN_MIN_SAMPLES,
    LOADING_ZONE_BUFFER_FEET,
    LOADING_ZONE_KEYWORDS,
    LOCAL_CRS,
    MIN_GAP_FEET,
    NOLA_BASE_URL,
    PARKING_CITATIONS_DATASET,
    REQUEST_HEADERS,
    REQUEST_TIMEOUT,
    SOCRATA_PAGE_LIMIT,
    WGS84,
)

log = logging.getLogger(__name__)


# ── API helpers ───────────────────────────────────────────────────────────────

def _request_with_retry(
    session: requests.Session,
    url: str,
    params: dict,
    max_retries: int = 4,
) -> requests.Response:
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


def _build_loading_zone_where(bbox: dict) -> str:
    """
    Compose a Socrata SoQL WHERE clause that selects loading-zone citations
    within the bounding box.
    """
    bbox_filter = (
        f"latitude > {bbox['ymin']} AND latitude < {bbox['ymax']} "
        f"AND longitude > {bbox['xmin']} AND longitude < {bbox['xmax']}"
    )
    # Build an OR chain for each keyword pattern
    kw_conditions = " OR ".join(
        f"upper(violation_desc) like '%{kw}%'" for kw in LOADING_ZONE_KEYWORDS
    )
    return f"({kw_conditions}) AND {bbox_filter}"


def fetch_loading_zone_citations(bbox: dict) -> Optional[gpd.GeoDataFrame]:
    """
    Query the NOLA parking citations dataset for loading-zone violations.

    Returns a GeoDataFrame of Point geometries in WGS84, or None on failure.
    """
    url = f"{NOLA_BASE_URL}/{PARKING_CITATIONS_DATASET}.json"
    where = _build_loading_zone_where(bbox)

    all_records: list = []
    offset = 0

    with requests.Session() as session:
        while True:
            params = {
                "$where":  where,
                "$select": "latitude,longitude,violation_desc,issue_date,location",
                "$limit":  SOCRATA_PAGE_LIMIT,
                "$offset": offset,
            }
            try:
                resp = _request_with_retry(session, url, params)
            except requests.RequestException as exc:
                log.error("Citations API request failed: %s", exc)
                return None

            records = resp.json()
            if not records:
                break

            all_records.extend(records)
            log.debug("Fetched %d citation records (offset=%d)", len(records), offset)

            if len(records) < SOCRATA_PAGE_LIMIT:
                break
            offset += SOCRATA_PAGE_LIMIT

    if not all_records:
        log.warning("No loading-zone citations returned from API")
        return gpd.GeoDataFrame(geometry=[], crs=WGS84)

    df = pd.DataFrame(all_records)

    # ── coerce coordinates ────────────────────────────────────────────────────
    df["latitude"]  = pd.to_numeric(df.get("latitude"),  errors="coerce")
    df["longitude"] = pd.to_numeric(df.get("longitude"), errors="coerce")
    df = df.dropna(subset=["latitude", "longitude"])

    if df.empty:
        log.warning("All citation records lacked valid coordinates")
        return gpd.GeoDataFrame(geometry=[], crs=WGS84)

    from shapely.geometry import Point
    gdf = gpd.GeoDataFrame(
        df,
        geometry=gpd.points_from_xy(df["longitude"], df["latitude"]),
        crs=WGS84,
    )
    log.info("Loaded %d loading-zone citation points", len(gdf))
    return gdf


# ── DBSCAN clustering ─────────────────────────────────────────────────────────

def cluster_citations(citations: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """
    Run DBSCAN on citation points and return loading-zone polygons.

    Each cluster is expanded to a convex hull buffered by
    LOADING_ZONE_BUFFER_FEET to produce a conservative no-park zone.

    Returns a GeoDataFrame of Polygon loading-zone geometries in LOCAL_CRS,
    with cluster_id and citation_count columns.
    """
    proj = citations.to_crs(LOCAL_CRS)
    coords = np.column_stack([proj.geometry.x, proj.geometry.y])

    db = DBSCAN(
        eps=DBSCAN_EPS_FEET,
        min_samples=DBSCAN_MIN_SAMPLES,
        metric="euclidean",
    ).fit(coords)

    labels = db.labels_
    unique_labels = set(labels) - {-1}  # -1 = noise / unclustered points
    log.info(
        "DBSCAN: %d clusters identified from %d citations (%d noise points)",
        len(unique_labels), len(labels), (labels == -1).sum(),
    )

    zone_rows = []
    for label in unique_labels:
        mask = labels == label
        cluster_coords = coords[mask]
        n = int(mask.sum())

        if len(cluster_coords) >= 3:
            hull = MultiPoint([tuple(c) for c in cluster_coords]).convex_hull
        else:
            hull = MultiPoint([tuple(c) for c in cluster_coords]).buffer(LOADING_ZONE_BUFFER_FEET)

        zone = hull.buffer(LOADING_ZONE_BUFFER_FEET)
        zone_rows.append({"geometry": zone, "cluster_id": int(label), "citation_count": n})

    if not zone_rows:
        log.info("No loading-zone clusters found — no zones to subtract")
        return gpd.GeoDataFrame(geometry=[], crs=LOCAL_CRS)

    zones = gpd.GeoDataFrame(zone_rows, crs=LOCAL_CRS)
    log.info("Created %d loading-zone polygons", len(zones))
    return zones


# ── subtraction from gaps ─────────────────────────────────────────────────────

def _subtract_loading_zones(
    gaps: gpd.GeoDataFrame,
    loading_zones: gpd.GeoDataFrame,
) -> gpd.GeoDataFrame:
    """
    Remove loading-zone polygons from gap segments via geometric difference.
    """
    zone_union = unary_union(loading_zones.geometry)
    filtered_rows = []

    for _, seg in gaps.iterrows():
        try:
            result = seg.geometry.difference(zone_union)
        except Exception as exc:
            log.debug("difference() failed on gap: %s", exc)
            filtered_rows.append(seg.to_dict())
            continue

        if result is None or result.is_empty:
            continue

        row_data = seg.drop("geometry").to_dict()

        if isinstance(result, MultiLineString):
            for part in result.geoms:
                if not part.is_empty:
                    filtered_rows.append({**row_data, "geometry": part})
        elif isinstance(result, LineString):
            filtered_rows.append({**row_data, "geometry": result})

    if not filtered_rows:
        return gpd.GeoDataFrame(geometry=[], crs=LOCAL_CRS)

    out = gpd.GeoDataFrame(filtered_rows, crs=LOCAL_CRS)
    out = out[out.geometry.is_valid & ~out.geometry.is_empty].reset_index(drop=True)

    # Refresh length after subtraction
    out["gap_length_ft"] = out.geometry.length
    out = out[out["gap_length_ft"] >= MIN_GAP_FEET].reset_index(drop=True)
    return out


# ── main entry point ──────────────────────────────────────────────────────────

def filter_loading_zones(gaps: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """
    Fetch citation clusters and subtract them from *gaps*.

    Returns the filtered gaps in LOCAL_CRS.  If the API is unavailable,
    logs a warning and returns *gaps* unchanged.
    """
    citations = fetch_loading_zone_citations(BBOX)

    if citations is None:
        log.warning(
            "Citations API unavailable — loading-zone filter skipped. "
            "Output may include loading zones."
        )
        return gaps

    if citations.empty:
        log.info("No loading-zone citations found in area — no zones to subtract")
        return gaps

    loading_zones = cluster_citations(citations)

    if loading_zones.empty:
        return gaps

    before = len(gaps)
    filtered = _subtract_loading_zones(gaps.to_crs(LOCAL_CRS), loading_zones)
    log.info(
        "Loading-zone filter: %d gaps → %d (removed %d short/overlapping segments)",
        before, len(filtered), before - len(filtered),
    )
    return filtered
