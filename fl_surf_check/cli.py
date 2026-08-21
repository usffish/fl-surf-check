"""
Command-line entry point for fl-surf-check.

    python -m fl_surf_check --zip 32118
"""

from __future__ import annotations

import argparse
import concurrent.futures
import sys

import requests

from .conditions import Conditions, fetch_marine_and_wind, fetch_tide
from .distance import closeness_factor, get_drive_estimate
from .location import GeocodeError, geocode_zip
from .scoring import score_conditions, worth_the_drive
from .spots import SPOTS

DEFAULT_DECAY_MILES = 75.0
DEFAULT_SURF_WEIGHT = 0.7


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fl-surf-check",
        description="Rate Florida surf spots out of 10 and work out which are worth driving to.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  fl-surf-check --zip 32118
  fl-surf-check --zip 33139 --top 5
  fl-surf-check --zip 32118 --decay-miles 150   # willing to drive further
  fl-surf-check --zip 32118 --surf-weight 0.9   # care mostly about quality
  fl-surf-check --zip 32118 --max-miles 100     # hard cap on distance
  fl-surf-check --zip 32118 --details           # show the score breakdown
""",
    )
    parser.add_argument("--zip", "-z", required=True, dest="zip_code",
                        help="Your 5-digit US zip code (the drive origin).")
    parser.add_argument("--top", "-n", type=int, default=10,
                        help="How many spots to show (default: 10, use 0 for all).")
    parser.add_argument("--decay-miles", type=float, default=DEFAULT_DECAY_MILES,
                        help=f"Distance at which 'closeness' drops to ~37%%; think of it as how "
                             f"far you're comfortable driving (default: {DEFAULT_DECAY_MILES:g}).")
    parser.add_argument("--surf-weight", type=float, default=DEFAULT_SURF_WEIGHT,
                        help=f"0-1. How much conditions matter vs. distance. 1.0 ignores "
                             f"distance entirely (default: {DEFAULT_SURF_WEIGHT:g}).")
    parser.add_argument("--max-miles", type=float, default=None,
                        help="Hard-exclude any spot further than this many miles.")
    parser.add_argument("--min-score", type=float, default=None,
                        help="Hard-exclude any spot whose surf score is below this.")
    parser.add_argument("--details", action="store_true",
                        help="Show the per-factor score breakdown and raw conditions.")
    parser.add_argument("--no-tides", action="store_true",
                        help="Skip NOAA tide lookups (faster; tide is a minor scoring input).")
    return parser


def gather(origin, args):
    """Fetch conditions, tides and drive times for every spot."""
    spots = list(SPOTS)

    # One batched request each for waves and wind, covering all spots.
    conditions = fetch_marine_and_wind(spots)

    session = requests.Session()

    # Tides: one request per unique NOAA station (not per spot).
    if not args.no_tides:
        stations = sorted({s.tide_station for s in spots})
        tide_by_station = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            futures = {pool.submit(fetch_tide, st, session): st for st in stations}
            for fut in concurrent.futures.as_completed(futures):
                tide_by_station[futures[fut]] = fut.result()
        for spot in spots:
            state, label = tide_by_station.get(spot.tide_station, (None, None))
            conditions[spot.name].tide_state = state
            conditions[spot.name].next_tide = label

    # Drive times, in parallel (OSRM demo server, one route per spot).
    drives = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        futures = {
            pool.submit(get_drive_estimate, origin.lat, origin.lon, s.lat, s.lon, 8.0, session): s
            for s in spots
        }
        for fut in concurrent.futures.as_completed(futures):
            drives[futures[fut].name] = fut.result()

    rows = []
    for spot in spots:
        cond = conditions.get(spot.name, Conditions())
        drive = drives[spot.name]
        surf = score_conditions(cond, spot)
        close = closeness_factor(drive.distance_miles, args.decay_miles)
        worth = worth_the_drive(surf.total, drive.distance_miles, close, args.surf_weight)
        rows.append({
            "spot": spot, "conditions": cond, "drive": drive,
            "surf": surf, "worth": worth,
        })
    return rows


def filter_and_sort(rows, args):
    out = rows
    if args.max_miles is not None:
        out = [r for r in out if r["drive"].distance_miles <= args.max_miles]
    if args.min_score is not None:
        out = [r for r in out if r["surf"].total >= args.min_score]
    out.sort(key=lambda r: r["worth"].total, reverse=True)
    if args.top and args.top > 0:
        out = out[: args.top]
    return out


def _fmt_time(minutes: float) -> str:
    h, m = divmod(int(round(minutes)), 60)
    return f"{h}h{m:02d}m" if h else f"{m}m"


def _bar(score: float, width: int = 10) -> str:
    filled = int(round((score / 10.0) * width))
    return "#" * filled + "." * (width - filled)


def render(rows, origin, args) -> str:
    lines = []
    lines.append("")
    lines.append(f"  Florida surf check  -  from {origin.label}")
    lines.append(f"  {len(rows)} spots shown, ranked by whether they're worth the drive")
    lines.append("  " + "-" * 92)
    lines.append(
        f"  {'#':<3}{'SPOT':<38}{'SURF':>6}{'WORTH':>7}{'MILES':>8}{'DRIVE':>8}   VERDICT"
    )
    lines.append("  " + "-" * 92)

    for i, r in enumerate(rows, 1):
        spot, drive, surf, worth = r["spot"], r["drive"], r["surf"], r["worth"]
        name = spot.name if len(spot.name) <= 36 else spot.name[:33] + "..."
        approx = "~" if drive.is_estimate else " "
        lines.append(
            f"  {i:<3}{name:<38}"
            f"{surf.total:>5.1f} {worth.total:>6.1f} "
            f"{approx}{drive.distance_miles:>6.0f} "
            f"{_fmt_time(drive.duration_minutes):>7}   {worth.verdict}"
        )

        if args.details:
            c = r["conditions"]
            wave = f"{c.wave_height_ft:.1f}ft" if c.wave_height_ft is not None else "n/a"
            per = f"{c.wave_period_s:.0f}s" if c.wave_period_s is not None else "n/a"
            wind = (f"{c.wind_speed_mph:.0f}mph @ {c.wind_direction_deg:.0f}deg"
                    if c.wind_speed_mph is not None else "n/a")
            lines.append(f"      [{_bar(surf.total)}]  {surf.breakdown()}")
            lines.append(f"      waves {wave} / {per}   wind {wind}"
                         + (f"   tide {c.next_tide}" if c.next_tide else ""))
            if surf.confidence != "full":
                lines.append(f"      (!) incomplete data - confidence: {surf.confidence}")
            if c.errors:
                lines.append(f"      (!) {'; '.join(c.errors)}")
            lines.append("")

    lines.append("  " + "-" * 92)
    lines.append(
        f"  SURF = conditions out of 10.  WORTH = conditions blended with distance "
        f"({args.surf_weight:.0%} conditions)."
    )
    if any(r["drive"].is_estimate for r in rows):
        lines.append("  ~ = straight-line distance estimate (routing server unreachable).")
    lines.append("")
    return "\n".join(lines)


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    if not 0.0 <= args.surf_weight <= 1.0:
        print("error: --surf-weight must be between 0 and 1", file=sys.stderr)
        return 2

    try:
        origin = geocode_zip(args.zip_code)
    except GeocodeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"  Checking {len(SPOTS)} Florida spots...", file=sys.stderr)

    try:
        rows = gather(origin, args)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130

    rows = filter_and_sort(rows, args)
    if not rows:
        print("\n  No spots matched your filters. Try relaxing --max-miles or --min-score.\n")
        return 0

    print(render(rows, origin, args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
