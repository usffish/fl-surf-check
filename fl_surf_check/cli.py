"""
Command-line entry point for fl-surf-check.

    python -m fl_surf_check --zip 32118
"""

from __future__ import annotations

import argparse
import concurrent.futures
import sys

import requests

from .climatology import load_baseline
from .conditions import Conditions, fetch_marine_and_wind, fetch_tide
from .distance import closeness_factor, get_drive_estimate
from .location import GeocodeError, geocode_zip
from .scoring import (
    MINUTES_PER_SIGMA,
    drive_allowance_minutes,
    effective_drive,
    rarity_score,
    score_conditions,
    storm_blocks_travel,
    storm_risk,
    storm_warning,
    worth_the_drive,
)
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
    parser.add_argument("--minutes-per-sd", type=float, default=MINUTES_PER_SIGMA,
                        help=f"Extra driving time you'll accept per standard deviation "
                             f"above normal conditions (default: {MINUTES_PER_SIGMA:g} min). "
                             f"Set 0 to rank on raw drive time only.")
    parser.add_argument("--rare-only", action="store_true",
                        help="Show only spots having an unusually good day for the time of "
                             "year, judged against ~5 years of statewide history.")
    parser.add_argument("--no-history", action="store_true",
                        help="Skip the statewide historical baseline (no rarity column).")
    parser.add_argument("--refresh-history", action="store_true",
                        help="Force-rebuild the cached historical baselines.")
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
        # Key by (station, tz): NOAA returns times in the station's own local
        # time, so the timezone is part of what identifies a tide lookup.
        stations = sorted({(s.tide_station, s.tz) for s in spots})
        tide_by_station = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            futures = {
                pool.submit(fetch_tide, st, tz, session): (st, tz)
                for st, tz in stations
            }
            for fut in concurrent.futures.as_completed(futures):
                tide_by_station[futures[fut]] = fut.result()
        for spot in spots:
            state, label = tide_by_station.get((spot.tide_station, spot.tz), (None, None))
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

    # ONE statewide seasonal baseline, shared by every spot. Cached on disk for
    # 30 days: the history request is ~1.1M samples and running it per-invocation
    # trips Open-Meteo's rate limit, which then starves the live conditions
    # request in the same run and drops every spot to its no-data floor.
    baseline = None
    if not args.no_history:
        baseline = load_baseline(spots, force_refresh=args.refresh_history)

    rows = []
    for spot in spots:
        cond = conditions.get(spot.name, Conditions())
        drive = drives[spot.name]
        surf = score_conditions(cond, spot)
        rare = rarity_score(cond, baseline)

        # Exceptional conditions buy extra drive time: every geometric SD above
        # normal is worth --minutes-per-sd more driving. The discount is applied
        # to the drive BEFORE distance decay, so a far spot on a rare day
        # competes with a close spot on an ordinary one.
        allowance = drive_allowance_minutes(
            rare.sigma, rare.n_days, minutes_per_sigma=args.minutes_per_sd
        )
        # Great surf buys extra driving - but never toward a lightning storm.
        risk = storm_risk(cond)
        if storm_blocks_travel(risk):
            allowance = 0.0
        eff_miles, eff_minutes = effective_drive(
            drive.distance_miles, drive.duration_minutes, allowance
        )

        close = closeness_factor(eff_miles, args.decay_miles)
        worth = worth_the_drive(surf.total, eff_miles, close, args.surf_weight)
        rows.append({
            "spot": spot, "conditions": cond, "drive": drive,
            "surf": surf, "worth": worth, "rarity": rare, "storm": risk,
            "allowance": allowance, "eff_miles": eff_miles, "eff_minutes": eff_minutes,
        })
    return rows


def filter_and_sort(rows, args):
    out = rows
    if args.max_miles is not None:
        out = [r for r in out if r["drive"].distance_miles <= args.max_miles]
    if args.min_score is not None:
        out = [r for r in out if r["surf"].total >= args.min_score]
    if getattr(args, "rare_only", False):
        out = [r for r in out if r["rarity"].is_standout]
    out.sort(key=lambda r: r["worth"].total, reverse=True)
    if args.top and args.top > 0:
        out = out[: args.top]
    return out


