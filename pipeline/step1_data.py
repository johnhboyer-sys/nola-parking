"""
Step 1: Fetch citywide meters and RPP segments from the NOLA MapServer.

Source: maps.nola.gov/server/rest/services/Streets/Parking_Revenue/MapServer
  Layer 0 — Metered Parking    → data/meters.geojson
  Layer 1 — RPP Segments       → data/rpp_segments.geojson
"""

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

MAPSERVER = "https://maps.nola.gov/server/rest/services/Streets/Parking_Revenue/MapServer"
PAGE_SIZE  = 1000
DATA_DIR   = Path(__file__).parent.parent / "data"


def fetch_layer(layer_id: int, retries: int = 3) -> list:
    """Fetch all features from an ArcGIS MapServer layer as GeoJSON."""
    features = []
    offset   = 0
    while True:
        params = urllib.parse.urlencode({
            "where":         "1=1",
            "outFields":     "*",
            "f":             "geojson",
            "resultOffset":  offset,
            "resultRecordCount": PAGE_SIZE,
        })
        url = f"{MAPSERVER}/{layer_id}/query?{params}"

        for attempt in range(retries):
            try:
                with urllib.request.urlopen(url, timeout=60) as resp:
                    page = json.loads(resp.read().decode())
                break
            except (urllib.error.URLError, TimeoutError) as exc:
                wait = 5 * (attempt + 1)
                print(f"    Retry in {wait}s ({exc})")
                time.sleep(wait)
        else:
            raise RuntimeError(f"Layer {layer_id} fetch failed at offset {offset}")

        batch = [f for f in page.get("features", []) if f.get("geometry")]
        features.extend(batch)
        print(f"    offset {offset}: +{len(batch)} features")
        if len(batch) < PAGE_SIZE:
            break
        offset += PAGE_SIZE

    return features


def normalise_meters(features: list) -> list:
    """Lowercase property keys to match the existing meters.geojson schema."""
    out = []
    for f in features:
        props = {k.lower(): v for k, v in f["properties"].items()}
        # ArcGIS uses 'Shape.STLength()' etc — drop shape fields
        props = {k: v for k, v in props.items() if not k.startswith("shape")}
        out.append({"type": "Feature", "geometry": f["geometry"], "properties": props})
    return out


def normalise_rpp(features: list) -> list:
    """Lowercase property keys and rename to match existing rpp_segments schema."""
    key_map = {
        "objectid":        "objectid",
        "name":            "name",
        "jidid":           "jidid",
        "jidstlab":        "jidstlab",
        "jidstreet":       "jidstreet",
        "jidfr":           "jidfr",
        "jidto":           "jidto",
        "rppzone":         "rppzone",
    }
    out = []
    for f in features:
        props = {key_map[k.lower()]: v
                 for k, v in f["properties"].items()
                 if k.lower() in key_map}
        out.append({"type": "Feature", "geometry": f["geometry"], "properties": props})
    return out


def save(features: list, path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"type": "FeatureCollection", "features": features}),
        encoding="utf-8",
    )
    print(f"  Saved {len(features)} features → {path}")


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    print("Fetching metered parking (layer 0)…")
    meter_features = fetch_layer(0)
    save(normalise_meters(meter_features), DATA_DIR / "meters.geojson")

    print("Fetching RPP segments (layer 1)…")
    rpp_features = fetch_layer(1)
    save(normalise_rpp(rpp_features), DATA_DIR / "rpp_segments.geojson")


if __name__ == "__main__":
    main()
