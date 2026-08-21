# fl-surf-check

**Rate Florida surf spots out of 10, then work out which ones are actually worth driving to.**

Checking the surf is easy. Deciding whether to *go* is the hard part — a 9/10 day
three hours away and a 5/10 day fifteen minutes away are genuinely difficult to
compare, because they're measured in different units.

`fl-surf-check` scores 26 Florida breaks on live wave, wind and tide data, then
normalizes that score against drive time from your zip code to produce a single
**"worth the drive"** number.

```
  Florida surf check  -  from Daytona Beach, FL 32118
  --------------------------------------------------------------------------------------------
  #  SPOT                                    SURF  WORTH   MILES   DRIVE   VERDICT
  --------------------------------------------------------------------------------------------
  1  Ponce Inlet                              9.3    9.0      13     16m   GO. Drop everything
      [#########.]  height 9.7 | period 9.9 | wind 8.3 | dir 7.1
      waves 5.4ft / 14s   wind 18mph @ 294deg   tide High 14:32

  2  Ormond Beach                             7.8    8.2       6      8m   Go surf
      [########..]  height 9.5 | period 7.9 | wind 4.5 | dir 8.7
      waves 5.7ft / 11s   wind 9mph @ 60deg   tide High 14:32

  3  New Smyrna Beach Inlet                   7.5    7.7      16     20m   Go surf
      [#######...]  height 9.9 | period 6.3 | wind 4.7 | dir 8.1
      waves 5.1ft / 9s   wind 8mph @ 80deg   tide High 14:32
  --------------------------------------------------------------------------------------------
```

---

## Contents

