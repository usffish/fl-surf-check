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
  #  SPOT                            SURF  WORTH  MILES   DRIVE     VS NORM   BONUS   VERDICT
  --------------------------------------------------------------------------------------------
  1  Sebastian Inlet (First Peak)     8.6    8.1    118   2h21m   p88 +1.9s   -1h33m  GO. Drop everything  [RARE]
  2  Ponce Inlet                      7.9    7.8     11      24m   p81 +1.4s    -1h08m  Go surf  [STANDOUT]
  3  New Smyrna Beach Inlet           7.7    7.5     17      32m   p81 +1.4s    -1h08m  Go surf  [STANDOUT]
  --------------------------------------------------------------------------------------------
  SURF = conditions out of 10.  WORTH = conditions blended with distance (70% conditions).
  VS NORM = percentile (and geometric SDs) against ALL Florida spots' swell history for this time of year;
            BONUS = drive time earned at 60 min per SD above normal, discounted before ranking.
```

`VS NORM` is the part that answers *"is today actually special?"* — a 2.8ft day
reads as unremarkable in absolute terms and is a genuine top-10% day in Florida.


---

## Contents

- [Quick start](#quick-start)
- [Usage](#usage)
- [How the surf score works](#how-the-surf-score-works)
- [How the "worth the drive" normalization works](#how-the-worth-the-drive-normalization-works)
- [Rarity: is today one of the good ones?](#rarity-is-today-one-of-the-good-ones)
- [Earning drive time](#earning-drive-time)
- [Thunderstorms](#thunderstorms)
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
| `--minutes-per-sd` | `60` | Extra driving you'll accept per standard deviation above normal conditions. `0` ranks on raw drive time. |
| `--rare-only` | off | Show only spots having an unusually good day for the time of year. |
| `--no-tides` | off | Skip NOAA tide lookups. Faster; tide is a minor input. |
| `--no-history` | off | Skip the statewide historical baseline. Drops the `VS NORM` and `BONUS` columns. |
| `--refresh-history` | off | Force-rebuild the cached baseline. Rarely needed — it refreshes itself every 30 days. |

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

# Only tell me if today is genuinely unusual
python -m fl_surf_check --zip 32118 --rare-only

# A great day is worth two extra hours per SD, not one
python -m fl_surf_check --zip 32118 --minutes-per-sd 120

# Rank on raw drive time, ignoring how rare the day is
python -m fl_surf_check --zip 32118 --minutes-per-sd 0
```

---

## How the surf score works

Each spot gets four independent sub-scores out of 10, which are then weighted
into a single rating.

> **Two wave measurements, used for different jobs.** Open-Meteo reports both
> the *total* sea state and the *swell partition* — the rideable groundswell
> with local wind chop removed. The 0–10 score below reads the total; rarity and
> the historical baseline read the swell. Checked against the NWS coastal waters
> forecast, the swell partition matched almost exactly (Open-Meteo 1.12ft @ 7.9s
> where NWS said "east 1 foot at 9 seconds"), while the total read 1.31ft @
> 7.25s with 0.66ft of 1.7s sea-breeze chop mixed in. Scoring the swell
> partition throughout would tighten the score on windy days — it drops a
> blown-out 3.2ft/5s afternoon from *"Marginal"* to *"Poor"* — and is a
> one-line change in `conditions.py`.

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

## Rarity: is today one of the good ones?

The 0–10 surf score answers *"is this rideable?"* on an absolute scale. It
cannot answer the question you actually care about — *"is today unusually
good?"* — because that depends entirely on what Florida normally does. A
2.5ft/11s day is unremarkable in Hawaii and a red-letter day here.

So the tool pools **~5 years of daily-maximum swell across all 26 spots** into a
single statewide seasonal baseline, and reports today as a percentile against
it. That's the `VS NORM` column.

### Why one statewide baseline, not one per spot

