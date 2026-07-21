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

The user is non-technical and operates entirely from an Android phone (GitHub via
mobile browser, edits by paste). Optimize explanations and change instructions for
that context: prefer full-file replacements over diffs/patches, because applying
partial edits on mobile has repeatedly caused copy-paste corruption. Avoid
assuming command-line, git CLI, or desktop access.

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
  uses a 0–100 integer. `confident_enough()` handles both.
- `RUN_MODE` (`check` or `report`): set from the workflow's `inputs.mode`,
  defaulting to `check` for scheduled runs.

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
4. Dedup by `detection_id()` = lat_lon_date_time. Compare against `seen` loaded
   from `seen_fires.json`; the difference is "new".
5. `cluster_fires()` — greedy single-pass spatial clustering: a detection joins an
   existing cluster if within `CLUSTER_DEG` in both lat and lon, updating a running
   centroid; otherwise starts a new cluster. Not a true metric clusterer, so a fire
   front wider than ~2 * CLUSTER_DEG can split into adjacent clusters. Acceptable
   for alerting.
6. Output: `report` mode summarizes all current fires (clustered); `check` mode
   messages only new activity. Both use `fmt_clusters()`, capped at `MAX_ITEMS`.
   After the text message, `send_map_pins()` sends a satellite image for the
   largest clusters, capped at `MAX_MAP_PINS`. The image comes from the free
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
7. `save_seen()` writes back the union, trimmed to the last 5000 ids to bound file
   growth.

## State file

`seen_fires.json` is a JSON list of detection-id strings. `load_seen()` tolerates a
missing, empty, or corrupted file (returns empty set) — this was added after the
user emptied the file and hit a `JSONDecodeError`. Keep that resilience if you
refactor. Clearing this file causes the next `check` run to treat all current
fires as new (one-time re-alert).

## Common modification requests and how to approach them

- **Change region:** update `FIRE_BBOX` (and/or `FIRE_COUNTRY`) in the workflow.
  For border-accurate filtering, replace `BG_POLYGON` with the new country's
  outline (list of `(lat, lon)`), or set `FILTER_TO_POLYGON = False` to use only
  the rectangle. Remember the bbox must actually cover the polygon.
- **Add a notification channel (e.g. ntfy.sh, Discord, email):** add a new sender
  function mirroring `send_telegram()` and call it wherever `send_telegram()` is
  called. Consider a small abstraction (list of senders) if adding several.
- **Quiet/dedup long-running fires:** `check` mode re-alerts each hour a fire
  produces new-timestamped detections. To throttle, track last-alert time per
  cluster location in the state file and suppress within a cooldown window.
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
- The `.strip()` on the three secrets is deliberate — pasted secrets picked up
  trailing whitespace/newlines that caused 400s. Keep it.
- Times from FIRMS are UTC; `acq_time` is HHMM without a colon (e.g. `0139`).
  Messages label times UTC; don't silently reinterpret as local.
- This is an awareness tool, not an emergency system. Don't add framing that
  implies guaranteed or real-time fire detection.
