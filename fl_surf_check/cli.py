"""
Command-line entry point for fl-surf-check.

    python -m fl_surf_check --zip 32118
"""

from __future__ import annotations

import argparse
import concurrent.futures
import os
import sys

import requests

from .climatology import is_daylight, load_baseline
from .conditions import (
    MAX_FORECAST_DAYS,
    Conditions,
    fetch_marine_and_wind,
    fetch_tide,
)
from .distance import closeness_factor, get_drive_estimate
from .location import GeocodeError, geocode_zip
from .scoring import (
    MINUTES_PER_SIGMA,
    drive_allowance_minutes,
    effective_drive,
    effective_sigma,
    pick_best_hour,
    rarity_score,
    score_conditions,
    value_score,
    storm_blocks_travel,
    storm_risk,
    storm_warning,
    worth_the_drive,
)
from .spots import SPOTS

#: Environment variable holding a default origin, so the common case is just
#: `fl-surf-check` with no arguments.
ZIP_ENV_VAR = "FL_SURF_ZIP"

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
    parser.add_argument("--zip", "-z", dest="zip_code", default=None,
                        help="Your 5-digit US zip code (the drive origin). Defaults to "
                             f"the {ZIP_ENV_VAR} environment variable if set, so you can "
                             "export it once instead of typing it every run.")
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
    parser.add_argument("--days", "-d", type=int, default=1,
                        help="How many days ahead to consider (1-7, default: 1). "
                             "Each spot is scored on its best surfable hour anywhere "
                             "in the window, and BEST shows which day that is.")
    parser.add_argument("--minutes-per-sd", type=float, default=MINUTES_PER_SIGMA,
                        help=f"Minutes of driving that one standard deviation of surf "
                             f"is worth (default: {MINUTES_PER_SIGMA:g}). This is the "
                             f"exchange rate in the value score.")
    parser.add_argument("--worth-only", action="store_true",
                        help="Show only spots with a positive value score, i.e. where the "
                             "surf actually justifies the drive.")
    parser.add_argument("--rare-only", action="store_true",
                        help="Show only spots having an unusually good day, judged against "
                             "the full statewide history.")
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
    conditions = fetch_marine_and_wind(spots, hours_ahead=args.days * 24)

    session = requests.Session()

    # Tides: one request per unique NOAA station (not per spot).
    if not args.no_tides:
        # Key by (station, tz): NOAA returns times in the station's own local
        # time, so the timezone is part of what identifies a tide lookup.
        stations = sorted({(s.tide_station, s.tz) for s in spots})
        tide_by_station = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            futures = {
                pool.submit(fetch_tide, st, tz, session, args.days * 24 + 24): (st, tz)
                for st, tz in stations
            }
            for fut in concurrent.futures.as_completed(futures):
                tide_by_station[futures[fut]] = fut.result()
        for spot in spots:
            state, label = tide_by_station.get((spot.tide_station, spot.tz), (None, None))
            for _, c in conditions.get(spot.name, []):
                c.tide_state = state
                c.next_tide = label

    # Drive times, in parallel (OSRM demo server, one route per spot).
    drives = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        futures = {
            pool.submit(get_drive_estimate, origin.lat, origin.lon, s.lat, s.lon, 8.0, session): s
            for s in spots
        }
        for fut in concurrent.futures.as_completed(futures):
            drives[futures[fut].name] = fut.result()

    # ONE statewide baseline, pooled across the full record and shared by every
    # spot - not windowed to any time of year. Cached on disk for 30 days: the
    # history request is several million samples and running it per-invocation
    # trips Open-Meteo's rate limit, which then starves the live conditions
    # request in the same run and drops every spot to its no-data floor.
    baseline = None
    if not args.no_history:
        baseline = load_baseline(spots, force_refresh=args.refresh_history)

    def _local_date(ts, spot):
        import datetime as _dt
        from zoneinfo import ZoneInfo
        return _dt.datetime.fromtimestamp(ts, ZoneInfo(spot.tz)).date()

    def _daylight(ts, lat, lon):
        import numpy as _np
        return bool(is_daylight(_np.array([float(ts)]), lat, lon)[0])

    rows = []
    for spot in spots:
        readings = conditions.get(spot.name) or [(0, Conditions())]
        drive = drives[spot.name]
        # Score every surfable hour in the window and take the best, rather
        # than whichever hour the tool happened to be run in.
        pick = pick_best_hour(readings, spot, baseline, _daylight, _local_date)
        cond, surf, rare = pick.conditions, pick.surf, pick.rarity

        # value = sigma(surf) - drive_minutes / minutes_per_sigma. Both terms in
        # the same units, so the number has a natural zero: positive means the
        # surf is worth the trip.
        risk = storm_risk(cond)
        # Swell rarity alone ignores wind, which the historical record does not
        # carry - fold today's wind in before pricing the drive.
        sigma = pick.sigma
        if sigma is not None and storm_blocks_travel(risk):
            # No amount of swell justifies driving toward lightning.
            sigma = min(sigma, 0.0)

        value = value_score(sigma, drive.duration_minutes,
                            minutes_per_sigma=args.minutes_per_sd)

        # Legacy blend, kept only for --no-history where there is no sigma.
        close = closeness_factor(drive.distance_miles, args.decay_miles)
        worth = worth_the_drive(surf.total, drive.distance_miles, close,
                                args.surf_weight)

        rows.append({
            "spot": spot, "conditions": cond, "drive": drive,
            "surf": surf, "worth": worth, "rarity": rare, "storm": risk,
            "value": value, "best_hour": pick,
        })
    return rows


