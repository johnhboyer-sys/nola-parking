"""
NOLA Free Parking Pipeline — main entry point.

Usage
-----
    python -m pipeline.run_pipeline [--force] [--skip-loading-zones] [--verbose]

Flags
-----
    --force              Ignore all intermediate caches; re-fetch and recompute
                         every step from scratch.
    --skip-loading-zones Skip Step 4 (useful when the citations API is down or
                         for a fast dry run).
    --verbose            Set log level to DEBUG.

Pipeline steps
--------------
    Step 1  Ingest paid parking capsules (from file or generated from meters)
    Step 2  Fetch road centerlines, generate left/right curb lines
    Step 3  Subtract paid zones from curb lines → raw gaps
    Step 4  Fetch citation clusters, subtract loading zones → filtered gaps
    Step 5  Export nola_free_parking_lanes.geojson
"""

import argparse
import logging
import sys
import time

from .step1_paid_parking  import ingest_paid_parking
from .step2_centerlines   import ingest_road_centerlines
from .step3_gap_analysis  import run_gap_analysis
from .step4_loading_zones import filter_loading_zones
from .step5_export        import export_free_parking
from .config              import FINAL_OUTPUT_FILE


def _configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )
    # Suppress overly chatty third-party loggers at non-debug level
    if not verbose:
        for noisy in ("fiona", "pyogrio", "urllib3", "requests"):
            logging.getLogger(noisy).setLevel(logging.WARNING)


def _elapsed(start: float) -> str:
    secs = time.perf_counter() - start
    return f"{secs:.1f}s"


def main(force: bool = False, skip_loading_zones: bool = False, verbose: bool = False) -> None:
    _configure_logging(verbose)
    log = logging.getLogger(__name__)
    pipeline_start = time.perf_counter()

    log.info("=" * 60)
    log.info("NOLA Free Parking Pipeline starting")
    log.info("=" * 60)

    # ── Step 1: paid parking capsules ────────────────────────────────────────
    t = time.perf_counter()
    log.info("[Step 1/5] Ingesting paid-parking capsules ...")
    try:
        paid_capsules = ingest_paid_parking()
    except FileNotFoundError as exc:
        log.error("Step 1 failed: %s", exc)
        sys.exit(1)
    log.info("[Step 1/5] Done  (%s)  —  %d capsules", _elapsed(t), len(paid_capsules))

    # ── Step 2: road centerlines + curb lines ────────────────────────────────
    t = time.perf_counter()
    log.info("[Step 2/5] Ingesting road centerlines and generating curb offsets ...")
    try:
        curb_lines = ingest_road_centerlines(force_fetch=force)
    except RuntimeError as exc:
        log.error("Step 2 failed: %s", exc)
        sys.exit(1)
    log.info("[Step 2/5] Done  (%s)  —  %d curb segments", _elapsed(t), len(curb_lines))

    if curb_lines.empty:
        log.error("No curb lines produced — cannot continue.  "
                  "Check API connectivity or provide a local centerlines cache.")
        sys.exit(1)

    # ── Step 3: gap analysis ─────────────────────────────────────────────────
    t = time.perf_counter()
    log.info("[Step 3/5] Computing geometric gaps (curb minus paid zones) ...")
    gaps = run_gap_analysis(curb_lines, paid_capsules, force=force)
    log.info("[Step 3/5] Done  (%s)  —  %d raw gap segments", _elapsed(t), len(gaps))

    if gaps.empty:
        log.warning("Gap analysis returned no segments — nothing to export.")
        sys.exit(0)

    # ── Step 4: loading-zone filter ──────────────────────────────────────────
    if skip_loading_zones:
        log.info("[Step 4/5] Skipped (--skip-loading-zones)")
    else:
        t = time.perf_counter()
        log.info("[Step 4/5] Filtering loading zones via citation mining ...")
        gaps = filter_loading_zones(gaps)
        log.info("[Step 4/5] Done  (%s)  —  %d gaps after filter", _elapsed(t), len(gaps))

    # ── Step 5: export ───────────────────────────────────────────────────────
    t = time.perf_counter()
    log.info("[Step 5/5] Exporting clean GeoJSON ...")
    result = export_free_parking(gaps, output_path=FINAL_OUTPUT_FILE)
    log.info("[Step 5/5] Done  (%s)", _elapsed(t))

    log.info("=" * 60)
    log.info(
        "Pipeline complete in %s  —  %d free-parking segments  "
        "(≈ %d spaces, %.0f ft of curb)",
        _elapsed(pipeline_start),
        len(result),
        int(result["approx_spaces"].sum()),
        result["gap_length_ft"].sum(),
    )
    log.info("Output: %s", FINAL_OUTPUT_FILE)
    log.info("=" * 60)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="NOLA Free Parking Pipeline")
    parser.add_argument("--force",              action="store_true", help="Bypass all caches")
    parser.add_argument("--skip-loading-zones", action="store_true", help="Skip Step 4")
    parser.add_argument("--verbose",            action="store_true", help="Debug logging")
    args = parser.parse_args()
    main(
        force=args.force,
        skip_loading_zones=args.skip_loading_zones,
        verbose=args.verbose,
    )
