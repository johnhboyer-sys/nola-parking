"""
Step 2: Fetch street centerlines from data.nola.gov (dataset 22q2-dqpb).

Each record includes address ranges (fromleft/toleft/fromright/toright) per
segment, which step 3 uses to match meters by street name + block number.
Outputs data/centerlines.geojson and data/street_base.geojson.

street_base.geojson is the geometry-only red base layer for the UI. It is
filtered to only include streets within ANCHOR_RADIUS_M of a meter or RPP
segment point, so the red layer stays bounded to the FQ/CBD coverage area.
"""

import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BBOX = None  # computed at runtime from meters + RPP extent; set here to override
BBOX_BUFFER = 0.003  # ~300 m padding around data extent

# Streets further than this from any meter or RPP point are excluded from
# the red base layer — keeps coverage within FQ/CBD rather than bleeding
# into surrounding residential neighbourhoods.
ANCHOR_RADIUS_M = 80

M_PER_DEG_LAT = 111_000
M_PER_DEG_LON =  96_200

API_BASE  = "https://data.nola.gov/resource/22q2-dqpb.json"
PAGE_SIZE = 1000
OUTPUT_PATH = Path(__file__).parent.parent / "data" / "centerlines.geojson"


def fetch_page(offset: int, bbox: tuple, retries: int = 3) -> list:
    params = urllib.parse.urlencode({
        "$where":  f"within_box(the_geom,{bbox[0]},{bbox[1]},{bbox[2]},{bbox[3]})",
        "$limit":  PAGE_SIZE,
        "$offset": offset,
        "$order":  "objectid",
    })
    url = f"{API_BASE}?{params}"
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                url,
                headers={"Accept": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode())
        except (urllib.error.URLError, TimeoutError) as exc:
            wait = 5 * (attempt + 1)
            print(f"  Network error ({exc}) — retrying in {wait}s…")
            time.sleep(wait)
    raise RuntimeError(f"API request failed after {retries} attempts (offset={offset})")


def record_to_features(rec: dict) -> list:
    """Expand a MultiLineString record into individual LineString features."""
    geom = rec.get("the_geom")
    if not geom or geom["type"] != "MultiLineString":
        return []

    props = {
        "centerlineid": rec.get("centerlineid", ""),
        "fullname":     rec.get("fullname", ""),
        "fullnameabv":  rec.get("fullnameabv", ""),
        "fromleft":     _int(rec.get("fromleft")),
        "toleft":       _int(rec.get("toleft")),
        "fromright":    _int(rec.get("fromright")),
        "toright":      _int(rec.get("toright")),
        "roadclass":    rec.get("roadclass", ""),
    }

    return [
        {
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": line},
            "properties": props,
        }
        for line in geom["coordinates"]
        if len(line) >= 2
    ]


def _int(val):
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def _load_anchors(data_dir: Path) -> list:
    """Return a flat list of [lon, lat] points from meters and RPP segments."""
    anchors = []
    for filename, geom_key in [("meters.geojson", None), ("rpp_segments.geojson", None)]:
        path = data_dir / filename
        if not path.exists():
            continue
        fc = json.loads(path.read_text())
        for ft in fc["features"]:
            geom = ft["geometry"]
            if geom["type"] == "Point":
                anchors.append(geom["coordinates"])
            elif geom["type"] == "LineString":
                anchors.extend(geom["coordinates"])
            elif geom["type"] == "MultiLineString":
                for ring in geom["coordinates"]:
                    anchors.extend(ring)
    return anchors


def _near_anchor(coords, anchors, r_lat, r_lon, radius_m) -> bool:
    """True if any vertex in coords is within radius_m of any anchor point."""
    for cx, cy in coords:
        for ax, ay in anchors:
            if abs(cx - ax) > r_lon or abs(cy - ay) > r_lat:
                continue
            d = math.hypot((cx - ax) * M_PER_DEG_LON, (cy - ay) * M_PER_DEG_LAT)
            if d <= radius_m:
                return True
    return False


def _data_bbox(data_dir: Path) -> tuple:
    """Derive bounding box from the extent of meters + RPP segments."""
    lons, lats = [], []
    for fname in ("meters.geojson", "rpp_segments.geojson"):
        path = data_dir / fname
        if not path.exists():
            continue
        fc = json.loads(path.read_text())
        for ft in fc["features"]:
            geom = ft["geometry"]
            if geom["type"] == "Point":
                lons.append(geom["coordinates"][0])
                lats.append(geom["coordinates"][1])
            else:
                for ring in (geom["coordinates"] if geom["type"] == "LineString"
                             else sum(geom["coordinates"], [])):
                    coords = ring if isinstance(ring[0], list) else [ring]
                    for seg in coords:
                        pts = seg if isinstance(seg[0], list) else [seg]
                        for pt in pts:
                            lons.append(pt[0])
                            lats.append(pt[1])
    if not lons:
        raise RuntimeError("No meter or RPP data found — run step1 first")
    return (
        min(lats) - BBOX_BUFFER,
        min(lons) - BBOX_BUFFER,
        max(lats) + BBOX_BUFFER,
        max(lons) + BBOX_BUFFER,
    )


def main():
    bbox = BBOX or _data_bbox(OUTPUT_PATH.parent)
    print("Fetching NOLA centerlines from data.nola.gov…")
    print(f"  Bounding box: {bbox}")

    all_features = []
    offset = 0
    while True:
        print(f"  Page offset {offset}…")
        records = fetch_page(offset, bbox)
        if not records:
            break
        for rec in records:
            all_features.extend(record_to_features(rec))
        if len(records) < PAGE_SIZE:
            break
        offset += PAGE_SIZE

    print(f"  {len(all_features)} LineString features from {offset + len(records)} records")

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(
        json.dumps({"type": "FeatureCollection", "features": all_features}),
        encoding="utf-8",
    )
    print(f"  Saved → {OUTPUT_PATH}")

    # Geometry-only copy for the frontend red base layer, clipped to streets
    # within ANCHOR_RADIUS_M of a meter or RPP segment point.
    anchors = _load_anchors(OUTPUT_PATH.parent)  # meters + RPP points
    r_lat = ANCHOR_RADIUS_M / M_PER_DEG_LAT
    r_lon = ANCHOR_RADIUS_M / M_PER_DEG_LON

    base_features = []
    for f in all_features:
        coords = f["geometry"]["coordinates"]
        if _near_anchor(coords, anchors, r_lat, r_lon, ANCHOR_RADIUS_M):
            base_features.append(
                {"type": "Feature", "geometry": f["geometry"], "properties": {}}
            )

    base_path = OUTPUT_PATH.parent / "street_base.geojson"
    base_path.write_text(
        json.dumps({"type": "FeatureCollection", "features": base_features}),
        encoding="utf-8",
    )
    print(f"  street_base: {len(base_features)} of {len(all_features)} streets kept")
    print(f"  Saved → {base_path}")


if __name__ == "__main__":
    main()