def filter_and_sort(rows, args):
    out = rows
    if args.max_miles is not None:
        out = [r for r in out if r["drive"].distance_miles <= args.max_miles]
    if args.min_score is not None:
        out = [r for r in out if r["surf"].total >= args.min_score]
    if getattr(args, "worth_only", False):
        out = [r for r in out if r["value"].worth_it]
    if getattr(args, "rare_only", False):
        out = [r for r in out if r["rarity"].is_standout]
    # Rank on the value score; fall back to the legacy blend only when no
    # baseline is available (--no-history) and every value is None.
    if any(r["value"].total is not None for r in out):
        out.sort(key=lambda r: (r["value"].total is not None, r["value"].total),
                 reverse=True)
    else:
        out.sort(key=lambda r: r["worth"].total, reverse=True)
    if args.top and args.top > 0:
        out = out[: args.top]
    return out


def _day_summary(rows, args) -> list[str]:
    """
    Best spot for each day in the window.

    With 41 spots over several days a full matrix is unreadable, and the
    question a multi-day run is actually asking is "which day, and where" -
    so this collapses to one line per day.
    """
    import datetime as _dt
    from zoneinfo import ZoneInfo

    by_day: dict = {}
    for r in rows:
        pick = r["best_hour"]
        if not pick or r["value"].total is None:
            continue
        cost = r["value"].drive_cost
        for day, entry in (pick.by_day or {}).items():
            if entry.sigma is None:
                continue
            val = entry.sigma - cost
            if day not in by_day or val > by_day[day][0]:
                local = _dt.datetime.fromtimestamp(entry.time, ZoneInfo(r["spot"].tz))
                by_day[day] = (val, r, local, entry)

    if len(by_day) < 2:
        return []

    out = ["  " + "-" * 92, f"  BEST DAY OF THE NEXT {args.days}", "  " + "-" * 92]
    best_key = max(by_day, key=lambda k: by_day[k][0])
    for key in sorted(by_day):
        val, r, local, entry = by_day[key]
        mark = "->" if key == best_key else "  "
        out.append(
            f"  {mark} {local:%a %d %b}  {val:>+6.2f}  {r['spot'].name[:26]:<28}"
            f"{local:%H:%M}  swell "
            + (f"{entry.conditions.swell_height_ft:.1f}ft/"
               f"{entry.conditions.swell_period_s:.0f}s"
               if entry.conditions.swell_height_ft is not None else "n/a")
        )
    if args.days >= 4:
        out.append(
            "     (days 4+ are directional - swell autocorrelation is 0.26 at 4 days"
        )
        out.append(
            "      and 0.21 at 5, so treat the far end as a plan, not a promise.)"
        )
    out.append("")
    return out


