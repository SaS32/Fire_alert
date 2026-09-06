# AI_CONTEXT.md — project brief for an AI assistant

This file exists so that an AI model (or a developer) picking up this project cold
can understand what it is, how it works, and how to modify it safely. If you are an
AI assistant helping the user change this project, read this first.

## What this project is

A personal wildfire-notification system. It queries NASA FIRMS (Fire Information
for Resource Management System) near-real-time active-fire data on a schedule and
pushes a Telegram message when new fires are detected in a target region
(currently Bulgaria). It runs on GitHub Actions' free tier; there is no server and
no database — persistence is a single JSON file committed back to the repo.

The project is now maintained from a PC with a local clone, git CLI, and a shell
available, so normal editing applies: targeted diffs/patches, running the script
locally, and committing directly are all fine. (Earlier versions of this file
required full-file replacements because the user worked only from an Android
phone via the GitHub mobile browser — that constraint no longer applies.)
Telegram remains the delivery channel and the phone is still the place alerts are
read, but development happens on desktop.

## Runtime model

- `.github/workflows/fire-alerts.yml` is the scheduler. It runs hourly
  (`cron: "0 * * * *"`, UTC) and also on manual `workflow_dispatch` with an input
  `mode` of `check` or `report`.
- Each run: checks out the repo, installs `requests`, runs
  `fire_alerts_action.py` once, then commits `seen_fires.json` back so state
  survives between runs.
- Secrets (`FIRMS_MAP_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`) are stored as
  GitHub Actions repository secrets and injected as environment variables. They are
  never in the code.
- `.github/workflows/find-hotspots.yml` is a second, independent workflow that
  runs `find_hotspots.py` — monthly (`cron: "17 6 1 * *"`) and on
  `workflow_dispatch` with inputs `min_days`, `recur_km`, `show_all`. It requests
  `permissions: contents: read`, takes no secrets, installs nothing (the script is
  stdlib-only), and commits nothing. Its whole output is the report, tee'd to the
  job log and re-printed into `$GITHUB_STEP_SUMMARY` so it is readable in the
  GitHub mobile app. It exists because the alert workflow's state file is the only
  record of what keeps repeating, and the user should not need a clone to read it.

## Configuration (environment variables set in the workflow)

- `FIRE_COUNTRY` (default `BGR`): 3-letter ISO code, used only if `FIRE_BBOX` unset.
- `FIRE_BBOX` (`"west,south,east,north"`): bounding box for the FIRMS *area*
  endpoint. Currently set because the FIRMS *country* endpoint has been
  intermittently unavailable (NASA-side outage). When `FIRE_BBOX` is set the script
  uses the area endpoint; otherwise it uses the country endpoint.
- `FIRE_SOURCES` (default `VIIRS_NOAA20_NRT,VIIRS_NOAA21_NRT,MODIS_NRT`):
  comma-separated satellite feeds. Note `VIIRS_SNPP_NRT` was dropped because the
  Suomi-NPP feed went stale (data availability lagged several days, causing HTTP
  400s when querying "today"). Always sanity-check a feed against
  `https://firms.modaps.eosdis.nasa.gov/api/data_availability/csv/<KEY>/all`
  before adding it.
