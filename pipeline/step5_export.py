"""
Step 5 — Export clean GeoJSON.

Projects the filtered gap segments back to WGS84, normalises the property
schema, and writes nola_free_parking_lanes.geojson.

Each exported feature includes:
    street          — full street name (title-cased)
    block_number    — nearest hundred block (e.g. "500" for the 500 block)
    block_side      — "left" or "right" relative to centerline direction
    gap_length_ft   — usable curb length in feet (rounded to 1 decimal)
    approx_spaces   — estimated number of standard parking spaces (22 ft each)
"""

import logging
import math

import geopandas as gpd
import pandas as pd

from .config import FINAL_OUTPUT_FILE, LOCAL_CRS, WGS84

log = logging.getLogger(__name__)

_SPACE_LENGTH_FT = 22.0  # standard parallel-parking space length


def _nearest_hundred_block(address_str: str) -> str:
    """
    Return the block label for a given address number string.
    '523' → '500',  '1842' → '1800',  '' / non-numeric → ''
    """
    try:
        num = int(float(address_str))
        return str((num // 100) * 100)
    except (ValueError, TypeError):
        return ""


def _block_number(row: pd.Series) -> str:
    """
    Derive a representative block number from the block_from / block_to range.
    Falls back to the raw block field if present (meters-sourced data).
    """
    # Prefer midpoint of address range
    if row.get("block_from") or row.get("block_to"):
        candidate = row.get("block_from") or row.get("block_to") or ""
        return _nearest_hundred_block(candidate)
    # Fallback: explicit block field from meters data
    if row.get("block"):
        return _nearest_hundred_block(str(row["block"]))
    return ""


def export_free_parking(
    gaps: gpd.GeoDataFrame,
    output_path=FINAL_OUTPUT_FILE,
) -> gpd.GeoDataFrame:
    """
    Write *gaps* to *output_path* as a clean GeoJSON FeatureCollection.

    Returns the exported GeoDataFrame (in WGS84) for inspection.
    """
    if gaps.empty:
        log.warning("No gap segments to export — output file not written")
        return gaps

    work = gaps.to_crs(LOCAL_CRS).copy()
    work["gap_length_ft"] = work.geometry.length  # recompute in projected units

    # ── build clean properties ───────────────────────────────────────────────
    work["street"] = (
        work.get("street", pd.Series("", index=work.index))
        .fillna("")
        .str.strip()
        .str.title()
    )
    work["block_number"] = work.apply(_block_number, axis=1)
    work["block_side"]   = work.get("side", pd.Series("", index=work.index)).fillna("")

    work["gap_length_ft"] = work["gap_length_ft"].round(1)
    work["approx_spaces"] = (work["gap_length_ft"] / _SPACE_LENGTH_FT).apply(math.floor).clip(lower=0)

    # ── project to WGS84 for export ──────────────────────────────────────────
    out = work[["geometry", "street", "block_number", "block_side", "gap_length_ft", "approx_spaces"]]
    out = out.to_crs(WGS84)
    out = out[out.geometry.is_valid & ~out.geometry.is_empty].reset_index(drop=True)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    out.to_file(output_path, driver="GeoJSON")

    total_spaces = int(out["approx_spaces"].sum())
    total_length = out["gap_length_ft"].sum()
    log.info(
        "Exported %d free-parking segments to %s  "
        "(%.0f ft total curb  ≈ %d parking spaces)",
        len(out), output_path, total_length, total_spaces,
    )
    return out
