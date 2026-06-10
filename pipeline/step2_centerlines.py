"""
Step 2: Fetch street centerlines via Overpass API (OpenStreetMap).

Replaces the defunct data.nola.gov endpoint (dataset vdeh-g3jq, HTTP 404).
Outputs data/centerlines.geojson — a FeatureCollection of LineString features
covering the French Quarter / CBD bounding box.
"""

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

# FQ/CBD bounding box derived from the meter data extent plus a small buffer.
# Overpass bbox order: (south, west, north, east)
BBOX = (29.927, -90.087, 29.968, -90.055)

OVERPASS_URL = "https://overpass-api.de/api/interpreter"

# Street types to include — excludes footways, cycleways, construction, etc.
HIGHWAY_FILTER = (
    "residential|primary|secondary|tertiary|unclassified|"
    "living_street|service|trunk|motorway|"
    "motorway_link|trunk_link|primary_link|secondary_link|tertiary_link"
)

OVERPASS_QUERY = f"""
[out:json][timeout:90];
(
  way["highway"~"^({HIGHWAY_FILTER})$"]
  ({BBOX[0]},{BBOX[1]},{BBOX[2]},{BBOX[3]});
);
out geom;
""".strip()

OUTPUT_PATH = Path(__file__).parent.parent / "data" / "centerlines.geojson"


def fetch_overpass(query: str, retries: int = 3, backoff: float = 5.0) -> dict:
    data = ("data=" + urllib.parse.quote(query)).encode()
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                OVERPASS_URL,
                data=data,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "User-Agent": "NOParking-pipeline/1.0 (github NOParking)",
                },
            )
            with urllib.request.urlopen(req, timeout=120) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            if exc.code == 429 or exc.code >= 500:
                wait = backoff * (attempt + 1)
                print(f"  HTTP {exc.code} — retrying in {wait:.0f}s…")
                time.sleep(wait)
            else:
                raise
        except (urllib.error.URLError, TimeoutError) as exc:
            wait = backoff * (attempt + 1)
            print(f"  Network error ({exc}) — retrying in {wait:.0f}s…")
            time.sleep(wait)
    raise RuntimeError(f"Overpass query failed after {retries} attempts")


def way_to_feature(element: dict) -> Optional[dict]:
    """Convert an Overpass way element (with inline geometry) to GeoJSON Feature."""
    if "geometry" not in element:
        return None
    coords = [[pt["lon"], pt["lat"]] for pt in element["geometry"]]
    if len(coords) < 2:
        return None
    tags = element.get("tags", {})
    return {
        "type": "Feature",
        "geometry": {"type": "LineString", "coordinates": coords},
        "properties": {
            "osm_id": element["id"],
            "name": tags.get("name", ""),
            "highway": tags.get("highway", ""),
            "oneway": tags.get("oneway", ""),
            "lanes": tags.get("lanes", ""),
            "surface": tags.get("surface", ""),
            "maxspeed": tags.get("maxspeed", ""),
        },
    }


def main():
    print("Querying Overpass API for street centerlines…")
    print(f"  Bounding box: {BBOX}")
    result = fetch_overpass(OVERPASS_QUERY)

    elements = result.get("elements", [])
    ways = [e for e in elements if e.get("type") == "way"]
    print(f"  Received {len(ways)} way elements")

    features = [f for f in (way_to_feature(w) for w in ways) if f is not None]
    print(f"  Converted {len(features)} features")

    geojson = {"type": "FeatureCollection", "features": features}

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(geojson), encoding="utf-8")
    print(f"  Saved → {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
