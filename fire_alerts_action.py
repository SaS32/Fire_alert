"""NASA FIRMS Fire Alerts - multi-satellite, clustering, border buffer, retry + outage alert."""

import csv
import io
import json
import math
import os
import time

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

CLUSTER_DEG = 0.02        # detections closer than ~2 km count as one fire
MAX_ITEMS = 35
BUFFER_KM = 5.0           # keep fires up to this far outside the border outline
RETRY_DELAYS = [300, 600]  # wait 5 min, then 10 min, between fetch attempts

SEEN_FILE = "seen_fires.json"
CONF_ORDER = {"l": 0, "low": 0, "n": 1, "nominal": 1, "h": 2, "high": 2}

FILTER_TO_POLYGON = True
BG_POLYGON = [
    (44.22, 22.68), (44.00, 23.00), (43.85, 23.60), (43.75, 24.50),
    (43.72, 25.60), (43.95, 26.60), (44.12, 27.27), (43.75, 28.58),
    (43.35, 28.47), (42.60, 27.65), (42.10, 27.90), (41.98, 28.03),
    (41.72, 27.35), (41.71, 26.35), (41.32, 25.30), (41.24, 24.60),
    (41.34, 23.63), (41.40, 23.33), (41.34, 22.94), (41.80, 22.87),
    (42.32, 22.36), (42.85, 22.55), (43.20, 22.95), (43.65, 22.36),
    (44.05, 22.40),
]


def point_in_polygon(lat, lon, polygon):
    inside = False
    n = len(polygon)
    j = n - 1
    for i in range(n):
        lat_i, lon_i = polygon[i]
        lat_j, lon_j = polygon[j]
        if ((lon_i > lon) != (lon_j > lon)) and (
            lat < (lat_j - lat_i) * (lon - lon_i) / (lon_j - lon_i) + lat_i
        ):
            inside = not inside
        j = i
    return inside


def km_to_segment(lat, lon, p1, p2):
    kx = 111.32 * math.cos(math.radians(lat))
    ky = 110.57
    ax, ay = (p1[1] - lon) * kx, (p1[0] - lat) * ky
    bx, by = (p2[1] - lon) * kx, (p2[0] - lat) * ky
    dx, dy = bx - ax, by - ay
    seg_len_sq = dx * dx + dy * dy
    if seg_len_sq == 0:
        return math.hypot(ax, ay)
    t = max(0.0, min(1.0, -(ax * dx + ay * dy) / seg_len_sq))
    cx, cy = ax + t * dx, ay + t * dy
    return math.hypot(cx, cy)


def near_border(lat, lon, polygon, max_km):
    n = len(polygon)
    for i in range(n):
        if km_to_segment(lat, lon, polygon[i], polygon[(i + 1) % n]) <= max_km:
            return True
    return False


def in_area(row):
    if not FILTER_TO_POLYGON:
        return True
    try:
        lat, lon = float(row["latitude"]), float(row["longitude"])
    except (KeyError, TypeError, ValueError):
        return False
    if point_in_polygon(lat, lon, BG_POLYGON):
        return True
    return near_border(lat, lon, BG_POLYGON, BUFFER_KM)


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


def fetch_all_with_retries():
    """Fetch all sources; retry failed ones after 5 and 10 minutes."""
    all_rows = []
    pending = [s.strip() for s in SOURCES]
    last_errors = {}

    for attempt in range(len(RETRY_DELAYS) + 1):
        still_failing = []
        for source in pending:
            try:
                all_rows.extend(fetch_hotspots(source))
            except Exception as e:
                last_errors[source] = str(e)
                still_failing.append(source)
                print(f"Attempt {attempt + 1}: source failed: {source}: {e}")
        pending = still_failing
        if not pending:
            break
        if attempt < len(RETRY_DELAYS):
            delay = RETRY_DELAYS[attempt]
            print(f"Waiting {delay // 60} min before retrying: {', '.join(pending)}")
            time.sleep(delay)

    return all_rows, pending, last_errors


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
            })
    return clusters


def load_seen():
    if os.path.exists(SEEN_FILE):
        try:
            with open(SEEN_FILE) as f:
                return set(json.load(f))
        except (json.JSONDecodeError, ValueError):
            print("Warning: seen file was empty or corrupted, starting fresh.")
            return set()
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
        lines.append(
            f"• Fire near {c['lat']:.3f},{c['lon']:.3f} — "
            f"{c['count']} detection(s), last seen {c['last_seen']} UTC\n"
            f"  https://maps.google.com/?q={c['lat']:.5f},{c['lon']:.5f}"
        )
    if len(clusters) > MAX_ITEMS:
        lines.append(f"...and {len(clusters) - MAX_ITEMS} more fires.")
    return "\n".join(lines)


def main():
    all_rows, failed_sources, errors = fetch_all_with_retries()

    if failed_sources and not all_rows:
        # Everything is down even after retries: warn on Telegram, then fail the run
        try:
            send_telegram(
                "⚠️ Fire alert system: could not reach NASA FIRMS after "
                "3 attempts over 15 minutes. No fire data this hour. "
                "Will try again on the next scheduled run."
            )
        except Exception as te:
            print(f"Could not send Telegram warning either: {te}")
        raise RuntimeError("All satellite sources failed after retries: "
                           + "; ".join(f"{s}: {errors[s]}" for s in failed_sources))

    if failed_sources:
        print(f"Note: continuing without: {', '.join(failed_sources)}")

    good = {}
    skipped = 0
    for row in all_rows:
        if not confident_enough(row):
            continue
        if not in_area(row):
            skipped += 1
            continue
        good[detection_id(row)] = row
    if skipped:
        print(f"Filtered out {skipped} detection(s) outside Bulgaria (+{BUFFER_KM} km buffer).")

    seen = load_seen()
    new_hits = [r for did, r in good.items() if did not in seen]
    seen.update(good.keys())

    area = "Bulgaria" if FILTER_TO_POLYGON else (BBOX or COUNTRY)
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
