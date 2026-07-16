"""NASA FIRMS Fire Alerts - multi-satellite, with fire clustering."""

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
SOURCES = os.environ.get(
    "FIRE_SOURCES", "VIIRS_NOAA20_NRT,VIIRS_NOAA21_NRT,MODIS_NRT"
).split(",")
DAY_RANGE = 1
MIN_CONFIDENCE = os.environ.get("FIRE_MIN_CONFIDENCE", "nominal")
REPORT_MODE = os.environ.get("RUN_MODE", "check") == "report"

# Detections closer than ~2 km are treated as one fire
CLUSTER_DEG = 0.02
MAX_ITEMS = 35

SEEN_FILE = "seen_fires.json"
CONF_ORDER = {"l": 0, "low": 0, "n": 1, "nominal": 1, "h": 2, "high": 2}


def fetch_hotspots(source):
    if BBOX:
        url = (f"https://firms.modaps.eosdis.nasa.gov/api/area/csv/"
               f"{FIRMS_MAP_KEY}/{source}/{BBOX}/{DAY_RANGE}")
    else:
        url = (f"https://firms.modaps.eosdis.nasa.gov/api/country/csv/"
               f"{FIRMS_MAP_KEY}/{source}/{COUNTRY}/{DAY_RANGE}")
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    text = resp.text.strip()
    if not text or text.lower().startswith("invalid"):
        raise RuntimeError(f"FIRMS API problem for {source}: {text[:200]}")
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


def cluster_fires(rows):
    """Group detections within ~2 km into single fires."""
    clusters = []
    for r in rows:
        try:
            lat, lon = float(r["latitude"]), float(r["longitude"])
        except (KeyError, TypeError, ValueError):
            continue
        placed = False
        for c in clusters:
            if abs(lat - c["lat"]) < CLUSTER_DEG and abs(lon - c["lon"]) < CLUSTER_DEG:
                n = c["count"]
                c["lat"] = (c["lat"] * n + lat) / (n + 1)
                c["lon"] = (c["lon"] * n + lon) / (n + 1)
                c["count"] = n + 1
                key = f"{r.get('acq_date')} {r.get('acq_time')}"
                if key > c["last_seen"]:
                    c["last_seen"] = key
                placed = True
                break
        if not placed:
            clusters.append({
                "lat": lat, "lon": lon, "count": 1,
                "last_seen": f"{r.get('acq_date')} {r.get('acq_time')}",
                "conf": r.get("confidence", "?"),
            })
    return clusters


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


def fmt_clusters(clusters, title):
    clusters = sorted(clusters, key=lambda c: c["count"], reverse=True)
    lines = [title]
    for c in clusters[:MAX_ITEMS]:
        size = f"{c['count']} detection(s)"
        lines.append(
            f"• Fire near {c['lat']:.3f},{c['lon']:.3f} — {size}, "
            f"last seen {c['last_seen']} UTC\n"
            f"  https://maps.google.com/?q={c['lat']:.5f},{c['lon']:.5f}"
        )
    if len(clusters) > MAX_ITEMS:
        lines.append(f"...and {len(clusters) - MAX_ITEMS} more fires.")
    return "\n".join(lines)


def main():
    all_rows, errors = [], []
    for source in SOURCES:
        source = source.strip()
        try:
            all_rows.extend(fetch_hotspots(source))
        except Exception as e:
            errors.append(f"{source}: {e}")
            print(f"Warning, source failed: {source}: {e}")

    if errors and not all_rows and len(errors) == len(SOURCES):
        raise RuntimeError("All satellite sources failed: " + "; ".join(errors))

    good = {}
    for row in all_rows:
        if confident_enough(row):
            good[detection_id(row)] = row

    seen = load_seen()
    new_hits = [r for did, r in good.items() if did not in seen]
    seen.update(good.keys())

    area = BBOX or COUNTRY
    if REPORT_MODE:
        clusters = cluster_fires(list(good.values()))
        if clusters:
            msg = fmt_clusters(
                clusters,
                f"📋 Report: {len(clusters)} active fire(s) "
                f"({len(good)} detections) in {area}, last 24h:")
        else:
            msg = f"📋 Report: no active fires detected in {area} in the last 24h. ✅"
        send_telegram(msg)
        print("Report sent.")
    elif new_hits:
        clusters = cluster_fires(new_hits)
        send_telegram(fmt_clusters(
            clusters,
            f"🔥 {len(clusters)} fire(s) with new activity "
            f"({len(new_hits)} new detections) in {area}:"))
        print(f"Alert sent: {len(clusters)} fires, {len(new_hits)} detections.")
    else:
        print("No new detections.")

    save_seen(seen)


if __name__ == "__main__":
    main()
