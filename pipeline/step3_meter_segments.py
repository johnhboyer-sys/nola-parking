"""
Step 3: Generate ParkMobile-style block-face segments.

Groups meters by ParkMobile zone ID, snaps each group to its nearest OSM
centerline, and extracts the sub-segment spanned by that zone's meter points.
Outputs data/meter_segments.geojson — a FeatureCollection of LineStrings
intended to replace the individual meter dots in the UI.
"""

import json
import math
from pathlib import Path

METERS_PATH      = Path(__file__).parent.parent / "data" / "meters.geojson"
CENTERLINES_PATH = Path(__file__).parent.parent / "data" / "centerlines.geojson"
OUTPUT_PATH      = Path(__file__).parent.parent / "data" / "meter_segments.geojson"

# Approximate metres per degree at lat ~30° N
M_PER_DEG_LAT = 111_000
M_PER_DEG_LON =  96_200

SNAP_RADIUS_M = 60   # ignore centerlines further than this from a zone centroid
MIN_SEG_M     = 15   # minimum rendered segment length (pads single-meter zones)
GAP_SPLIT_M   = 18   # gaps larger than this between consecutive meters → split segment


# ── Geometry helpers ──────────────────────────────────────────────────────────

def dist_m(ax, ay, bx, by):
    return math.hypot((bx - ax) * M_PER_DEG_LON, (by - ay) * M_PER_DEG_LAT)


def point_to_segment(px, py, ax, ay, bx, by):
    """Nearest point on segment a->b; returns (nx, ny, t) with t in [0,1]."""
    dx, dy = bx - ax, by - ay
    seg_sq = dx * dx + dy * dy
    if seg_sq < 1e-18:
        return ax, ay, 0.0
    t = ((px - ax) * dx + (py - ay) * dy) / seg_sq
    t = max(0.0, min(1.0, t))
    return ax + t * dx, ay + t * dy, t


def snap_to_line(px, py, coords):
    """
    Snap point (px,py) to a LineString.
    Returns (seg_idx, t_local, dist_m) — global_t = seg_idx + t_local.
    """
    best_d, best = float("inf"), None
    for i in range(len(coords) - 1):
        ax, ay = coords[i]
        bx, by = coords[i + 1]
        nx, ny, t = point_to_segment(px, py, ax, ay, bx, by)
        d = dist_m(px, py, nx, ny)
        if d < best_d:
            best_d = d
            best = (i, t, d)
    return best  # (seg_idx, t_local, dist_m)


def extract_subline(coords, gt_start, gt_end):
    """
    Extract LineString sub-segment between global_t values gt_start..gt_end.
    global_t = segment_index + t_local (ranges 0 .. len(coords)-1).
    """
    n_segs = len(coords) - 1

    def interp(gt):
        si = min(int(gt), n_segs - 1)
        t  = gt - si
        ax, ay = coords[si]
        bx, by = coords[si + 1]
        return [ax + t * (bx - ax), ay + t * (by - ay)]

    result = [interp(gt_start)]

    lo, hi = int(gt_start) + 1, int(gt_end)
    for i in range(lo, hi + 1):
        if i < len(coords):
            result.append(list(coords[i]))

    ep = interp(gt_end)
    if ep != result[-1]:
        result.append(ep)

    return result


