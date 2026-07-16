"""
NASA FIRMS Fire Alerts - GitHub Actions version.
Runs once per invocation; the schedule is handled by GitHub Actions.
"""

import csv
import io
import json
import os

import requests

FIRMS_MAP_KEY = os.environ["FIRMS_MAP_KEY"].strip()
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"].strip()
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"].strip()

COUNTRY = os.environ.get("FIRE_COUNTRY", "BGR")
BBOX = os.environ.get("FIRE_BBOX") or None
SOURCE = os.environ.get("FIRE_SOURCE", "VIIRS_NOAA20_NRT")
DAY_RANGE = 1
MIN_CONFIDENCE = os.environ.get("FIRE_MIN_CONFIDENCE", "nominal")

SEEN_FILE = "seen_fires.json"
CONF_ORDER = {"l": 0, "low": 0, "n": 1, "nominal": 1, "h": 2, "high": 2}


def fetch_hotspots():
    if BBOX:
        url = (f"https://firms.modaps.eosdis.nasa.gov/api/area/csv/"
               f"{FIRMS_MAP_KEY}/{SOURCE}/{BBOX}/{DAY_RANGE}")
    else:
        url = (f"https://firms.modaps.eosdis.nasa.gov/api/country/csv/"
               f"{FIRMS_MAP_KEY}/{SOURCE}/{COUNTRY}/{DAY_RANGE}")
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    text = resp.text.strip()
    if not text or text.lower().startswith("invalid"):
        raise RuntimeError(f"FIRMS API problem: {text[:200]}")
    return list(csv.DictReader(io.StringIO(text)))


def confident_enough(row):
    conf = str(row.get("confidence", "")).strip().lower()
    if conf.isdigit():
        try:
            return int(conf) >= int(MIN_CONFIDENCE)
        except (ValueError, TypeError):
            return int(conf) >= 80
    needed = CONF_ORDER.get(str(MIN_CONFIDENCE).lower(), 1)
    return CONF_ORDER.get(conf, 0) >= needed


def detection_id(row):
    return f"{row.get('latitude')}_{row.get('longitude')}_{row.get('acq_date')}_{row.get('acq_time')}"


def load_seen():
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE) as f:
            return set(json.load(f))
    return set()


def save_seen(seen):
    with open(SEEN_FILE, "w") as f:
        json.dump(sorted(seen)[-5000:], f)


def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    resp = requests.post(url, data={
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "disable_web_page_preview": True,
    }, timeout=30)
    resp.raise_for_status()


def main():
    rows = fetch_hotspots()
    seen = load_seen()
    new_hits = []
    for row in rows:
        if not confident_enough(row):
            continue
        did = detection_id(row)
        if did in seen:
            continue
        seen.add(did)
        new_hits.append(row)

    if new_hits:
        lines = [f"🔥 {len(new_hits)} new fire detection(s) in {BBOX or COUNTRY}:"]
        for r in new_hits[:10]:
            lat, lon = r.get("latitude"), r.get("longitude")
            lines.append(
                f"• {r.get('acq_date')} {r.get('acq_time')} UTC — "
                f"conf: {r.get('confidence')}\n"
                f"  https://maps.google.com/?q={lat},{lon}"
            )
        if len(new_hits) > 10:
            lines.append(f"...and {len(new_hits) - 10} more.")
        send_telegram("\n".join(lines))
        print(f"Alert sent: {len(new_hits)} new detections.")
    else:
        print("No new detections.")

    save_seen(seen)


if __name__ == "__main__":
    main()
