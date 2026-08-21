# Security review

Every third-party dependency and every piece of borrowed code in this project
was reviewed before being used. This is what was checked and what was found.

## Method

For each candidate package and repository:

1. **Read the source** of anything small enough to read in full
   (`openmeteo_requests` is 162 lines; the borrowed rating algorithm is 95).
2. **Grepped for dangerous primitives** across the full source tree:
   `eval(`, `exec(`, `__import__`, `os.system`, `subprocess.*`,
   `pickle.loads`, `base64.b64decode`, raw socket use, `shutil.rmtree`.
3. **Enumerated every hardcoded network endpoint** to confirm each package only
   talks to the service it claims to.
4. **Checked the dependency tree** — a small clean package that pulls a large
   one has a large audit surface.
5. **Checked the license** for anything that would constrain redistribution.

## Findings

### Accepted dependencies

| Package | Version | License | Endpoints contacted | Result |
|---|---|---|---|---|
| `requests` | ≥2.31 | Apache-2.0 | caller-specified | Clean. Ubiquitous, heavily audited. |
| `openmeteo-requests` | 1.7.5 | Apache-2.0 | none hardcoded (URL passed by caller) | Clean. Read in full — 162 lines, a thin flatbuffers decoder over HTTP. Official Open-Meteo client. |
| `requests-cache` | 1.3.3 | BSD-2-Clause | none | Clean. One `base64.b64decode` in `serializers/preconf.py`, used to deserialize cached binary response bodies — expected and benign for a cache library. |
| `pgeocode` | 0.5.0 | BSD-3-Clause | `download.geonames.org`, `symerio.github.io` | Clean. Downloads a postal-code dataset once, then works offline. |
| `geopy` | 2.5.0 | MIT | per-geocoder, only when called | Clean. Only the Nominatim geocoder is used, and only as a fallback. |

No dangerous primitives were found in any accepted dependency.

### Rejected dependencies

**`retry-requests` — rejected on licensing.**
Open-Meteo's own documentation examples use this package, and it is
functionally fine. But it is **GPLv3+**, a strong copyleft license. For a
project intended to be published and reused, taking a GPL dependency for
what amounts to a retry loop is a poor trade. Replaced with a 10-line
`_with_retry` helper in `conditions.py`. The whole project stays MIT.

**`noaa-coops` — rejected on dependency surface.**
It wraps the same NOAA endpoint this project calls, which is superficially
attractive. But it requires **`pandas` *and* `zeep`** — zeep being a complete
SOAP client stack — to support parts of the NOAA API this project never
touches. That is a very large transitive audit surface in exchange for
replacing a single documented `GET` request. The direct `requests` call in
`conditions.py` is ~25 lines and depends on nothing new.

Its source *was* reviewed and found clean (all 29 hardcoded endpoints are
`*.noaa.gov`, no dangerous primitives) — the rejection is about proportionality,
not suspicion.

**`uszipcode` — rejected, does not build.**
Fails to install on Python 3.11 (its `atomicwrites` dependency errors during
wheel build). `pgeocode` covers the same need. Not investigated further.

### Borrowed source code

Code adapted from other repositories was read line by line before use. Nothing
was copied wholesale, and no code was taken from a repository without a clear
license.

| Repo | License | What was taken | Review notes |
|---|---|---|---|
| [hugosrc/surf-forecast-api](https://github.com/hugosrc/surf-forecast-api) | Apache-2.0 | The *design* of the rating algorithm, reimplemented in Python | Read `src/services/rating.ts` in full (95 lines, pure math, no I/O). It also ships a test spec, which is a good sign for correctness. Nothing executable was copied. |
| [ryansurf/cli-surf](https://github.com/ryansurf/cli-surf) | MIT | The Open-Meteo caching-client pattern and the nearest-NOAA-station approach | Reviewed. One `subprocess.run` exists in `src/send_email.py` — it shells out to `curl` with a URL from its own config to build an email body. Benign in context, but it is **not** used here; the email module was not adapted. |
| [mpiannucci/surfnerd](https://github.com/mpiannucci/surfnerd) | **no license file** | **nothing** | Reviewed for reference only. With no license, its code carries no grant of use — so nothing from it was taken, on principle. |
| [swrobel/meta-surf-forecast](https://github.com/swrobel/meta-surf-forecast) | MIT | nothing | Reviewed; its approach depends on Surfline/Spitcast endpoints this project deliberately avoids (see below). |

### A deliberate omission: Surfline

Surfline has richer surf-specific data, including its own 1–10 rating, and
several GitHub projects access it through reverse-engineered endpoints.

**This project does not use it.** Those endpoints are undocumented and
unofficial: they can change or disappear without notice, using them is a poor
foundation for something you want to keep working, and scraping a commercial
service's private API sits in a legal grey area. Every source used here is one
that is *published for public use*.

## Runtime security properties

- **No credentials anywhere.** No API keys, no tokens, no accounts. Nothing to
  leak, and nothing to put in a `.env`.
- **No inbound network surface.** It's a CLI. It opens no ports and listens for
  nothing.
- **No code execution from network data.** API responses are parsed as JSON or
  flatbuffers and read as numbers. Nothing is `eval`'d, deserialized with
  `pickle`, or executed.
- **Location stays local.** Your zip code is resolved offline after the first
  run. Coordinates are sent only to the weather, tide, and routing APIs that
  need them to answer the question.
- **Cache is local and bounded.** `requests-cache` writes a SQLite file
  (`.fl_surf_cache.sqlite`) in the working directory, holding only public
  forecast responses. It's gitignored. Deleting it is always safe.
- **Fails closed, not loud.** Every network call is wrapped; an unreachable API
  degrades the output (missing data, estimated distance) rather than crashing or
  retrying indefinitely.

## Re-running this audit

```bash
# dangerous primitives across all installed deps
grep -rnE "\beval\(|\bexec\(|os\.system|subprocess\.(run|call|Popen)|pickle\.loads|__import__\(" \
  $(python -c "import site; print(site.getsitepackages()[0])")

# every endpoint a package can contact
grep -rnoE "https?://[a-zA-Z0-9./_%{}-]+" <package_dir> | sort -u

# license check across the tree
pip-licenses --format=markdown
```