def seg_length_m(coords, gt_start, gt_end):
    """Approximate length in metres of a sub-segment."""
    pts = extract_subline(coords, gt_start, gt_end)
    return sum(dist_m(*pts[i], *pts[i + 1]) for i in range(len(pts) - 1))


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    meters_fc = json.loads(METERS_PATH.read_text())
    cl_fc     = json.loads(CENTERLINES_PATH.read_text())

    centerlines = [
        {
            "osm_id": f["properties"]["osm_id"],
            "name":   f["properties"]["name"],
            "coords": f["geometry"]["coordinates"],
            # Precompute bbox for fast filtering
            "bbox": (
                min(c[0] for c in f["geometry"]["coordinates"]),
                min(c[1] for c in f["geometry"]["coordinates"]),
                max(c[0] for c in f["geometry"]["coordinates"]),
                max(c[1] for c in f["geometry"]["coordinates"]),
            ),
        }
        for f in cl_fc["features"]
        if f["geometry"]["type"] == "LineString"
           and len(f["geometry"]["coordinates"]) >= 2
    ]

    # Group meters by ParkMobile zone ID
    zones: dict[str, list] = {}
    for ft in meters_fc["features"]:
        zid = ft["properties"].get("parkmobile", "")
        if zid:
            zones.setdefault(zid, []).append(ft)

    print(f"Snapping {len(zones)} ParkMobile zones to centerlines…")

    features = []
    skipped  = 0
    r_lat    = SNAP_RADIUS_M / M_PER_DEG_LAT
    r_lon    = SNAP_RADIUS_M / M_PER_DEG_LON

    for zone_id, meters in zones.items():
        lons = [m["geometry"]["coordinates"][0] for m in meters]
        lats = [m["geometry"]["coordinates"][1] for m in meters]
        cx   = sum(lons) / len(lons)
        cy   = sum(lats) / len(lats)

        # Candidates: centerlines whose bbox overlaps the snap search box
        candidates = []
        for cl in centerlines:
            minx, miny, maxx, maxy = cl["bbox"]
            if maxx < cx - r_lon or minx > cx + r_lon:
                continue
            if maxy < cy - r_lat or miny > cy + r_lat:
                continue
            snap = snap_to_line(cx, cy, cl["coords"])
            if snap and snap[2] <= SNAP_RADIUS_M:
                candidates.append((snap[2], cl))

        if not candidates:
            skipped += 1
            continue

        # Nearest centerline wins
        best_cl = min(candidates, key=lambda x: x[0])[1]

        # Project every meter in the zone onto the centerline, keep meter ref
        snapped = []
        for m in meters:
            mx, my = m["geometry"]["coordinates"]
            snap = snap_to_line(mx, my, best_cl["coords"])
            if snap:
                snapped.append((snap[0] + snap[1], m))  # (global_t, meter_feature)

        if not snapped:
            skipped += 1
            continue

        snapped.sort(key=lambda x: x[0])
        n_segs = len(best_cl["coords"]) - 1

        # Split into sub-runs wherever the gap between consecutive projected
        # meters exceeds GAP_SPLIT_M (fire hydrant, driveway, loading zone, etc.)
        runs = []   # each run is a list of (global_t, meter_feature)
        run  = [snapped[0]]
        for prev, curr in zip(snapped, snapped[1:]):
            gap = seg_length_m(best_cl["coords"], prev[0], curr[0])
            if gap > GAP_SPLIT_M:
                runs.append(run)
                run = []
            run.append(curr)
        runs.append(run)

        for run in runs:
            gt_start = run[0][0]
            gt_end   = run[-1][0]

            # Pad short runs (single meter, or tightly clustered pay-station)
            actual_len = seg_length_m(best_cl["coords"], gt_start, gt_end)
            if actual_len < MIN_SEG_M:
                mid_si = min(int((gt_start + gt_end) / 2), n_segs - 1)
                seg_m  = dist_m(*best_cl["coords"][mid_si],
                                *best_cl["coords"][mid_si + 1])
                pad    = (MIN_SEG_M / 2) / max(seg_m, 1)
                gt_start = max(0.0,    gt_start - pad)
                gt_end   = min(n_segs, gt_end   + pad)

            seg_coords = extract_subline(best_cl["coords"], gt_start, gt_end)
            if len(seg_coords) < 2:
                continue

            run_meters   = [item[1] for item in run]
            total_spaces = sum(
                int(m["properties"].get("num_spaces") or 1) for m in run_meters
            )
            p = run_meters[0]["properties"]

            features.append({
                "type": "Feature",
                "geometry": {"type": "LineString", "coordinates": seg_coords},
                "properties": {
                    "parkmobile":  zone_id,
                    "street":      p.get("street", ""),
                    "block":       p.get("block", ""),
                    "side":        p.get("side", ""),
                    "zone":        p.get("zone", ""),
                    "num_spaces":  total_spaces,
                    "meter_type":  p.get("type", ""),
                },
            })

    print(f"  Generated {len(features)} segments  ({skipped} zones skipped — no nearby centerline)")
    OUTPUT_PATH.write_text(
        json.dumps({"type": "FeatureCollection", "features": features}),
        encoding="utf-8",
    )
    print(f"  Saved → {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
