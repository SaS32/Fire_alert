# Fire Alert — satellite wildfire notifications for Bulgaria

Automatically checks NASA satellite fire data every hour and sends a Telegram
message when a new fire is detected in (or near) Bulgaria. Runs entirely on free
GitHub Actions — no server, no computer left on, nothing to install on your phone
except Telegram.

Currently configured for **Bulgaria**, but it works for any country or region by
changing a couple of settings (see "Changing the monitored area" below).

---

## What it does

- Every hour, pulls near-real-time fire detections from **4 NASA satellites**
  (NOAA-20, NOAA-21, Terra, Aqua) covering Bulgaria.
- Keeps only detections inside Bulgaria's border, plus a 5 km safety buffer, so
  fires in Turkey / Romania / Greece are filtered out (border fires are kept).
- Groups detections within ~2 km into a single "fire" so one wildfire isn't
  reported as a dozen separate points.
- Remembers what it already told you, so you only get alerted about **new** fire
  activity — not the same fire every hour.
- With each alert it also sends a **satellite photo** of each fire (up to 5),
  with a red marker on the exact spot and a **📍 Open map** button underneath —
  so you can immediately see whether the fire is in forest, farmland, or a town.
- On request ("report" mode), sends a full snapshot of all fires in the last 24h.
- If NASA's servers are unreachable, it retries after 5 and 10 minutes, and only
  warns you (once) if everything is still down after ~15 minutes.

---

## Files in this package

| File | Purpose |
|------|---------|
| `fire_alerts_action.py` | The main program. Runs once per invocation. |
| `.github/workflows/fire-alerts.yml` | Tells GitHub to run the script hourly, and adds the manual "check/report" button. |
| `seen_fires.json` | Memory of already-reported detections. Starts empty (`[]`). GitHub updates it automatically. |
| `AI_CONTEXT.md` | Technical explanation for an AI assistant if you want help modifying the project later. |
| `README.md` | This file. |

---

## One-time setup

You need three free accounts/keys: a NASA key, a Telegram bot, and a GitHub repo.
Everything below can be done from a phone browser.

### 1. Get a NASA FIRMS map key

1. Go to <https://firms.modaps.eosdis.nasa.gov/api/map_key>
2. Enter your email and request a key.
3. NASA emails you a 32-character key (check spam). Keep it handy.

### 2. Create a Telegram bot

1. In Telegram, open **@BotFather**.
2. Send `/newbot`, pick a name and a username ending in `bot`.
3. BotFather gives you a **bot token** like `123456789:AAH...`. Copy it.

### 3. Get your Telegram chat ID

1. Open your new bot in Telegram and send it any message (e.g. "hi").
2. In a browser, open (replace `<TOKEN>`):
   `https://api.telegram.org/bot<TOKEN>/getUpdates`
3. Find `"chat":{"id":NUMBER` — that `NUMBER` is your **chat ID**.

### 4. Create the GitHub repository

1. Create a new repository on <https://github.com> (private is fine).
2. Add these files, keeping the same folder layout:
   - `fire_alerts_action.py` (repo root)
   - `seen_fires.json` (repo root)
   - `.github/workflows/fire-alerts.yml` (create this exact path — typing the
     `/` characters makes the folders)
   - `README.md` and `AI_CONTEXT.md` are optional but recommended.

   On mobile, use the browser (not the GitHub app): **Add file → Create new
   file**, type the path, paste the content, **Commit changes**.

### 5. Add your three secrets

In the repo: **Settings → Secrets and variables → Actions → New repository
secret**. Add these three (names must match exactly):

| Secret name | Value |
|-------------|-------|
| `FIRMS_MAP_KEY` | your NASA key |
| `TELEGRAM_BOT_TOKEN` | your bot token |
| `TELEGRAM_CHAT_ID` | your chat ID |

*(If the Settings tab is hidden on mobile, enable "Desktop site" in the browser
menu. Settings only appears on repos you own.)*

### 6. Test it

1. Repo → **Actions** tab → **Fire Alerts** → **Run workflow**.
2. Choose **report** from the dropdown and run it.
3. Within a minute you should get a Telegram message — either a list of active
   fires or "no active fires detected ... ✅".

That's it. From now on it runs by itself every hour.

---

## Daily use

- **You do nothing.** Hourly checks run automatically and message you only when
  there's new fire activity.
- **To get a full status on demand** (e.g. "is that fire still burning?"): Actions
  → Fire Alerts → Run workflow → choose **report**. This works from the GitHub
  mobile app too.
- **Reading an alert:** first a text message lists each fire — its approximate
  location, how many satellite detections it has (more = bigger/hotter), when it
  was last seen (UTC), and a Google Maps link. Then, for the biggest fires, a
  satellite photo follows with a red marker on the fire and a **📍 Open map**
  button. (Tapping the photo just zooms it; use the button to open the map.)
  The satellite imagery is archival — it shows what the terrain normally looks
  like, not the fire or smoke itself.

---

## Changing the monitored area

Open `.github/workflows/fire-alerts.yml` and edit the `env:` block:

- **Different country:** change `FIRE_COUNTRY: "BGR"` to another 3-letter code
  (e.g. `GRC` for Greece). Then either remove the `FIRE_BBOX` line (uses NASA's
  country endpoint, currently unreliable) or set a new bounding box.
- **Different rectangle:** set `FIRE_BBOX` to `"west,south,east,north"` in degrees.

The precise border filtering is separate — it lives in `fire_alerts_action.py` as
`BG_POLYGON`. If you switch countries and want border-accurate filtering, either
replace that polygon with the new country's outline or set `FILTER_TO_POLYGON =
False` to rely on the rectangle alone. See `AI_CONTEXT.md` for details.

---

## Tuning knobs (top of `fire_alerts_action.py`)

| Setting | Default | Meaning |
|---------|---------|---------|
| `CLUSTER_DEG` | `0.02` | How close (in degrees, ~2 km) detections merge into one fire. |
| `BUFFER_KM` | `5.0` | How far outside the border a fire is still reported. |
| `MAX_ITEMS` | `35` | Max fires listed per Telegram message. |
| `MAX_MAP_PINS` | `5` | Max satellite photos sent per alert (biggest fires first). |
| `MAP_HALF_SPAN_DEG` | `0.02` | Zoom of the satellite photo (~±2 km around the fire). Smaller = closer. |
| `RETRY_DELAYS` | `[300, 600]` | Seconds to wait between retry attempts on NASA outages. |
| `FILTER_TO_POLYGON` | `True` | Whether to apply the border-shape filter at all. |
| `MIN_CONFIDENCE` | `nominal` | Minimum detection confidence (via `FIRE_MIN_CONFIDENCE` env). |

---

## Good to know / limitations

- **Not for life-safety decisions.** Satellites pass over only ~4–6 times a day,
  and detections arrive 1–3 hours after the pass. A new fire may take hours to
  appear. Always defer to official emergency services.
- **Clouds and smoke** can hide fires from satellites; absence from a report is
  strong but not absolute proof a fire is out.
- **Scheduled runs use UTC** and can be delayed 5–15 minutes when GitHub is busy.
- **Inactive repos:** GitHub pauses scheduled workflows after 60 days without
  repo activity. The hourly state-file commits normally keep it active; if it ever
  pauses, re-enable it with one tap in the Actions tab.
- **Reignition:** if a fire goes out and restarts later, the new detections have
  new timestamps, so you'll be alerted again.

---

## Credits

Fire data © NASA FIRMS (Fire Information for Resource Management System),
near-real-time MODIS and VIIRS active fire products. Free for public use.
