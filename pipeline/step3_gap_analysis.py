"""
Step 3 — Geometric gap analysis.

Subtracts paid-parking capsule polygons from curb-line geometries to isolate
the unmetered stretches of curb.  The result is the set of LineString
segments where no paid meter currently covers the curb face.

Algorithm
─────────
1. Buffer each paid capsule by CAPSULE_BUFFER_FEET to absorb GPS imprecision
   and slight misalignment between the meter database and the curb-line layer.
2. Union all buffered capsules into one dissolved geometry so we need only a
   single difference operation per curb segment (much faster than per-capsule).
3. For each curb segment: gap = curb_line.difference(paid_union).
4. Explode MultiLineString results into individual LineString rows.
5. Drop fragments shorter than MIN_GAP_FEET (too short to park in).

Output: GeoDataFrame of LineString gap segments in LOCAL_CRS, with all
        metadata columns inherited from the curb-lines layer plus gap_length_ft.
"""

import logging

import geopandas as gpd
import pandas as pd
from shapely.geometry import MultiLineString, LineString
from shapely.ops import unary_union

from .config import (
    CAPSULE_BUFFER_FEET,
    CURB_LINES_FILE,
    GAPS_FILE,
    LOCAL_CRS,
    MIN_GAP_FEET,
    WGS84,
)

log = logging.getLogger(__name__)


def _explode_multilinestring(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """
    Expand rows whose geometry is a MultiLineString into one row per part,
    preserving all non-geometry columns.
    """
    expanded = []
    for _, row in gdf.iterrows():
        geom = row.geometry
        if isinstance(geom, MultiLineString):
            for part in geom.geoms:
                new_row = row.copy()
                new_row.geometry = part
                expanded.append(new_row)
        else:
            expanded.append(row)

    if not expanded:
        return gpd.GeoDataFrame(columns=gdf.columns, geometry="geometry", crs=LOCAL_CRS)

    result = gpd.GeoDataFrame(expanded, crs=LOCAL_CRS).reset_index(drop=True)
    return result


def compute_gaps(
    curb_lines: gpd.GeoDataFrame,
    paid_capsules: gpd.GeoDataFrame,
) -> gpd.GeoDataFrame:
    """
    Return the portion of each curb-line segment NOT covered by any paid
    parking capsule, as a GeoDataFrame of LineStrings in LOCAL_CRS.

    Parameters
    ----------
    curb_lines : GeoDataFrame (LOCAL_CRS) — output of step 2
    paid_capsules : GeoDataFrame (LOCAL_CRS) — output of step 1
    """
    # ── ensure same CRS ──────────────────────────────────────────────────────
    curb_lines   = curb_lines.to_crs(LOCAL_CRS)
    paid_capsules = paid_capsules.to_crs(LOCAL_CRS)

    # ── build paid-zone mask ─────────────────────────────────────────────────
    log.info("Buffering %d paid capsules by %.1f ft and unioning ...", len(paid_capsules), CAPSULE_BUFFER_FEET)
    buffered = paid_capsules.geometry.buffer(CAPSULE_BUFFER_FEET)
    paid_union = unary_union(buffered)
    log.info("Paid-zone union complete")

    # ── asymmetric difference: curb minus paid zones ─────────────────────────
    gap_rows = []
    skipped  = 0

    for _, seg in curb_lines.iterrows():
        try:
            gap_geom = seg.geometry.difference(paid_union)
        except Exception as exc:
            log.debug("difference() failed on segment %s: %s", seg.get("centerline_id", "?"), exc)
            skipped += 1
            continue

        if gap_geom is None or gap_geom.is_empty:
            continue

        row_data = seg.drop("geometry").to_dict()

        if isinstance(gap_geom, MultiLineString):
            for part in gap_geom.geoms:
                if not part.is_empty:
                    gap_rows.append({**row_data, "geometry": part})
        elif isinstance(gap_geom, LineString):
            gap_rows.append({**row_data, "geometry": gap_geom})
        # Ignore Point/GeometryCollection edge cases from degenerate intersections

    if skipped:
        log.warning("Skipped %d curb segments due to geometry errors", skipped)

    if not gap_rows:
        log.warning("Gap analysis produced no output — check that capsules and curb lines overlap")
        return gpd.GeoDataFrame(geometry=[], crs=LOCAL_CRS)

    gaps = gpd.GeoDataFrame(gap_rows, crs=LOCAL_CRS)
    gaps = gaps[gaps.geometry.is_valid & ~gaps.geometry.is_empty].reset_index(drop=True)

    # ── drop sub-space fragments ─────────────────────────────────────────────
    gaps["gap_length_ft"] = gaps.geometry.length
    before = len(gaps)
    gaps = gaps[gaps["gap_length_ft"] >= MIN_GAP_FEET].reset_index(drop=True)
    log.info(
        "Gap analysis: %d raw gaps → %d after removing fragments < %.0f ft  (dropped %d)",
        before, len(gaps), MIN_GAP_FEET, before - len(gaps),
    )

    return gaps


def run_gap_analysis(
    curb_lines: gpd.GeoDataFrame,
    paid_capsules: gpd.GeoDataFrame,
    force: bool = False,
) -> gpd.GeoDataFrame:
    """
    Compute gaps and cache the result to GAPS_FILE.
    Pass force=True to recompute even if the cache exists.
    """
    if not force and GAPS_FILE.exists():
        log.info("Loading gap cache from %s", GAPS_FILE.name)
        return gpd.read_file(GAPS_FILE).to_crs(LOCAL_CRS)

    gaps = compute_gaps(curb_lines, paid_capsules)

    if not gaps.empty:
        gaps.to_crs(WGS84).to_file(GAPS_FILE, driver="GeoJSON")
        log.info("Gaps cached to %s", GAPS_FILE.name)

    return gaps
