# Attribution

This project stands on existing open-source work. Everything borrowed is listed
here with its license and what specifically was taken.

---

## hugosrc/surf-forecast-api — Apache License 2.0

**Source:** https://github.com/hugosrc/surf-forecast-api
**File referenced:** `src/services/rating.ts`
**Used in:** `fl_surf_check/scoring.py`

### What was taken

The **architecture** of the surf rating: decompose a forecast into independent
sub-ratings (swell height, swell period, wind-vs-swell direction), score each on
its own scale, then combine them into one number. That decomposition is the good
idea, and it's the idea this project reuses.

The implementation is a Python reimplementation, not a translation. No source
was copied.

### What was changed, and why

**1. Continuous angles instead of compass quadrants.**
The original bins wind and swell directions into four quadrants (N/E/S/W) and
compares them. Florida's Atlantic coast rotates through roughly 80° between
Fernandina Beach and Miami, so quadrant bins put spots 50 miles apart into
different categories while treating a 44° and a 46° wind as fundamentally
different. Here, wind is scored on the continuous angle between its bearing and
each spot's own offshore bearing.

**2. Wind speed is a factor, not just direction.**
The original scores wind on direction alone (same-direction: 1, offshore: 5,
otherwise: 3). But 3mph onshore is glassy and fine, while 30mph offshore is
unrideable. Here, speed scales how much direction is allowed to matter, and
strong wind carries a penalty regardless of direction.

**3. Florida-tuned height curve.**
The original treats "head high and above" as a flat maximum. Florida is a
small-wave coast where beach breaks tend to close out rather than improve past a
point, so this curve peaks around 3–5ft and eases back above ~7ft.

**4. Continuous interpolation instead of integer tiers.**
The original returns integers 1–5 per factor. Ranking 26 spots that way produces
constant ties. Here every sub-score is piecewise-linear interpolated over
control points, so a 2.49ft wave and a 2.51ft wave score almost identically and
the final ranking is strict.

> Apache-2.0 permits this use with attribution and a statement of changes. This
> file is that statement.

---

## ryansurf/cli-surf — MIT License

**Source:** https://github.com/ryansurf/cli-surf
Copyright (c) 2024 Ryan Frederich
**Used in:** `fl_surf_check/conditions.py`

### What was taken

- The pattern of wrapping the Open-Meteo client in a **cached session** so
  repeated runs don't re-hit the free API (from `src/open_meteo.py`).
- The approach of resolving the **nearest NOAA tide station** by great-circle
  distance, and the NOAA CO-OPS `datagetter` request shape — product
  `predictions`, `interval=hilo`, datum `MLLW` (from `src/api.py`).

### What was changed

- Tide stations are **precomputed per spot** in `spots.py` rather than looked up
  at runtime. The station list doesn't change between runs, and this removes a
  network call and a dependency on NOAA's station index being reachable.
- The retry layer is a local 10-line helper rather than the `retry-requests`
  package, for licensing reasons (see [SECURITY-REVIEW.md](SECURITY-REVIEW.md)).
- Nothing from `src/send_email.py` was used.

---

## open-meteo/python-requests — Apache License 2.0

**Source:** https://github.com/open-meteo/python-requests
**Used as:** a dependency (`openmeteo-requests`)

The official Open-Meteo Python client. Used unmodified. Its multi-location
support is what lets this project fetch all 26 spots in a single API request
instead of 26 — the main reason it's a dependency rather than hand-rolled
`requests` calls.

---

## symerio/pgeocode — BSD 3-Clause

**Source:** https://github.com/symerio/pgeocode
**Used as:** a dependency, in `fl_surf_check/location.py`

Offline zip code → coordinates, backed by the GeoNames postal-code dataset.
Chosen over a geocoding API so that your location isn't sent over the network
on every run, and so there's no rate limit to hit.

---

## geopy — MIT License

**Source:** https://github.com/geopy/geopy
**Used as:** a dependency, in `distance.py` and `location.py`

`geopy.distance.great_circle` for straight-line distances, and its Nominatim
geocoder as a fallback when `pgeocode` can't resolve a zip.

---

## Reviewed but deliberately not used

**[mpiannucci/surfnerd](https://github.com/mpiannucci/surfnerd)** — a genuinely
good suite of Go surf-forecasting and wave-analysis tools, and the closest prior
art to this project. It has **no license file**, which means no grant of use is
given. Nothing was taken from it.

**[swrobel/meta-surf-forecast](https://github.com/swrobel/meta-surf-forecast)**
(MIT) — aggregates Surfline and Spitcast. Reviewed, but this project
deliberately avoids unofficial/reverse-engineered endpoints.

---

## Data sources

| Source | Terms |
|---|---|
| [Open-Meteo](https://open-meteo.com/) | Free for non-commercial use, CC BY 4.0 data |
| [NOAA CO-OPS](https://tidesandcurrents.noaa.gov/) | US Government work — public domain |
| [OSRM demo server](https://project-osrm.org/) | Free public demo instance (BSD-2-Clause software) |
| [GeoNames](https://www.geonames.org/) | CC BY 4.0 |
| [OpenStreetMap Nominatim](https://nominatim.org/) | ODbL data; usage policy honored (descriptive User-Agent, ≤1 request/run) |
