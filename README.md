# fl-surf-check

**Rate Florida surf spots out of 10, then work out which ones are actually worth driving to.**

Checking the surf is easy. Deciding whether to *go* is the hard part — a 9/10 day
three hours away and a 5/10 day fifteen minutes away are genuinely difficult to
compare, because they're measured in different units.

`fl-surf-check` scores 41 Florida breaks on live wave, wind and tide data, then
normalizes that score against drive time from your zip code to produce a single
**"worth the drive"** number.

```
  Florida surf check  -  from Daytona Beach, FL 32118
  --------------------------------------------------------------------------------------------
  #  SPOT                        VALUE  SURF        BEST   DRIVE   COST     VS NORM   VERDICT
  --------------------------------------------------------------------------------------------
  1  Ponce Inlet                 +0.80   5.3   Mon 18:00     24m  -0.20   p52 +0.0s   Worth the drive
  2  Daytona Beach Shores        +0.67   5.0   Mon 18:00      9m  -0.07   p52 +0.0s   Worth the drive
  3  Apollo Beach (Canaveral NS  +0.43   5.9   Tue 11:00     50m  -0.42   p51 +0.0s   Worth the drive
  --------------------------------------------------------------------------------------------
  BEST = the best surfable hour in the window; all figures are for that hour.
  VALUE = surf (in SDs above normal) minus drive time, at 120 min per SD.  Positive = worth going.
```

**`VALUE` is the whole answer.** Positive means the surf is worth the drive;
the number is how much margin you have, in standard deviations. `BEST` is when
to go, and `VS NORM` is what makes it meaningful — a 3.5ft day reads as
unremarkable in absolute terms and is a genuine top-10% day in Florida.

Use `--days 5` to plan a weekend rather than an afternoon.


---

## Contents