def _local_hhmm(row) -> str:
    """Local clock time of the hour being reported for this spot."""
    import datetime as _dt
    from zoneinfo import ZoneInfo
    ts = row["best_hour"].time
    if not ts:
        return "-"
    return _dt.datetime.fromtimestamp(ts, ZoneInfo(row["spot"].tz)).strftime("%a %H:%M")


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
    if getattr(args, "days", 1) > 1:
        lines.extend(_day_summary(rows, args))

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
            f"  UNUSUALLY GOOD TODAY: {len(standouts)} spot(s) well above the Florida norm. "
            f"Best is {best['spot'].name}."
        )
        lines.append(f"  baseline: {best['rarity'].baseline_summary}")
        for r in standouts:
            rr = r["rarity"]
            lines.append(
                f"     {r['spot'].name[:34]:<36} p{rr.percentile:<3.0f} vs all of FL, "
                f"any time of year   [{rr.label()}]"
            )
        lines.append("  " + "=" * 92)

    lines.append("  " + "-" * 92)
    lines.append(
        f"  {'#':<3}{'SPOT':<26}{'VALUE':>7}{'SURF':>6}{'BEST':>12}{'DRIVE':>8}"
        f"{'COST':>7}{'VS NORM':>12}   VERDICT"
    )
    lines.append("  " + "-" * 92)

    for i, r in enumerate(rows, 1):
        spot, drive, surf, worth = r["spot"], r["drive"], r["surf"], r["worth"]
        name = spot.name if len(spot.name) <= 24 else spot.name[:21] + "..."
        approx = "~" if drive.is_estimate else " "
        rare, val = r["rarity"], r["value"]
        rare_cell = f"p{rare.percentile:.0f}" if rare.percentile is not None else "-"
        if rare.sigma is not None and rare.percentile is not None:
            rare_cell += f" {rare.sigma:+.1f}s"
        val_cell = f"{val.total:+.2f}" if val.total is not None else "-"
        flag = rare.label()

        # An active thunderstorm overrides the verdict outright. Leaving "GO.
        # Drop everything" next to a lightning warning is worse than useless -
        # the two lines contradict each other and the wrong one is louder.
        verdict = val.verdict if val.total is not None else worth.verdict
        if r["storm"] == "active":
            verdict = "LIGHTNING - do not paddle out"
        elif r["storm"] == "likely":
            verdict = f"{verdict} (storms likely)"
        lines.append(
            f"  {i:<3}{name[:24]:<26}"
            f"{val_cell:>7}{surf.total:>6.1f}{_local_hhmm(r):>12}"
            f"{approx}{_fmt_time(drive.duration_minutes):>7}"
            f"{-val.drive_cost:>7.2f}"
            f"{rare_cell:>12}   {verdict}"
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
            if val.total is not None:
                margin = val.margin_minutes()
                verb = "still worth it with" if margin >= 0 else "short by"
                lines.append(
                    f"      value = {val.sigma:+.2f} SD - "
                    f"{_fmt_time(drive.duration_minutes)}/{args.minutes_per_sd:g}min "
                    f"= {val.total:+.2f}   ({verb} {_fmt_time(abs(margin))} of driving)"
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
        f"  BEST = the best surfable hour in the next {args.days * 24}h; all figures "
        f"are for that hour."
    )
    lines.append(
        f"  VALUE = surf (in SDs above normal) minus drive time, at "
        f"{args.minutes_per_sd:g} min per SD.  Positive = worth going."
    )
    if any(r["rarity"].percentile is not None for r in rows):
        lines.append(
            "  VS NORM = percentile (and geometric SDs) against ALL Florida spots' swell "
            "history, pooled across the full record;"
        )
        lines.append(
            f"            COST = the drive in those same units. VALUE = VS NORM sigma + COST."
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

    if not 1 <= args.days <= MAX_FORECAST_DAYS:
        print(f"error: --days must be between 1 and {MAX_FORECAST_DAYS} "
              f"(the marine model has no usable swell beyond that)", file=sys.stderr)
        return 2
    if not 0.0 <= args.surf_weight <= 1.0:
        print("error: --surf-weight must be between 0 and 1", file=sys.stderr)
        return 2

    zip_code = args.zip_code or os.environ.get(ZIP_ENV_VAR)
    if not zip_code:
        print(f"error: no zip code given. Pass --zip, or set {ZIP_ENV_VAR}:\n"
              f"    export {ZIP_ENV_VAR}=33613", file=sys.stderr)
        return 2

    try:
        origin = geocode_zip(zip_code)
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
        if args.worth_only:
            print("\n  Nothing is worth the drive right now - no spot's surf covers "
                  "the time to get there.\n"
                  "  Run without --worth-only to see the least-bad options.\n")
        elif args.rare_only:
            print("\n  No spot is having an unusually good day right now.\n"
                  "  Run without --rare-only to see the best of an ordinary day.\n")
        else:
            print("\n  No spots matched your filters. Try relaxing --max-miles or --min-score.\n")
        return 0

    print(render(rows, origin, args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