def _month_name() -> str:
    import datetime as _dt
    return _dt.date.today().strftime("%B")


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
    storms = [r for r in rows if r["storm"] == "active"]
    if storms:
        lines.append("  " + "!" * 92)
        lines.append(
            f"  LIGHTNING NOW at {len(storms)} spot(s): "
            + ", ".join(r["spot"].name for r in storms[:4])
            + ("..." if len(storms) > 4 else "")
        )
        lines.append("  Rain is fine to surf in. Lightning is not - sit these out.")
        lines.append("  " + "!" * 92)

    standouts = [r for r in rows if r["rarity"].is_standout]
    if standouts:
        lines.append("  " + "=" * 92)
        best = max(standouts, key=lambda r: r["rarity"].percentile)
        lines.append(
            f"  UNUSUALLY GOOD TODAY: {len(standouts)} spot(s) well above the Florida norm "
            f"for {_month_name()}. Best is {best['spot'].name}."
        )
        lines.append(f"  baseline: {best['rarity'].baseline_summary}")
        for r in standouts:
            rr = r["rarity"]
            lines.append(
                f"     {r['spot'].name[:34]:<36} p{rr.percentile:<3.0f} vs all of FL "
                f"in {_month_name()}   [{rr.label()}]"
            )
        lines.append("  " + "=" * 92)

    lines.append("  " + "-" * 92)
    lines.append(
        f"  {'#':<3}{'SPOT':<30}{'SURF':>6}{'WORTH':>7}{'MILES':>7}{'DRIVE':>8}"
        f"{'VS NORM':>12}{'BONUS':>8}   VERDICT"
    )
    lines.append("  " + "-" * 92)

    for i, r in enumerate(rows, 1):
        spot, drive, surf, worth = r["spot"], r["drive"], r["surf"], r["worth"]
        name = spot.name if len(spot.name) <= 28 else spot.name[:25] + "..."
        approx = "~" if drive.is_estimate else " "
        rare = r["rarity"]
        rare_cell = f"p{rare.percentile:.0f}" if rare.percentile is not None else "-"
        if rare.sigma is not None and rare.percentile is not None:
            rare_cell += f" {rare.sigma:+.1f}s"
        bonus = r["allowance"]
        bonus_cell = f"-{_fmt_time(bonus)}" if bonus >= 1 else "-"
        flag = rare.label()

        # An active thunderstorm overrides the verdict outright. Leaving "GO.
        # Drop everything" next to a lightning warning is worse than useless -
        # the two lines contradict each other and the wrong one is louder.
        verdict = worth.verdict
        if r["storm"] == "active":
            verdict = "LIGHTNING - do not paddle out"
        elif r["storm"] == "likely":
            verdict = f"{worth.verdict} (storms likely)"
        lines.append(
            f"  {i:<3}{name[:28]:<30}"
            f"{surf.total:>5.1f} {worth.total:>6.1f} "
            f"{approx}{drive.distance_miles:>5.0f} "
            f"{_fmt_time(drive.duration_minutes):>8}"
            f"{rare_cell:>12}{bonus_cell:>8}   {verdict}"
            + (f"  [{flag}]" if flag and r["storm"] != "active" else "")
        )

        if args.details:
            c = r["conditions"]
            wave = f"{c.wave_height_ft:.1f}ft" if c.wave_height_ft is not None else "n/a"
            per = f"{c.wave_period_s:.0f}s" if c.wave_period_s is not None else "n/a"
            wind = (f"{c.wind_speed_mph:.0f}mph @ {c.wind_direction_deg:.0f}deg"
                    if c.wind_speed_mph is not None else "n/a")
            lines.append(f"      [{_bar(surf.total)}]  {surf.breakdown()}")
            sw = (f"swell {c.swell_height_ft:.1f}ft/{c.swell_period_s:.0f}s"
                  if c.swell_height_ft is not None and c.swell_period_s is not None else "")
            if rare.percentile is not None:
                lines.append(
                    f"      vs history: height p{rare.height_percentile:.0f}, "
                    f"period p{rare.period_percentile:.0f}  ->  p{rare.percentile:.0f} overall "
                    f"({rare.n_days}d over {rare.n_years}yr)   {sw}"
                )
            warn = storm_warning(r["storm"])
            if warn:
                extra = ""
                if c.cape_j_kg is not None:
                    extra = f"  (CAPE {c.cape_j_kg:.0f} J/kg"
                    if c.precip_probability is not None:
                        extra += f", {c.precip_probability:.0f}% precip"
                    extra += ")"
                lines.append(f"      (!) {warn}{extra}")
            if r["allowance"] >= 1:
                lines.append(
                    f"      {rare.sigma:+.1f} SD above normal -> worth "
                    f"{_fmt_time(r['allowance'])} extra driving; "
                    f"{_fmt_time(drive.duration_minutes)} scored as "
                    f"{_fmt_time(r['eff_minutes'])}"
                )
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
    if any(r["rarity"].percentile is not None for r in rows):
        lines.append(
            "  VS NORM = percentile (and geometric SDs) against ALL Florida spots' swell "
            "history for this time of year;"
        )
        lines.append(
            f"            BONUS = drive time earned at {args.minutes_per_sd:g} min per SD "
            f"above normal, discounted before ranking."
        )
        lines.append(
            "            (marine record starts Oct 2021; thin baselines shrink toward normal.)"
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
        if args.rare_only:
            print("\n  No spot is having an unusually good day for this time of year.\n"
                  "  Run without --rare-only to see the best of an ordinary day.\n")
        else:
            print("\n  No spots matched your filters. Try relaxing --max-miles or --min-score.\n")
        return 0

    print(render(rows, origin, args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
