"""
Step 1 — Ingest paid parking capsules.

Priority order:
  1. Load nola_paid_capsules.geojson if it exists (high-fidelity source, e.g.
     exported from ParkMobile / city API).
  2. Generate approximate capsules from the local meters.geojson fallback by
     buffering each meter point proportional to its reported space count.

Output: GeoDataFrame of Polygon capsules in LOCAL_CRS.
"""

import logging

import geopandas as gpd
import pandas as pd

from .config import (
    LOCAL_CRS,
    METERS_FILE,
    ONE_SPACE_HALF_LEN_FT,
    PAID_CAPSULES_FILE,
    WGS84,
)

log = logging.getLogger(__name__)


def _generate_capsules_from_meters(meters_gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """
    Build circular-buffer capsules from meter point data.

    Each meter's buffer radius = num_spaces × ONE_SPACE_HALF_LEN_FT feet,
    which gives a circle whose diameter ≈ the expected curb run for that
    meter.  The result is only an approximation; replace with high-fidelity
    capsule polygons from the city API when available.
    """
    proj = meters_gdf.to_crs(LOCAL_CRS).copy()

    num_spaces = (
        pd.to_numeric(proj.get("num_spaces", pd.Series(1, index=proj.index)), errors="coerce")
        .fillna(1)
        .clip(lower=1)
    )
    radii = num_spaces * ONE_SPACE_HALF_LEN_FT

    capsules = proj.geometry.buffer(radii)

    result = gpd.GeoDataFrame(
        {
            "street":     proj.get("street", pd.Series("", index=proj.index)),
            "block":      proj.get("block",  pd.Series("", index=proj.index)),
            "side":       proj.get("side",   pd.Series("U", index=proj.index)),
            "parkmobile": proj.get("parkmobile", pd.Series("", index=proj.index)),
            "source":     "meters_buffer",
        },
        geometry=capsules,
        crs=LOCAL_CRS,
    )
    result = result[~result.geometry.is_empty].reset_index(drop=True)
    log.debug("Generated %d capsules from meter points", len(result))
    return result


def ingest_paid_parking() -> gpd.GeoDataFrame:
    """
    Return paid-parking capsule polygons in LOCAL_CRS.

    Tries PAID_CAPSULES_FILE first; falls back to generating capsules from
    METERS_FILE.  Raises FileNotFoundError if neither source is available.
    """
    if PAID_CAPSULES_FILE.exists():
        log.info("Loading high-fidelity paid capsules from %s", PAID_CAPSULES_FILE)
        gdf = gpd.read_file(PAID_CAPSULES_FILE).to_crs(LOCAL_CRS)
        gdf["source"] = "capsules_file"
    elif METERS_FILE.exists():
        log.warning(
            "%s not found — generating approximate capsules from %s",
            PAID_CAPSULES_FILE.name,
            METERS_FILE.name,
        )
        meters = gpd.read_file(METERS_FILE)
        gdf = _generate_capsules_from_meters(meters)
    else:
        raise FileNotFoundError(
            f"No paid-parking data found. "
            f"Expected {PAID_CAPSULES_FILE} or {METERS_FILE}."
        )

    if gdf.crs is None:
        gdf = gdf.set_crs(WGS84).to_crs(LOCAL_CRS)

    gdf = gdf[gdf.geometry.is_valid & ~gdf.geometry.is_empty].reset_index(drop=True)
    log.info("Paid-parking capsules ready: %d polygons", len(gdf))
    return gdf