Per-spot baselines grade every break on its own curve, which flatters the weak
ones: a mediocre Pensacola day would read as "p90" beside a genuinely excellent
Sebastian Inlet day, because each is only compared to itself. Since the whole
point is choosing *where to drive*, they need one shared yardstick — a spot
that is simply better should read as better.

### Why percentile rank, not min–max

Surf is heavily right-skewed: a handful of tropical systems own the top of the
range. Measured on the real record, min–max normalisation maps a **median** day
to 0.17 and even a strong p90 day to only 0.38 — every ordinary day bunches
into the bottom fifth of the scale and the signal is lost. Percentile rank asks
the same question ("where does this sit in the observed population?") but
spreads evenly by construction and is immune to how extreme the extremes are.

### Why thin evidence gets shrunk

The record only starts in October 2021, so a "top 2% day!" claim may rest on
five seasons. Rather than let a thin sample shout, the percentile is pulled
toward normal in proportion to the evidence behind it — Laplace's rule of
succession, generalised from a proportion to a percentile:

```
shrunk = (percentile x n + 50 x prior) / (n + prior)
```

With a full baseline (n ≈ 130 days) a p98 reading lands near p89. With 8 days
behind it, that same reading lands near p60 — visible, but not shouted about.

Because shrinkage imposes a ceiling, the `STANDOUT` / `RARE` / `BEST IN NYR`
labels are thresholds on the *attainable* range rather than fixed percentiles.
Fixed cutoffs would be unreachable dead code.

---

## Earning drive time

Distance decay alone treats every day the same: the same `--decay-miles` on a
flat Tuesday as on the best swell of the year. But willingness to drive isn't
fixed — it stretches with how good the day is.

> **Every 1 standard deviation above normal buys 60 more minutes of driving.**

Tunable with `--minutes-per-sd` (`0` disables it). The discount is applied to
the drive *before* ranking, so a far spot on a rare day competes with a close
spot on an ordinary one. A 2-hour drive on a day that has earned 60 minutes is
scored as though it were a 1-hour drive.

### The standard deviation is geometric

Measured on the real record, raw swell height has a **skew of 1.95**. A plain
z-score on that misbehaves badly:

| | raw values | log space |
|---|---|---|
| skew | 1.95 | **0.27** |
| z = 0 lands at | p64 | **p50** |
| +1 SD | p87.6 | **p84.8** (textbook: 84.1) |
| biggest day in 5 years | z **+5.9** | z **+3.1** |

At 60 minutes per SD, that +5.9 would have justified a *six-hour* drive. Taking
logs first restores σ's textbook meaning, and the record day becomes a
well-behaved +3.1σ ≈ three extra hours. Two guards on top: the sigma is damped
by the same evidence weighting as the percentile, and the allowance is capped at
four hours. Below-normal days earn nothing but are never penalised — the real
drive time is already the honest cost.

---

## Thunderstorms

Rain doesn't stop anyone surfing. Lightning does — and a surfer is the tallest
conductive object on a flat wet plain, which is why Florida leads the country in
lightning fatalities.

So storm risk is deliberately **not** folded into the 0–10 score. A perfect 8ft
swell under a squall line isn't "slightly worse surf," it's surf you must not
paddle out into. It's reported as a separate gate, and:

- **An active storm forfeits the drive allowance entirely.** Great surf must
  never route you *toward* lightning.
- **An active storm overrides the verdict.** Printing "GO. Drop everything"
  beside a lightning warning is worse than useless.
- **Rain never flags.** Drizzle, showers and heavy rain at 90% precipitation
  probability all read as `none`.