- `FIRE_MIN_CONFIDENCE` (default `nominal`): VIIRS uses low/nominal/high; MODIS
  uses a 0–100 integer. `confident_enough()` translates between the two via
  `CONF_PERCENT` (NASA's MODIS bands: low 0–29, nominal 30–79, high 80–100), so a
  word threshold means the same thing to both families and a numeric one does
  too. Before this, a word threshold fell through to a hardcoded 80 for MODIS,
  which silently held MODIS to "high" while VIIRS ran at nominal. Don't
  reintroduce a bare numeric default here.
- `RUN_MODE` (`check` or `report`): set from the workflow's `inputs.mode`,
  defaulting to `check` for scheduled runs.
- `FIRE_CENTER_NAME` / `FIRE_CENTER_LAT` / `FIRE_CENTER_LON` (default
  `Sofia` / `42.6977` / `23.3219`): the reference point every fire is measured
  from. Fires are reported as "<distance> <compass direction> of <name>" and
  ordered nearest first. Read through `env_float()`, which fails *open* like
  `load_excluded_zones()` — a missing, non-numeric or out-of-range value logs a
  warning and falls back to the default rather than raising. Keep that: a
  cosmetic setting must never be able to stop an alert.

## The FIRMS API (essentials)

- Area endpoint:
  `https://firms.modaps.eosdis.nasa.gov/api/area/csv/{KEY}/{SOURCE}/{west,south,east,north}/{DAY_RANGE}`
- Country endpoint:
  `https://firms.modaps.eosdis.nasa.gov/api/country/csv/{KEY}/{SOURCE}/{ISO3}/{DAY_RANGE}`
- Returns CSV. Columns include `latitude`, `longitude`, `acq_date`, `acq_time`,
  `confidence`, `satellite`.
- On a bad request the body may be plain text starting with "Invalid" while still
  returning HTTP 200, so the code checks the body text, not just the status.
- Rate limit: ~5000 transactions / 10 minutes per key. This project uses a handful
  per run, so it's nowhere near the limit.

## `geo.py` (shared helpers)

Distance, bearing and the reference point live here so `fire_alerts_action.py`
and `find_hotspots.py` agree on what "34 km NE of Sofia" means. Stdlib only and
it reads no secrets — that matters, because `find_hotspots.py` must stay
runnable without a NASA key, so it can never import `fire_alerts_action.py`
(which raises on a missing `FIRMS_MAP_KEY` at import time). Put anything both
scripts need here rather than duplicating it.

`km_between()` (flat) and `great_circle_km()` (haversine) both exist on purpose:
the flat one is used for the sub-kilometre zone and cluster checks where it is
cheap and accurate enough, the haversine one for distance from the centre point,
which can be hundreds of km.

## Program structure (`fire_alerts_action.py`)

Pipeline in `main()`:

1. `fetch_all_with_retries()` — fetches every source. Failed sources are retried
   after `RETRY_DELAYS` (5 min, then 10 min). Partial success proceeds with what
   it has; total failure after retries sends a single Telegram outage warning and
   raises (marking the run failed).
2. Filter each row: `confident_enough()` then `in_area()`.
3. `in_area()` — point-in-polygon test against `BG_POLYGON` (ray casting), OR
   within `BUFFER_KM` of any border segment (`near_border()` +
   `km_to_segment()`, an equirectangular approximation adequate at this scale and
   latitude). This is what removes Turkish/Romanian fires while keeping border
   ones. Controlled by `FILTER_TO_POLYGON`.
4. `zone_containing()` — find the user-defined circle a detection falls inside
   from `excluded_zones.json` (hot factories, flares, landfills). Default radius
   `EXCLUDE_RADIUS_KM` = 0.2 km, overridable per zone. **Applied per detection,
   before clustering, deliberately**: `CLUSTER_DEG` is ~2 km, so filtering by
   cluster centroid instead would let a factory swallow a genuine fire up to
   2 km away and suppress both. Suppression is silent in Telegram (explicit user
   requirement — excluded zones must not appear in notifications, including
   `report` mode) and logged to stdout only. The suppression decision itself
   lives in `main()`, not in `zone_containing()`, because a zone carrying an
   optional `max_frp` lets a detection hotter than that through — the safety
   valve this file used to list as considered-but-unimplemented. Without
   `max_frp` a zone suppresses unconditionally, exactly as before.
   `row_frp()` returns None rather than 0 for a missing value, so a feed that
   omits the column can never be mistaken for a cold fire and can never trip the
   valve.
5. Dedup by `detection_id()` = lat_lon_date_time. Compare against `seen` loaded
   from `seen_fires.json`; the difference is "new". The id starts with the
   coordinates, so **never sort these strings directly to mean "by time"** — that
   sorts by latitude. `acquired_at()` is the time sort key, and it zero-pads
   `acq_time` because FIRMS strips leading zeros (00:43 arrives as `43`).
6. `cluster_fires()` — greedy single-pass spatial clustering: a detection joins an
   existing cluster if within `CLUSTER_DEG` in both lat and lon, updating a running
   centroid; otherwise starts a new cluster. Not a true metric clusterer, so a fire
   front wider than ~2 * CLUSTER_DEG can split into adjacent clusters. Acceptable
   for alerting.
7. `by_distance()` — annotates each cluster with `dist_km` (haversine, via
   `great_circle_km()`) and `direction` (16-point compass, `compass_from_center()`)
   relative to `CENTER_LAT`/`CENTER_LON`, then returns them sorted nearest first.
   Must run *after* clustering, because the distance applies to the cluster
   centroid, not to individual detections. `great_circle_km()` is deliberately
   separate from `km_between()`: the latter's flat-earth approximation is fine
   for the sub-kilometre excluded-zone checks but drifts over the few hundred km
   a fire can be from the centre point. Everything downstream assumes the list is
   already annotated and sorted — `fmt_clusters()` and `send_map_pins()` no
   longer sort, so anything new that formats clusters must pass through
   `by_distance()` first.
8. `fmt_title()` builds the first line for both modes, and `marker()` the
   per-fire bullet. A cluster within `NEAR_KM` (20 km) of the centre is "near":
   the title switches from the counts to `🚨 FIRE 8 KM NE OF SOFIA — ...` and the
   bullet from `•` to `🚨`. The reasoning is that Telegram's notification preview
   shows only the first line, so on the one day it matters that line must carry
   the alarming fact rather than a fire count. Ordinary days are unchanged.
9. Cooldown (`check` mode only). A wildfire produces newly-timestamped
   detections on every satellite pass, so step 5 keeps finding "new" activity
   for as long as it burns and the alert repeated hourly for days.
   `in_cooldown()` drops a cluster that sits within `COOLDOWN_MATCH_KM` (3 km) of
   an entry in `alert_cooldown.json` younger than its window —
   `NEAR_COOLDOWN_HOURS` (2h) if the fire is within `NEAR_KM`, else
   `COOLDOWN_HOURS` (6h). `remember_alerts()` then records what was actually
   sent and prunes anything past the longest window. Deliberately **not**
   applied in `report` mode: that is an explicit request for current status.
   Suppressed detections still enter `seen`, so they never queue up. Like the
   other state files this one fails open — unreadable means no cooldowns, and
   a negative age (clock skew, hand edit) counts as expired. It must never be
   able to swallow an alert, only repeat one.
10. Output: `report` mode summarizes all current fires (clustered); `check` mode
   messages only new activity. Both use `fmt_clusters()`, capped at `MAX_ITEMS`,
   listing nearest fire first; each line reads
   `Fire 34 km NE of Sofia (42.812,23.678) - N detection(s), last seen ... UTC`.
   The title line names the nearest fire, because Telegram's notification preview
   only shows the first line. After the text message, `send_map_pins()` sends a
   satellite image for the *nearest* clusters, capped at `MAX_MAP_PINS` — changed
   from biggest-first so the photos line up with the top of the text list.
   The image comes from the free
   keyless Esri World Imagery export endpoint (`server.arcgisonline.com`),
   covering ±`MAP_HALF_SPAN_DEG` around the fire, which sits at image center.
   The endpoint cannot draw a marker, so `draw_fire_marker()` draws a red
   circle + crosshair at the center using Pillow (the one dependency beyond
   `requests`; installed in the workflow). If Pillow is missing or drawing
   fails, the unmarked image is sent instead — never let the marker block the
   photo. The script downloads the JPEG and uploads it via Telegram
   `sendPhoto` (multipart), because letting Telegram fetch the URL itself is
   less reliable. Each photo carries an inline "📍 Open map" button (Google
   Maps URL) — tapping the photo itself only opens Telegram's photo viewer;
   a URL button is the closest Telegram allows to a clickable map photo. If the imagery fetch fails it falls back to a plain Telegram
   `sendLocation` pin; all failures are logged but never block the alert
   (text already sent).
11. `save_seen()` writes back the union, trimmed to the 5000 most recently
   *acquired* ids (`acquired_at`) to bound file growth. It previously trimmed on
   the raw string sort, i.e. kept the northernmost ids and discarded the oldest
   — which would have re-alerted southern fires forever once the file passed the
   cap. The window only has to outlast `DAY_RANGE` for dedup to work.

## `find_hotspots.py` (analysis helper, not part of the hourly run)

A standalone stdlib-only script, run either by hand or via the **Find Hotspots**
workflow. It clusters `seen_fires.json` by proximity and ranks locations by the
number of *distinct days* they appear on — the discriminator between a wildfire (1–2 days) and an
industrial source (most days). Prints paste-ready `excluded_zones.json` entries
with a radius derived from each location's observed scatter, plus a maps link.

Deliberately read-only: it never edits `excluded_zones.json`. A zone suppresses
real fires at that spot too, so a human must eyeball the imagery first. Don't
"improve" this by adding an auto-append flag without the user asking.

Note the feedback loop — once a zone is active those detections never reach
`seen`, so a suppressed spot slowly disappears from this report. That is
intended, but it means the report reflects what got through, not ground truth.

The script exits non-zero (`sys.exit(msg)`) when `seen_fires.json` is missing,
unparseable, or empty. In the workflow that shows as a failed run, deliberately —
an empty state file means the alert side has stopped recording. The summary step
carries `if: always()` so the reason is still published either way.

Workflow inputs are passed to the run step as environment variables and expanded
by bash, never interpolated into the script body with `${{ }}` — that form is a
command-injection hole on `workflow_dispatch` string inputs. Keep it that way if
you add an input. The summary is capped at 1000 lines (`--all` currently emits
~2800); the full text stays in the job log.

## State files

`seen_fires.json` is a JSON list of detection-id strings. `load_seen()` tolerates a
missing, empty, or corrupted file (returns empty set) — this was added after the
user emptied the file and hit a `JSONDecodeError`. Keep that resilience if you
refactor. Clearing this file causes the next `check` run to treat all current
fires as new (one-time re-alert).

`alert_cooldown.json` is a JSON list of `{"lat", "lon", "last_alert"}` with
`last_alert` an ISO `YYYY-MM-DDTHH:MM:SSZ` UTC stamp. Written only when an alert
actually goes out, committed by the workflow alongside `seen_fires.json`.
Deleting it makes the next `check` run re-announce every currently burning fire
once. It is bounded by the prune in `remember_alerts()`, not by a count cap.

`excluded_zones.json` is a hand-maintained JSON list of
`{"name", "lat", "lon", "radius_km"?}` objects. The script only ever reads it, so
the workflow does not need to commit it. `load_excluded_zones()` fails *open* on
purpose: a missing, unreadable, non-list, or malformed file yields zero zones, and
individual bad entries are skipped rather than aborting the load. Reasoning — a
config typo should cost the user some false-positive noise, never a suppressed
real fire. Do not "harden" this into raising. Note that suppressed detections are
dropped before entering `seen`, so deleting a zone re-alerts anything still in the
FIRMS 24h window once.

## Common modification requests and how to approach them

- **Change region:** update `FIRE_BBOX` (and/or `FIRE_COUNTRY`) in the workflow.
  For border-accurate filtering, replace `BG_POLYGON` with the new country's
  outline (list of `(lat, lon)`), or set `FILTER_TO_POLYGON = False` to use only
  the rectangle. Remember the bbox must actually cover the polygon.
- **Add a notification channel (e.g. ntfy.sh, Discord, email):** add a new sender
  function mirroring `send_telegram()` and call it wherever `send_telegram()` is
  called. Consider a small abstraction (list of senders) if adding several.
- **Change how often a burning fire repeats:** `COOLDOWN_HOURS` /
  `NEAR_COOLDOWN_HOURS`. Set both to 0 to go back to alerting on every pass.
- **Recurring false positive (industrial hotspot):** append an entry to
  `excluded_zones.json`; no code change. To find candidates, run the **Find
  Hotspots** workflow (or `find_hotspots.py` locally). If one keeps leaking through, widen that
  entry's `radius_km` rather than raising the global default — MODIS pixels are
  ~1 km and the reported centroid drifts between passes. A considered but
  unimplemented safety valve: a per-zone `max_frp` threshold letting genuinely
  large fires (FIRMS `frp` column) alert despite the zone.
- **Move the distance reference point:** edit `FIRE_CENTER_*` in the workflow's
  `env:` block; no code change. Only cosmetic — it changes ordering and wording,
  never which fires are reported (that is `BG_POLYGON` + `FIRE_BBOX`).
- **Sort by something else (size, recency):** change the key in `by_distance()`
  or sort after it. Note the user explicitly asked for nearest-first, so don't
  revert it to the old biggest-first ordering without being asked.
- **Sensitivity:** lower `MIN_CONFIDENCE` to catch more (and more false positives);
  raise it to reduce noise. Adjust `CLUSTER_DEG` to change how aggressively nearby
  detections merge, and `BUFFER_KM` for how far outside the border to include.
- **Faster detection:** changing the cron is largely cosmetic; the satellite
  overpass cadence (~4–6/day) is the real limit, not the check frequency.

## Constraints and gotchas

- Only stdlib + `requests` + `pillow` (for the map marker). Keep it that way
  unless there's a strong reason;
  extra dependencies mean editing the workflow's install step and add failure
  surface for a non-technical maintainer.
- No secrets in code, ever. They must stay in GitHub Actions secrets.
- JSON files are opened with `encoding="utf-8-sig"`. The user now edits them on
  Windows, where an editor may add a BOM; without this, `json.load` raises and
  every exclusion zone silently disappears. Keep it.
- The `.strip()` on the three secrets is deliberate — pasted secrets picked up
  trailing whitespace/newlines that caused 400s. Keep it.
- Times from FIRMS are UTC and `acq_time` is HHMM without a colon — but with
  **leading zeros stripped**, so 00:43 arrives as `43`. `stamp_of()` zero-pads it
  into a canonical `YYYY-MM-DD HHMM`; without that, `cluster_fires()` compared
  `"930" > "1149"` as strings and a cluster could report an earlier time as its
  latest. Never build a timestamp from `acq_time` without padding.
- `fmt_seen()` renders that stamp as local time with UTC in brackets
  (`14:22 EEST (11:22 UTC)`), zone from `FIRE_TIMEZONE`, default `Europe/Sofia`.
  The UTC value is always kept — it is what FIRMS actually reported and what the
  logs use. `zoneinfo` needs a tz database, which Linux runners have and bare
  Windows does not, so `load_local_zone()` returns None there and the format
  degrades to UTC-only rather than raising.
- This is an awareness tool, not an emergency system. Don't add framing that
  implies guaranteed or real-time fire detection.