- [Quick start](#quick-start)
- [Usage](#usage)
- [How the surf score works](#how-the-surf-score-works)
- [The value calculation](#the-value-calculation)
- [Planning several days ahead](#planning-several-days-ahead)
- [Which hour it scores](#which-hour-it-scores)
- [Rarity: is today one of the good ones?](#rarity-is-today-one-of-the-good-ones)
- [Your surf log](#your-surf-log)
- [Thunderstorms](#thunderstorms)
- [Tuning it to how you actually surf](#tuning-it-to-how-you-actually-surf)
- [Data sources](#data-sources)
- [Spot list](#spot-list)
- [Data visualizations](#data-visualizations)
- [Project layout](#project-layout)
- [Testing](#testing)
- [Adding another region later](#adding-another-region-later)
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

Set your home zip once and drop the flag entirely:

```bash
export FL_SURF_ZIP=33613        # add to ~/.zshrc to make it stick
python -m fl_surf_check
python -m fl_surf_check --days 5
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
| `--zip`, `-z` | `$FL_SURF_ZIP` | Your 5-digit zip code — the drive origin. Falls back to the `FL_SURF_ZIP` environment variable. |
| `--top`, `-n` | `10` | How many spots to show. `0` shows all 41. |
| `--minutes-per-sd` | `120` | Minutes of driving one standard deviation of surf is worth. The exchange rate in the value score. |
| `--worth-only` | off | Show only spots with a positive value score. |
| `--decay-miles` | `75` | *Legacy.* Only used by the `--no-history` fallback blend. |
| `--surf-weight` | `0.7` | *Legacy.* Only used by the `--no-history` fallback blend. |
| `--max-miles` | *none* | Hard cutoff — drop anything further than this. |
| `--min-score` | *none* | Hard cutoff — drop anything below this surf score. |
| `--details` | off | Show the per-factor breakdown and raw conditions. |
| `--days`, `-d` | `1` | How many days ahead to consider (1–7). |
| `--rare-only` | off | Show only spots having an unusually good day, judged against the full history. |
| `--no-tides` | off | Skip NOAA tide lookups. Faster; tide is a minor input. |
| `--surfed` | — | Record a session and exit. Fuzzy-matched, so `apollo` works. |
| `--on` | today | Date for `--surfed`, as `YYYY-MM-DD`. |
| `--log-path` | `$FL_SURF_LOG` | Where the surf log lives (default `~/.fl_surf_log.json`). |
| `--itch-rate` | `0.05` | σ gained per day since your last session. |
| `--novelty-weight` | `0.5` | How hard to handicap spots you surf often. `0` disables. |
| `--no-personal` | off | Ignore the surf log for this run. |
| `--no-history` | off | Skip the statewide historical baseline. `VALUE` and `VS NORM` fall back to `-`. |
| `--refresh-history` | off | Force-rebuild the cached baseline. Rarely needed — it refreshes itself every 30 days. |

### Examples

```bash
# What's good near me right now?
python -m fl_surf_check --zip 32118

# Only show me what's actually worth the drive
python -m fl_surf_check --zip 32118 --worth-only

# I've got all day: one good SD is worth four hours to me
python -m fl_surf_check --zip 32118 --minutes-per-sd 240

# I'll drive an hour, max, and only for something decent
python -m fl_surf_check --zip 32118 --max-miles 60 --min-score 5

# Show me the numbers behind the rating
python -m fl_surf_check --zip 32118 --details

# Only tell me if today is genuinely unusual
python -m fl_surf_check --zip 32118 --rare-only

# Stricter: a great day is only worth one extra hour per SD
python -m fl_surf_check --zip 32118 --minutes-per-sd 60

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

## The value calculation

Surf quality and drive time are different units, so the tool converts both into
the same one — **standard deviations of surf** — and subtracts:

```
value = sigma(surf)  -  drive_minutes / minutes_per_sd
```

Two optional terms join this once you start logging sessions — `itch` lifts
every spot the longer you have been out of the water, and `novelty` handicaps
the ones you surf most. Both are zero until then, so the formula above is what
runs by default. See [Your surf log](#your-surf-log).

**One standard deviation above normal is worth 120 minutes of driving.** Two is
worth four hours. The sign is the answer: positive means go, zero is
break-even, and the magnitude is your margin.

```
 0.5 sigma  ->  1h00m of driving
 1.0 sigma  ->  2h00m
 2.0 sigma  ->  4h00m
 3.0 sigma  ->  6h00m
```

The `--details` view shows the arithmetic:

```
value = +0.51 SD - 24m/120min = +0.31   (still worth it with 37m of driving)
```

### Why 120 and not 60

Calibrated against the record rather than guessed. At 60 min/SD the single best
surfable day in five years — New Smyrna, 14 September 2023, 5.1ft at 11s with
3mph offshore wind, an effective **+2.63 sigma** — scored **−0.29** from Tampa.
It justified 2.6 hours against a 2h55m drive, so the tool would have said stay
home on the best day it has ever seen. At 120 it scores +1.17. Change it with
`--minutes-per-sd`.

### Why distance is not itself z-scored

Standardising distance across the spot list would make it relative to whichever
spots happen to be listed, destroying the absolute calibration the score
depends on. Measured against the shipped spot list:

| | a distance z of −1.0 means |
|---|---|
| from Daytona | 30 miles |
| from Tampa | 109 miles |

"One sigma closer" cannot be worth a fixed 120 minutes in both places. Raw
minutes keep the units absolute and comparable between users.

### What replaced the old blend

Earlier versions ranked on `worth = 10 × [w × surf/10 + (1−w) × closeness]`,
with an exponential distance decay. That number had **no natural zero** — a
flat, unsurfable day scored 5.6/10, which reads as "maybe," and its meaning
shifted with `--surf-weight` and `--decay-miles`. The blend survives only as a
fallback for `--no-history`, where there is no sigma to work with.

---

## Planning several days ahead

`--days N` (1–7) widens the window. Each spot is still scored on its single
best surfable hour, and `BEST` shows which day that falls on — so a Wednesday
run with `--days 5` covers Thursday through Sunday:

```bash
python -m fl_surf_check --zip 33613 --days 5
```

With more than one day it also prints a **best day** summary, since a 41-spot
by 5-day matrix is unreadable and the question you are actually asking is
"which day, and where":

```
  BEST DAY OF THE NEXT 5
     Sat 22 Aug   -0.38  Spessard Holland            11:00  swell 1.0ft/8s
     Sun 23 Aug   -0.24  Apollo Beach (Canaveral NS  12:00  swell 1.2ft/8s
  -> Mon 24 Aug   -0.15  Apollo Beach (Canaveral NS  09:00  swell 1.2ft/8s
     Tue 25 Aug   -0.47  Apollo Beach (Canaveral NS  08:00  swell 1.1ft/8s
     Wed 26 Aug   -0.61  Apollo Beach (Canaveral NS  10:00  swell 1.0ft/7s
```

**Seven days is the hard ceiling**, and it is the swell model that sets it, not
the atmosphere. Measured: at `forecast_days=7` the marine API returns 168 hours
with zero gaps; at 10 the final day comes back empty, and at 16 more than a
third of the window is missing. Wind and CAPE run to 16 days, but without swell
they are no use.

**Trust the far end less.** Florida swell decorrelates quickly — the
day-over-day correlation of daily-max swell height is 0.74 at one day but only
0.26 by day four:

| lead | correlation | median change |
|---|---|---|
| 1 day | 0.74 | 19% |
| 2 days | 0.49 | 29% |
| 3 days | 0.33 | 35% |
| 5 days | 0.21 | 38% |

The forecast model does much better than persistence, but the same physics
applies: day 5 is directional, not precise. Runs of four days or more say so in
the output.

---

## Which hour it scores

Not the one you happen to run it in. The tool scores **every surfable hour in
the next 24 hours** and reports the best, shown in the `BEST` column — every
other figure in that row belongs to that hour.

This matters for two reasons.

**The comparison was otherwise unfair.** The baseline is built from each day's
*best* hour, so scoring it against an arbitrary current hour is
apples-to-oranges. Measured on the record, a randomly chosen hour scored a
median of **p34** against that baseline when it should average p50 — a
systematic 16-point understatement of every rarity score. Scoring the window
and taking its maximum puts both sides in the same units: median p48.

**Florida's wind swings hard through the day.** Measured at Cocoa Beach over
July–August, the sea breeze rotates the wind from 232° at 05:00 (offshore) to
125° by 15:00 (onshore), roughly doubling in speed:

```
local hr   wind mph   dir   wind score
   5:00        4.2    232        6.0   offshore-ish
  11:00        4.9    216        6.1   offshore-ish
  15:00        6.8    125        5.2   ONSHORE
```

Same swell, very different surf. So the honest answer is often *"not now, but
go tomorrow at 11"* — which is what the tool now says, rather than judging the
whole day by whichever hour you asked in.

---

## Rarity: is today one of the good ones?

The 0–10 surf score answers *"is this rideable?"* on an absolute scale. It
cannot answer the question you actually care about — *"is today unusually
good?"* — because that depends entirely on what Florida normally does. A
2.5ft/11s day is unremarkable in Hawaii and a red-letter day here.

So the tool pools **the full historical record of daily-maximum swell across
all 41 spots** — every day since October 2021, not windowed to any time of
year — into a single statewide baseline, and reports today as a percentile
against it. That's the `VS NORM` column. See
[Why the baseline isn't seasonal](#why-the-baseline-isnt-seasonal) for the
tradeoff that choice makes.

### Why one statewide baseline, not one per spot

Per-spot baselines grade every break on its own curve, which flatters the weak
ones: a mediocre Pensacola day would read as "p90" beside a genuinely excellent
Sebastian Inlet day, because each is only compared to itself. Since the whole
point is choosing *where to drive*, they need one shared yardstick — a spot
that is simply better should read as better.

### Why the baseline isn't seasonal

An earlier version windowed the baseline to &plusmn;14 days around the date
being scored, so a February day was judged against other Februaries. That was
removed: the baseline now pools **every day in the full record**, regardless
of time of year.

The tradeoff is real, and it changes what "normal" means. Measured on the
actual record:

| | late-August window (old) | full record (current) |
|---|---|---|
| median swell height | 1.25ft | **1.51ft** |
| p90 swell height | 2.95ft | **3.54ft** |

Winter nor'easters run bigger than summer trade-wind swell, and pooling them
in pulls the whole distribution up. The practical effect: a typical *August*
day now reads as somewhat below normal, where a seasonal baseline would have
called it ordinary for the time of year. In exchange, "normal" stops shifting
meaning with the calendar — it answers one question, "how does this compare
to Florida surf overall," rather than a question whose answer depends on
today's date. It also means the baseline no longer needs rebuilding every time
the season moves, and every day of the 6-year record now counts as evidence,
so shrinkage (below) has far less work to do than it used to.

### Why percentile rank, not min–max

Surf is heavily right-skewed: a handful of tropical systems own the top of the
range. Measured on the real record, min–max normalisation maps a **median** day
to 0.10 and even a strong p90 day to only 0.24 — every ordinary day bunches
into the bottom quarter of the scale and the signal is lost. Percentile rank asks
the same question ("where does this sit in the observed population?") but
spreads evenly by construction and is immune to how extreme the extremes are.

### Night hours are excluded

You cannot surf in the dark, so night is dropped before the baseline is built —
otherwise 3am swell would help set "normal" and could supply a day's maximum.

The cutoff uses real solar geometry rather than fixed clock hours, because
Florida's daylight swings about three hours between solstices. Validated
against published Cocoa Beach sunrise/sunset at both solstices and equinoxes,
worst error **8 minutes**. The threshold is civil twilight (−6°), not true
sunrise: first light is when Florida's wind is calmest and most offshore, and a
0° cutoff would discard the best hour of many days.

It changes the numbers less than you would expect — p50 unchanged, p90 −2.3% —
because daily maxima are robust to dropping hours. It is a correctness fix, not
a scoring change.

### Why thin evidence gets shrunk

The record only starts in October 2021, so a "top 2% day!" claim may rest on a
young dataset. Rather than let a thin sample shout, the percentile is pulled
toward normal in proportion to the evidence behind it — Laplace's rule of
succession, generalised from a proportion to a percentile:

```
shrunk = (percentile x n + 50 x prior) / (n + prior)
```

With the full baseline (n ≈ 1,787 days, pooling every day in the record) a p98
reading lands at p97 — barely moved, because there is now a great deal of
evidence behind it. That's the direct benefit of not windowing to a season:
a thin 130-day slice used to pull that same p98 down to p89. With only 8 days
of evidence, the same reading still lands near p60 — visible, but not shouted
about.

Because shrinkage imposes a ceiling, the `STANDOUT` / `RARE` / `BEST IN NYR`
labels are thresholds on the *attainable* range rather than fixed percentiles.
Fixed cutoffs would be unreachable dead code.

---

### Why the standard deviation is geometric

Measured on the pooled full-record baseline, raw swell height has a
**skew of 1.96**. A plain z-score on that misbehaves badly:

| | raw values | log space |
|---|---|---|
| skew | 1.96 | **&minus;0.12** |
| z = 0 lands at | p62 | **p50** |
| +1 SD | p89 | **p83.5** (textbook: 84.1) |
| biggest day on record | z **+9.7** | z **+3.4** |

At 120 minutes per SD, that raw +9.7 would have justified a *nineteen-hour*
drive — an even starker case for log space now that the fuller record includes
more of the tail. Taking logs first restores σ's textbook meaning, and the
same day becomes a well-behaved +3.4σ. There is no separate cap on this
anywhere in the value formula — every term runs unbounded in both directions,
so a big enough σ really can outweigh a very long drive (and the surf-log
factors are uncapped for the same reason). Below-normal days earn nothing but are never penalised — the real
drive time is already the honest cost.

---

## Your surf log

Two factors no forecast can supply. Both read from a personal log and are
**zero until you record a session**, so this is inert until you opt in.

```bash
python -m fl_surf_check --surfed "apollo"                  # today
python -m fl_surf_check --surfed "ponce" --on 2026-08-18
```

Names are fuzzy-matched, and an unrecognised one is **refused rather than
guessed** — a wrong guess silently logs a session against the wrong break and
quietly skews everything downstream. The log is a JSON file at
`~/.fl_surf_log.json` (override with `$FL_SURF_LOG`), written atomically,
outside the repo and gitignored: it is a record of where you go and when, which
does not belong in a public repository.

The full formula becomes:

```
value = sigma(surf) + itch - novelty - drive_minutes / minutes_per_sd
```

### Itch — days since you last surfed

Linear and **uncapped**, at `--itch-rate` σ per day:

```
 3 days   +0.15σ   +18 min      30 days   +1.50σ   +180 min
 7 days   +0.35σ   +42 min      45 days   +2.25σ   +270 min
```

**This never changes the ranking.** It is identical for every spot, so it
slides the whole board and moves the go/no-go line without reordering anything
— which is exactly "make me more willing to drive", not "send me somewhere
different".

Because it is uncapped, it grows without limit if the log goes stale. That is
right for a real dry spell and is also the failure mode of *forgetting to log*.
An empty log is deliberately treated as **zero itch, not infinite** — a fresh
install should not open by demanding you drive across the state.

### Novelty — how often you have surfed each spot

A z-score of visit counts across the whole spot list, so spots you keep
returning to get handicapped and neglected ones surface. **This one does
reorder.** Uncapped, at `--novelty-weight`.

Two corrections to a plain z-score, both for measured reasons:

**Log-transform first.** Visit counts across 41 spots are extremely
zero-inflated, and the raw z-score of a heavily-used spot runs away. On a
realistic 200-session log, raw counts penalise the most-surfed spot **1.7×
harder** than log space does. In log space, equal *ratios* of visits cost
equal increments — 1→10 costs about what 10→100 costs.

**Shrink by evidence.** Even in log space, a young log produces huge z values
simply because almost everything is zero: measured, a single logged session has
a raw z of **+6.3**, which at 120 min/σ is a *twelve-hour* handicap from one
surf. Scaling by `n / (n + 20)` — the same Laplace pattern as
`shrink_percentile` — keeps a three-session log quiet while a two-hundred-session
one applies at nearly full strength.

```
sessions logged     most-surfed spot      never-surfed
      1                 +0.15σ   +18min       −0.00σ
     10 / 3 spots       +0.82σ   +98min       −0.04σ
     60 / 6 spots       +1.37σ  +164min       −0.15σ
```

### The honest caveat

Both factors are worth exactly as much as your logging discipline. An unlogged
session is invisible: novelty will think you have never surfed a spot you go to
weekly, and itch will keep climbing as though you have been dry. Nothing here
can detect that — `--no-personal` turns the whole thing off if the log drifts
out of date.

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

**"I'd drive further than that for a good day."**
Raise `--minutes-per-sd`. At `240`, one standard deviation buys four hours.

**"I'm not driving more than an hour, ever."**
Lower it, or just set `--max-miles`. At `--minutes-per-sd 60` a 1-SD day is
worth only an hour.

**"Only tell me when it's genuinely on."**
`--worth-only` hides everything with a negative value score. `--rare-only` is
stricter still — only days well above the seasonal norm.

**"I care about different things than these weights."**
Every scoring function in `scoring.py` is pure and has explicit control points.
`WEIGHTS` sets the balance of height / period / wind / direction;
`WIND_SIGMA_RANGE` sets how far wind alone can move the value score.

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

- **All 41 spots are fetched in 2 API calls, not 82.** Open-Meteo accepts
  comma-separated coordinate lists, so wave data for every spot arrives in one
  request and wind data in another.
- **Responses are cached locally for 30 minutes** (`requests-cache`). Running it
  five times in a row hits the network once.
- **The historical baseline is fetched once and cached for 30 days.** An
  earlier version keyed the cache to the date being scored (needed for the
  seasonal window that no longer exists), so it expired at midnight and re-ran
  the full pull every single day — precisely the thing that trips the rate
  limit. Now that the baseline pools the whole record rather than a moving
  window, freshness is purely a matter of age: it is a single batched request,
  but a large one — millions of samples across 41 spots and the full history —
  and running it every invocation would trip Open-Meteo's per-minute rate
  limit, which then starves the live conditions request in the same run.
  The cache (`.fl_surf_climatology.json`, gitignored) is what prevents that.
- **Rate-limit errors are not retried.** Open-Meteo's limit is per-minute, so
  short backoff could never outlast it and each attempt would spend more of the
  quota. The tool fails fast with an accurate message instead.
- **Tides are fetched per-station, not per-spot** — 41 spots share 10 NOAA stations.
- **Zip lookups are offline** after the first run.

If OSRM or NOAA is unreachable, the tool degrades rather than failing: distances
fall back to a straight-line estimate (marked `~` in the output) and tide simply
drops out of the score.

---

## Spot list

**41 spots**, and that number is measured rather than chosen. Ranked by the
share of days each supplies a good session over the 2021-10 → 2026-08 record,
and by how often each would be the top recommendation from a range of Florida
origins.

### How many spots is the right number

Two bounds. Below, the marine model is the limit: the grid is **1/12° ≈ 5.7
miles**, so 77 candidate breaks collapsed into only 58 distinct forecasts. Two
spots in one cell get byte-identical surf and differ only in drive time. The
shipped 41 map to 36 distinct cells.

Above, most spots never win. Simulating every day of the record from seven
different Florida origins:

| origin | spots that ever win | top 8 covers |
|---|---|---|
| Tampa | 23 of 75 | 97.6% |
| Daytona | 9 | 99.9% |
| Miami Beach | 17 | 92.9% |
| Jacksonville Bch | 13 | 99.3% |
| Pensacola | 6 | 100% |
| Vero | 11 | 99.8% |

Any one surfer needs 8–10 spots. But the list serves every origin, and the
**union of each origin's top 8 is 40** — which is where 41 comes from.

### The best water in the state

```
 1 Jupiter Inlet          35.8% of days a good session
 2 Juno Beach             32.7%
 3 Apollo Beach           32.3%
 6 Hobe Sound             30.5%
 8 Lake Worth Pier        27.6%
...
23 Sebastian Inlet        19.6%
40 Cocoa Beach Pier        8.1%   (of 75 candidates)
```

Two caveats worth stating plainly. **Cocoa Beach ranks low** because Cape
Canaveral shelters it from NE swell — it is famous for being consistent and
accessible, not big. And **Sebastian Inlet ranks lower than it deserves**: the
scoring only sees open-ocean swell, while Sebastian's reputation comes from the
jetty and sandbar wrapping that swell into a defined peak. No grid cell knows
that. The same blind spot applies to every inlet and reef break here.

### The Gulf spots exist for a reason

The west-central Gulf beaches are poor in absolute terms — a good session on
1–5% of days, against Jupiter's 36%. But simulated from a Tampa zip they win
**roughly a third of all days**, because three hours of driving costs 1.5σ and
they only need to beat the Atlantic by that much. That is exactly the trade the
value score exists to make, and it could not be made while those beaches were
missing from the list.

### Regions covered

**Northeast FL** (5) · **East Central / Space Coast** (12) · **Treasure Coast**
(6) · **Palm Beaches** (4) · **Broward** (2) · **Miami-Dade** (1) · **Gulf
Coast, west central** (8) · **Gulf Coast, Panhandle** (3)

---

## Data visualizations

Two self-contained HTML pages in the repo root, built from the same historical
data the CLI scores against. Open either directly in a browser — no server,
no build step.

- **`swell-distribution.html`** — the pooled statewide swell-height and
  -period distributions, plus the raw-vs-log comparison behind the geometric
  σ (why standard deviation is measured on `log(height)`, not height itself).
- **`CODE_EXPLANATION.html`** — a plain-language walkthrough of the whole
  codebase, organised as the path of a single run rather than file-by-file.
  Includes a "why things are shaped oddly" section documenting the bugs that
  produced several of the stranger-looking design decisions.
- **`florida-surf-spots.html`** — all 41 spots on a real Florida coastline,
  shaded by the tool's own value score from a specific origin (33613), not by
  raw surf quality. Jupiter Inlet has the best swell in the state and still
  reads pale, because a four-hour drive prices most of that advantage away.

**Both are snapshots, not live views.** Each embeds a JSON blob computed once
against the historical record and the origin current at build time — they do
not re-run the pipeline in the browser, and they will not reflect a spot list
change, a scoring change, or another year of history without being rebuilt.
Treat them as "what the data showed on the day this was generated," not as a
dashboard.

---

## Project layout

```
fl_surf_check/
├── spots.py        # The 41 spots: coordinates, beach bearings, tide stations
├── location.py     # Zip code → lat/lon (pgeocode, Nominatim fallback)
├── distance.py     # Great-circle + OSRM driving distance, decay function
├── conditions.py   # Open-Meteo + NOAA fetching, batched and cached
├── climatology.py  # Full-record statewide swell baseline, pooled and cached on disk
├── surflog.py      # Your session log; feeds the itch and novelty factors
├── scoring.py      # All the scoring math (pure functions, no I/O)
└── cli.py          # argparse, orchestration, table rendering

tests/
├── test_scoring.py       # 32 tests on the scoring math
├── test_climatology.py   # 70 tests on baselines, rarity, value, storms, daylight
├── test_surflog.py       # 36 tests on the surf log, itch and novelty
└── test_cli_offline.py   # 23 end-to-end tests with the network mocked
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
161 passed in 3.54s
```

All 161 tests run **offline** — network calls are mocked and `build_baseline`
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

## Adding another region later

The tool is deliberately Florida-only: 41 hardcoded spots, and scoring curves
tuned to Florida beach breaks. If you want it somewhere else, here is exactly
what is and isn't in the way — measured against real Pacific Northwest data
rather than guessed.

**Already region-agnostic.** Open-Meteo's marine API is global and returns
clean history for Westport, WA back to 2021. NOAA CO-OPS covers every US coast,
OSRM routes worldwide, `pgeocode` handles any US zip. Nothing in
`conditions.py`, `distance.py` or `climatology.py` knows about Florida —
`build_baseline` already pools whatever spots you hand it.

**Three things are coupled.**

1. **The spot list** (`spots.py`) is a module-level constant. It would need to
   become data selected by proximity to your origin. Mechanical.

2. **The baseline is pooled statewide.** Point `build_baseline` at a different
   set of spots and you get a different region's climatology for free — the
   machinery already supports it.

3. **The scoring curves are Florida-shaped**, and this is the real work.
   `HEIGHT_CURVE`, `PERIOD_CURVE` and `WIND_PENALTY_CURVE` in `scoring.py` are
   named constants precisely so a second region can supply its own.

### Why the curves matter more than the spot list

Westport, WA has a **median** day of 4.72ft and a p90 of 9.32ft, against
Florida's 1.51ft and 3.54ft. Its biggest day on record is 22.4ft; Florida's is
14.5ft. Fed to the Florida height curve, the ordering inverts:

| Westport day | ft | Florida height score |
|---|---|---|
| small (p10) | 2.2 | 5.7 |
| median (p50) | 4.7 | **9.8** |
| big (p90) | 9.3 | **6.6** |
| huge (p99) | 14.4 | 3.8 |

A typical day scores higher than a genuinely big one, because the curve encodes
"Florida sandbar" rather than "wave." Scored against Florida's *climatology* it
is worse still — an ordinary Westport Tuesday reads as `BEST IN 6YR`, so the
sigma term pins high and stops discriminating between days entirely.

A Pacific Northwest curve would peak somewhere near 8–12ft. That is a local
surfer's judgement, not a coding problem.

Note this is an argument for keeping rarity **monotonic** in height
(`APPLY_CLOSEOUT_ROLLOFF = False`): a rarity score that does not bake in a
closeout assumption travels between coasts far better than an absolute curve
that does.

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
nearest wet grid cell, up to ~14 miles. The 41 spots collapse into 36 distinct
cells, so four groups get identical conditions and are separated only by drive
time: St. Augustine/Vilano, Flagler/Ormond, Patrick/Indialantic, and New
Smyrna/Ponce/Daytona Beach Shores.

**Bigger always scores better, even when it shouldn't.** Rarity is monotonic in
swell height by design (`APPLY_CLOSEOUT_ROLLOFF = False`), so a 15ft swell
scores higher than a 5ft one. The *absolute* surf score disagrees — its height
curve peaks at 5ft and falls away above 7ft, because Florida sandbars close out
rather than hold shape — so `SURF` and `VALUE` will diverge on very large days.
That is deliberate: the size at which a break stops being fun depends on who is
paddling out. Set `APPLY_CLOSEOUT_ROLLOFF = True` to make rarity inherit the
rolloff.

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
