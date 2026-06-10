"""
Step 2: Fetch street centerlines from data.nola.gov (dataset 22q2-dqpb).

Each record includes address ranges (fromleft/toleft/fromright/toright) per
segment, which step 3 uses to match meters by street name + block number.
Outputs data/centerlines.geojson.
"""

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BBOX = (29.927, -90.087, 29.968, -90.055)  # (south, west, north, east)

API_BASE  = "https://data.nola.gov/resource/22q2-dqpb.json"
PAGE_SIZE = 1000
OUTPUT_PATH = Path(__file__).parent.parent / "data" / "centerlines.geojson"


def fetch_page(offset: int, retries: int = 3) -> list:
    params = urllib.parse.urlencode({
        "$where":  f"within_box(the_geom,{BBOX[0]},{BBOX[1]},{BBOX[2]},{BBOX[3]})",
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


def main():
    print("Fetching NOLA centerlines from data.nola.gov…")
    print(f"  Bounding box: {BBOX}")

    all_features = []
    offset = 0
    while True:
        print(f"  Page offset {offset}…")
        records = fetch_page(offset)
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


if __name__ == "__main__":
    main()