Risk needs *both* instability and precipitation: high CAPE on a dry day is a
loaded gun with no trigger, and heavy rain with no instability is just rain.

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
| [Open-Meteo Marine API](https://open-meteo.com/en/docs/marine-weather-api) | Wave height, period, direction; the swell partition | Free for non-commercial use, no key |
| Open-Meteo Marine, `start_date`/`end_date` | ~5 years of history for the statewide baseline | Same endpoint, same terms |
| [Open-Meteo Forecast API](https://open-meteo.com/en/docs) | Wind, weather code, CAPE, precipitation probability | Free for non-commercial use, no key |
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
- **The historical baseline is fetched once and cached for 30 days.** It is a
  single batched request, but a large one — ~1.1M samples across 26 spots and
  five years. Running it every invocation would trip Open-Meteo's per-minute
  rate limit, which then starves the live conditions request in the same run.
  The cache (`.fl_surf_climatology.json`, gitignored) is what prevents that.
- **Rate-limit errors are not retried.** Open-Meteo's limit is per-minute, so
  short backoff could never outlast it and each attempt would spend more of the
  quota. The tool fails fast with an accurate message instead.
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
├── climatology.py  # ~5yr statewide swell baseline, pooled and cached on disk
├── scoring.py      # All the scoring math (pure functions, no I/O)
└── cli.py          # argparse, orchestration, table rendering

tests/
├── test_scoring.py       # 32 tests on the scoring math
├── test_climatology.py   # 48 tests on baselines, rarity, drive bonus, storms
└── test_cli_offline.py   # 11 end-to-end tests with the network mocked
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
91 passed in 0.64s
```

All 91 tests run **offline** — network calls are mocked and `build_baseline`
takes an injectable client — so the suite is fast and works in CI. They cover
the scoring curves (monotonicity, bounds, continuity, Florida-specific tuning),
the distance decay math, the worth-the-drive blend (including that an epic far
day beats a mediocre close one), spot data integrity, CLI filtering and
sorting, and graceful degradation when every API is down.

The historical layer adds coverage for percentile inversion, Laplace shrinkage,
the sigma-to-minutes relationship, the thunderstorm gate, and cache
invalidation. Two are regression tests for bugs the live run surfaced:

- **Tide timezone.** Pins that a tide 20 minutes away in Central time is found
  for a Central station and *not* for an Eastern one. Written to be independent
  of the timezone the test machine runs in, and verified to fail against the
  old naive-`datetime.now()` comparison.
- **Reachable rarity labels.** Sweeps the input space asserting every label can
  actually be produced. The first thresholds were fixed percentiles (≥93, ≥97)
  while shrinkage capped the attainable value near p91 — making the top two
  labels dead code.

---

## Limitations and honest caveats

**The marine record only reaches back to October 2021.** Open-Meteo serves no
marine data before that, and the atmospheric archive returns all-NaN for wave
variables — so every rarity percentile rests on roughly five seasons, not
thirty. This is why percentiles are shrunk toward normal in proportion to the
evidence behind them, and why a p99 reading is reported as p90.

**Storm detection is forecast-only.** ERA5, the reanalysis behind the
historical baseline, emits *zero* thunderstorm codes across 1.1M hours of
Florida record — convection is sub-grid at ~25km resolution and gets
parameterised into ordinary rain — and CAPE is unavailable historically. The
baseline therefore cannot be storm-filtered. Measurement says it does not
matter: because the baseline is built from daily maxima and a thunderstorm is a
1–3 hour afternoon event, excluding storm hours moves the percentiles by 0.00%.

**Nearby spots can share a forecast cell.** Open-Meteo snaps each request to the
nearest wet grid cell, up to ~14 miles. The 26 spots collapse into 22 distinct
cells, so four pairs — St. Augustine/Vilano, Flagler/Ormond, New Smyrna/Ponce,
Patrick/Indialantic — get identical conditions and are separated only by drive
time.

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
- **The movie-score engine** (this author's own, unpublished) — the shape of the
  rarity composite: normalise across the whole batch rather than per-item,
  weight by how much evidence stands behind a score (Laplace's rule of
  succession), and drop missing signals from numerator *and* denominator rather
  than zero-filling them. Adapted here with percentile rank substituted for
  min–max, because surf is far more right-skewed than film ratings.
- **[symerio/pgeocode](https://github.com/symerio/pgeocode)** (BSD-3-Clause) — offline zip geocoding.
- **[geopy](https://github.com/geopy/geopy)** (MIT) — great-circle distance.

## License

MIT — see [LICENSE](LICENSE).
