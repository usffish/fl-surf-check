"""
Local web UI for fl-surf-check.

Run with:
    python -m fl_surf_check.webapp
then open http://127.0.0.1:5050

This is a thin wrapper around the exact same pipeline the CLI uses
(build_parser -> geocode_zip -> gather -> filter_and_sort -> compute_verdict),
called through the `cli` module rather than importing individual names, so
there is one scoring path and two front ends - never a second copy of the
ranking or verdict logic that could quietly drift from the terminal output.
"""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode

from flask import Flask, redirect, render_template, request, url_for

from . import cli
from .location import GeocodeError
from .spots import SPOTS

app = Flask(__name__)

# Query-string keys the form submits, mapped to CLI flags. Deliberately a
# subset: the legacy blend (--decay-miles/--surf-weight), --details, hard
# cutoffs (--max-miles/--min-score) and personal-log tuning stay CLI-only for
# now - this covers the everyday "where and when" question.
FORM_DEFAULTS = {
    "zip": "",
    "days": "1",
    "top": "10",
    "minutes_per_sd": str(cli.MINUTES_PER_SIGMA),
    "worth_only": "",
    "rare_only": "",
    "no_tides": "",
    "no_history": "",
}


def _args_from_form(form) -> "cli.argparse.Namespace":
    argv = [
        "--days", form.get("days", "1") or "1",
        "--top", form.get("top", "10") or "10",
        "--minutes-per-sd", form.get("minutes_per_sd") or str(cli.MINUTES_PER_SIGMA),
    ]
    for flag, key in (
        ("--worth-only", "worth_only"),
        ("--rare-only", "rare_only"),
        ("--no-tides", "no_tides"),
        ("--no-history", "no_history"),
    ):
        if form.get(key):
            argv.append(flag)
    return cli.build_parser().parse_args(argv)


def _display_rows(rows):
    out = []
    for r in rows:
        spot, drive, surf, rare = r["spot"], r["drive"], r["surf"], r["rarity"]
        out.append({
            "name": spot.name,
            "value": f"{r['value'].total:+.2f}" if r["value"].total is not None else "-",
            "surf": f"{surf.total:.1f}",
            "best": cli._local_hhmm(r),
            "drive": cli._fmt_time(drive.duration_minutes),
            "vs_norm": (f"p{rare.percentile:.0f} ({rare.sigma:+.1f}σ)"
                        if rare.percentile is not None else "-"),
            "verdict": cli.compute_verdict(r),
            "flag": rare.label(),
            "storm": r["storm"],
        })
    return out


@app.route("/")
def index():
    form = {**FORM_DEFAULTS, **request.args}
    zip_code = form["zip"].strip()

    context = {
        "form": form, "rows": None, "error": None, "origin": None,
        "n_spots": len(SPOTS), "meta": None,
        "logged": request.args.get("logged"),
        "log_error": request.args.get("log_error"),
    }

    if not zip_code:
        return render_template("index.html", **context)

    try:
        args = _args_from_form(form)
    except SystemExit:
        context["error"] = "One of the numeric fields isn't valid - check days/top/minutes."
        return render_template("index.html", **context)

    if not 1 <= args.days <= cli.MAX_FORECAST_DAYS:
        context["error"] = f"Days must be between 1 and {cli.MAX_FORECAST_DAYS}."
        return render_template("index.html", **context)

    try:
        origin = cli.geocode_zip(zip_code)
    except GeocodeError as exc:
        context["error"] = str(exc)
        return render_template("index.html", **context)

    rows, meta = cli.gather(origin, args)
    rows = cli.filter_and_sort(rows, args)

    context.update(
        origin=origin,
        rows=_display_rows(rows),
        meta={
            "itch": meta.get("itch", 0.0),
            "sessions": meta["log"].total_sessions() if meta.get("log") else 0,
            "days_since": meta["log"].days_since_last() if meta.get("log") else None,
        },
        storms=[r["spot"].name for r in rows if r["storm"] == "active"],
    )
    return render_template("index.html", **context)


@app.route("/surfed", methods=["POST"])
def surfed():
    """
    Record a session via cli.record_surfed - the same validation the
    --surfed CLI flag uses - then bounce back to whatever view was open,
    with a query param carrying the result so it survives the redirect.
    """
    params = dict(parse_qsl(request.form.get("return_qs", "")))
    params.pop("logged", None)
    params.pop("log_error", None)
    spot_query = request.form.get("spot", "").strip()
    on = request.form.get("on", "").strip() or None

    if not spot_query:
        params["log_error"] = "Enter a spot name."
    else:
        try:
            matched, _when, _log = cli.record_surfed(spot_query, on)
            params["logged"] = matched
        except cli.SurfLogError as exc:
            params["log_error"] = str(exc)

    return redirect(f"{url_for('index')}?{urlencode(params)}")


# Not 5000: macOS's AirPlay Receiver listens there by default (System
# Settings > General > AirDrop & Handoff), which silently steals the port
# from Flask's dev server rather than erroring cleanly.
DEFAULT_PORT = 5050


def main():
    app.run(debug=True, port=DEFAULT_PORT)


if __name__ == "__main__":
    main()