- [Quick start](#quick-start)
- [Usage](#usage)
- [How the surf score works](#how-the-surf-score-works)
- [How the "worth the drive" normalization works](#how-the-worth-the-drive-normalization-works)
- [Tuning it to how you actually surf](#tuning-it-to-how-you-actually-surf)
- [Data sources](#data-sources)
- [Spot list](#spot-list)
- [Project layout](#project-layout)
- [Testing](#testing)
- [Limitations and honest caveats](#limitations-and-honest-caveats)
- [Credits](#credits)

---

## Quick start

```bash
git clone https://github.com/usffish/fl-surf-check.git
cd fl-surf-check
pip install -r requirements.txt

python -m fl_surf_check --zip 32118
```

No API keys. No signup. No accounts. Every data source used is free and public.

**First run needs internet** to download the zip-code database (a few MB, via
`pgeocode`). After that the zip lookup is fully offline.

---

## Usage

```
python -m fl_surf_check --zip ZIP [options]
```

| Flag | Default | What it does |
|---|---|---|
| `--zip`, `-z` | *required* | Your 5-digit zip code — the drive origin. |
| `--top`, `-n` | `10` | How many spots to show. `0` shows all 26. |
| `--decay-miles` | `75` | How far you're comfortable driving. Higher = distance matters less. |
| `--surf-weight` | `0.7` | `0`–`1`. How much conditions matter vs. distance. `1.0` ignores distance entirely. |
| `--max-miles` | *none* | Hard cutoff — drop anything further than this. |
| `--min-score` | *none* | Hard cutoff — drop anything below this surf score. |
| `--details` | off | Show the per-factor breakdown and raw conditions. |
| `--no-tides` | off | Skip NOAA tide lookups. Faster; tide is a minor input. |

### Examples

```bash
# What's good near me right now?
python -m fl_surf_check --zip 32118

# I've got all day and a full tank
python -m fl_surf_check --zip 32118 --decay-miles 200 --top 15

# Just tell me where the best waves are, I don't care how far
python -m fl_surf_check --zip 32118 --surf-weight 1.0

# I'll drive an hour, max, and only for something decent
python -m fl_surf_check --zip 32118 --max-miles 60 --min-score 5

# Show me the numbers behind the rating
python -m fl_surf_check --zip 32118 --details
```

---

## How the surf score works

Each spot gets four independent sub-scores out of 10, which are then weighted
into a single rating.

| Factor | Weight | Why |
|---|---|---|
| **Wave height** | 35% | The most obvious input, but not the most important for quality. |
| **Swell period** | 30% | The best single proxy for wave *quality*. See below. |
| **Wind** | 25% | Can turn a good swell to mush, or hold a mediocre one open. |
| **Swell direction** | 10% | How directly the swell hits the beach. Deliberately gentle — see caveats. |

Plus a small (±0.15) bonus for a moving tide.

### Wave height — tuned for Florida

Florida is a small-wave coast, so a generic curve that treats "bigger = better"
gets it wrong. This one peaks in the **3–5ft** range and *eases back above ~7ft*,
where Florida's sandbars tend to close out rather than improve.

```
score  0.0   1.0   3.5   6.5   9.0  10.0   8.5   6.0   3.5
  ft   0.0   0.7   1.5   2.5   3.5   5.0   7.0  10.0  15.0
                                     ^^^^
                                  peak: as good as FL gets
```

### Swell period — the quality signal

If you only look at one number, look at this one. Period is the gap between
waves, and it tells you where the swell came from:

- **Under 6s** — local wind chop. Disorganized slop. This is most of Florida, most of the time.
- **8–10s** — mixed, workable.
- **12s+** — real groundswell, generated by a distant storm. Organized, powerful, well-spaced. This is what you drive for.

A 4ft/5s day and a 4ft/13s day are the same height and completely different sessions.

### Wind — direction *and* speed

Wind is scored on the continuous angle between where it's blowing **from** and
that spot's own offshore bearing:

- **Offshore** (blowing from land out to sea) grooms the wave face and holds it open. Best.
- **Onshore** turns the surf to mush. Worst.
- **Cross-shore** is in between.

Two refinements that matter in practice:

1. **Speed scales how much direction matters.** Under ~4mph it's glassy and
   direction is nearly irrelevant — light onshore is still a good morning.
2. **Strong wind is penalized even when offshore.** 35mph offshore is not a
   better day than 8mph offshore; it's unrideable.

---

## How the "worth the drive" normalization works

This is the part you actually asked for. Two steps:

### 1. Turn distance into a 0–1 "closeness" factor

```
closeness = exp( -miles / decay_miles )
```

Exponential decay, not a hard cutoff. At `--decay-miles 75` (the default):

| Drive | Closeness |
|---|---|
| 0 mi | 1.00 |
| 25 mi | 0.72 |
| 50 mi | 0.51 |
| 75 mi | 0.37 |
| 150 mi | 0.14 |
| 300 mi | 0.02 |

### 2. Blend it with the surf score

```
worth = 10 × [ surf_weight × (surf ÷ 10)  +  (1 − surf_weight) × closeness ]
```

Both terms are normalized to 0–1 before weighting, which is what lets a 0–10
rating and a distance in miles be combined meaningfully at all.

**Why a weighted blend rather than `surf × closeness`?** Multiplying lets
distance zero out an otherwise perfect day, which isn't how anyone actually
decides. A blend keeps a genuinely epic far-away spot competitive with a
mediocre close one — which is exactly the trade-off you're trying to *see*.

The `VERDICT` column reads the two numbers together and gives you the plain
English call, from *"Flat — stay home"* through *"Worth a look"* to
*"GO. Drop everything"* (and *"Epic — but that's a road trip"* when the great
day is 200+ miles out).

---

## Tuning it to how you actually surf

The defaults encode one particular set of preferences. Yours will differ:

**"I'll drive further than that."**
Raise `--decay-miles`. At `200`, a two-hour drive barely registers as a cost.

**"I only care about quality."**
Raise `--surf-weight` toward `1.0` to rank purely on conditions.

**"I only care about what's close."**
Lower `--surf-weight` toward `0.0`, or just set `--max-miles`.

**"It keeps recommending spots that are too big/small for me."**
Edit the control points in `score_wave_height()` in `fl_surf_check/scoring.py`.
They're a plain list of `(feet, score)` pairs — move the peak to where you like it.

**"The wind call is backwards at my local."**
Adjust that spot's `facing_deg` in `fl_surf_check/spots.py`. These are
approximations of coastline orientation, not surveyed bearings.

**"My spot isn't in here."**
Add a `Spot(...)` entry to `SPOTS` in `fl_surf_check/spots.py`. You need a name,
region, lat/lon, the direction the beach faces, and the nearest NOAA tide
station ID.

---

## Data sources

Everything is free, public, documented, and key-free.

| Source | Used for | License / terms |
|---|---|---|
| [Open-Meteo Marine API](https://open-meteo.com/en/docs/marine-weather-api) | Wave height, period, direction | Free for non-commercial use, no key |
| [Open-Meteo Forecast API](https://open-meteo.com/en/docs) | Wind speed and direction | Free for non-commercial use, no key |
| [NOAA CO-OPS](https://api.tidesandcurrents.noaa.gov/api/prod/) | Tide predictions | US public domain |
| [OSRM demo server](https://project-osrm.org/) | Driving distance and time | Free public demo instance |
| [GeoNames](https://www.geonames.org/) (via `pgeocode`) | Zip code → coordinates | CC BY 4.0 |

### Being a good API citizen

This tool deliberately minimizes the load it puts on free services:

- **All 26 spots are fetched in 2 API calls, not 52.** Open-Meteo accepts
  comma-separated coordinate lists, so wave data for every spot arrives in one
  request and wind data in another.
- **Responses are cached locally for 30 minutes** (`requests-cache`). Running it
  five times in a row hits the network once.
- **Tides are fetched per-station, not per-spot** — 26 spots share 9 NOAA stations.
- **Zip lookups are offline** after the first run.

If OSRM or NOAA is unreachable, the tool degrades rather than failing: distances
fall back to a straight-line estimate (marked `~` in the output) and tide simply
drops out of the score.

---

## Spot list

26 breaks, north to south:

**Northeast FL** — Fernandina Beach · Jacksonville Beach Pier · Ponte Vedra · St. Augustine Pier · Vilano Beach

**East Central FL (Space Coast)** — Flagler Beach Pier · Ormond Beach · New Smyrna Beach Inlet · Ponce Inlet · Playalinda · Cocoa Beach Pier · Satellite Beach · Indialantic · **Sebastian Inlet**

**Treasure Coast** — Vero Beach · Fort Pierce Inlet · Stuart / House of Refuge · Juno Beach / Jupiter

**Palm Beaches** — Reef Road · Boynton Beach Inlet

**Broward / Miami-Dade** — Deerfield Beach · Fort Lauderdale · South Beach / South Pointe

**Gulf Coast (Panhandle)** — Panama City Beach · Navarre Beach · Pensacola Beach

> Gulf spots are included for completeness but only really work during cold
> fronts or tropical systems. On a typical day they'll score near zero, which is
> correct.

---

## Project layout

```
fl_surf_check/
├── spots.py        # The 26 spots: coordinates, beach bearings, tide stations
├── location.py     # Zip code → lat/lon (pgeocode, Nominatim fallback)
├── distance.py     # Great-circle + OSRM driving distance, decay function
├── conditions.py   # Open-Meteo + NOAA fetching, batched and cached
├── scoring.py      # All the scoring math (pure functions, no I/O)
└── cli.py          # argparse, orchestration, table rendering

tests/
├── test_scoring.py      # 32 tests on the scoring math
└── test_cli_offline.py  # 9 end-to-end tests with the network mocked
```

`scoring.py` contains no I/O at all, which is why it's the easiest part to test
and to tune.

---

## Testing

```bash
pip install pytest
python -m pytest tests/ -q
```

```
41 passed in 0.26s
```

All 41 tests run **offline** — network calls are mocked — so the suite is fast
and works in CI. They cover the scoring curves (monotonicity, bounds,
continuity, Florida-specific tuning), the distance decay math, the
worth-the-drive blend (including that an epic far day beats a mediocre close
one), spot data integrity, CLI filtering and sorting, and graceful degradation
when every API is down.

---

## Limitations and honest caveats

**This has not been validated against live API responses.** It was built in a
sandbox without network access to Open-Meteo, NOAA, or OSRM. The logic is
tested end-to-end against mocked data, and the API request shapes follow each
service's current documentation — but the first live run is still the real test.
If a field name has drifted, that's where it'll show.

**Beach bearings are estimates.** The `facing_deg` values approximate the
coastline's orientation at each spot from map inspection. They're good enough to
tell offshore from onshore, which is all the scoring needs — but they aren't
surveyed. This is exactly why swell direction is only weighted at 10%.

**Model data, not buoy data.** Open-Meteo's marine forecast is a numerical
model. It's good, and it's free, but it's not a wave buoy and it's not someone
standing on the beach looking at it. Treat the score as "worth checking the cam"
rather than gospel.

**Wave height ≠ face height.** Forecast models report significant wave height
offshore. What actually breaks on a given sandbar is a different, smaller, and
very local number.

**Sandbars change.** A spot's quality depends enormously on its sandbars, which
shift with every storm. No API knows this. Local knowledge still wins.

**No crowd factor.** New Smyrna on a good Saturday is a very different
proposition from the score alone. Sebastian Inlet even more so.

**Tide is crude.** It contributes ±0.15 based only on whether the tide is
moving. Real tide preference is deeply spot-specific — some breaks only work on
a pushing mid — and modeling that properly would need per-spot data this
doesn't have.

---

## Credits

This project reuses and adapts existing open-source work rather than
reinventing it. See [ATTRIBUTION.md](ATTRIBUTION.md) for full detail and
[SECURITY-REVIEW.md](SECURITY-REVIEW.md) for the dependency audit.

- **[hugosrc/surf-forecast-api](https://github.com/hugosrc/surf-forecast-api)**
  (Apache-2.0) — the structure of the surf rating algorithm: decompose the
  forecast into independent sub-ratings, then combine. Adapted here with
  continuous angles instead of compass quadrants, wind speed as a factor, and a
  Florida-tuned height curve.
- **[ryansurf/cli-surf](https://github.com/ryansurf/cli-surf)** (MIT) — the
  approach to Open-Meteo client caching and to resolving the nearest NOAA tide
  station.
- **[open-meteo/python-requests](https://github.com/open-meteo/python-requests)**
  (Apache-2.0) — the official Open-Meteo client, which makes the batched
  multi-location fetch possible.
- **[symerio/pgeocode](https://github.com/symerio/pgeocode)** (BSD-3-Clause) — offline zip geocoding.
- **[geopy](https://github.com/geopy/geopy)** (MIT) — great-circle distance.

## License

MIT — see [LICENSE](LICENSE).
