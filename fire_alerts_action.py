"""NASA FIRMS Fire Alerts - multi-satellite, clustering, border buffer,
distance-from-centre ranking, retry + outage alert."""

import csv
import io
import json
import math
import os
import time
from datetime import datetime, timezone

import requests

from geo import (compass_from, env_float, fmt_distance, great_circle_km,
                 km_between, load_center)

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

# Reference point every fire is measured from. Fires are listed nearest first
# and each line says how far away it is. Change it in the workflow's env: block
# (FIRE_CENTER_NAME / FIRE_CENTER_LAT / FIRE_CENTER_LON) - no code edit needed.
CENTER = load_center()
CENTER_NAME, CENTER_LAT, CENTER_LON = CENTER

# Alerts are read in Bulgaria, so they show local time with UTC in brackets.
# FIRMS always reports UTC; the UTC value is never dropped, only accompanied.
LOCAL_TZ_NAME = os.environ.get("FIRE_TIMEZONE", "").strip() or "Europe/Sofia"

# A fire this close is the reason the whole system exists, so it is called out
# separately rather than being one line among thirty.
NEAR_KM = 20.0

CLUSTER_DEG = 0.02        # detections closer than ~2 km count as one fire
MAX_ITEMS = 35
MAX_MAP_PINS = 10          # send at most this many map pictures per message
MAP_HALF_SPAN_DEG = 0.02  # satellite image covers ~±2 km around the fire
BUFFER_KM = 5.0           # keep fires up to this far outside the border outline
RETRY_DELAYS = [300, 600]  # wait 5 min, then 10 min, between fetch attempts

SEEN_FILE = "seen_fires.json"
ZONES_FILE = "excluded_zones.json"
EXCLUDE_RADIUS_KM = 0.2   # default ring around a known false-positive source (~200 m)
CONF_ORDER = {"l": 0, "low": 0, "n": 1, "nominal": 1, "h": 2, "high": 2}
# The two satellite families report confidence on different scales: VIIRS uses
# the words above, MODIS a 0-100 number. NASA's own MODIS bands are low 0-29,
# nominal 30-79, high 80-100, so this is what a word threshold means to MODIS.
CONF_PERCENT = {0: 0, 1: 30, 2: 80}

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


def fmt_size(cluster):
    """', 45 MW' when the feed reported power, otherwise nothing."""
    power = fmt_power(cluster.get("frp"))
    return f", {power}" if power else ""


def is_near(cluster):
    """Close enough to the centre point to deserve alarm rather than a bullet."""
    return cluster["dist_km"] <= NEAR_KM


def marker(cluster):
    """🚨 for a fire near the centre point, • for the rest."""
    return "🚨" if is_near(cluster) else "•"


def describe_place(cluster):
    """e.g. '34 km NE of Sofia'."""
    return (f"{fmt_distance(cluster['dist_km'])} "
            f"{cluster['direction']} of {CENTER_NAME}")


def by_distance(clusters):
    """Measure every fire from the centre point and order them nearest first."""
    for c in clusters:
        c["dist_km"] = great_circle_km(CENTER_LAT, CENTER_LON, c["lat"], c["lon"])
        c["direction"] = compass_from(CENTER_LAT, CENTER_LON, c["lat"], c["lon"])
    return sorted(clusters, key=lambda c: c["dist_km"])


def load_local_zone():
    """The display timezone, or None when the platform has no tz database.

    Windows without the `tzdata` package raises here. An alert must still go
    out, so this degrades to UTC-only rather than failing.
    """
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(LOCAL_TZ_NAME)
    except Exception as e:
        print(f"Warning: timezone {LOCAL_TZ_NAME!r} unavailable ({e}); showing UTC only.")
        return None


LOCAL_ZONE = load_local_zone()


def stamp_of(row):
    """Canonical 'YYYY-MM-DD HHMM' for a detection.

    acq_time is zero-padded here because FIRMS strips leading zeros: 00:43
    arrives as "43", and comparing that as a string would rank it above "1149".
    """
    return f"{row.get('acq_date')} {str(row.get('acq_time') or '').zfill(4)}"


def fmt_seen(stamp):
    """'2026-09-06 14:22 EEST (11:22 UTC)' from a canonical stamp.

    Falls back to showing the raw value if it cannot be parsed - a cosmetic
    helper must never be able to lose the alert.
    """
    try:
        utc = datetime.strptime(stamp, "%Y-%m-%d %H%M").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return f"{stamp} UTC"
    if LOCAL_ZONE is None:
        return f"{utc:%Y-%m-%d %H:%M} UTC"
    local = utc.astimezone(LOCAL_ZONE)
    return f"{local:%Y-%m-%d %H:%M} {local:%Z} ({utc:%H:%M} UTC)"


def row_frp(row):
    """Fire radiative power in MW, or None if the feed did not report it.

    FIRMS gives this per detection; it is a far better measure of how big a fire
    is than counting detections, which mostly reflects how many satellites
    happened to look. Missing or unparseable values are None, never 0, so a
    silent feed is never mistaken for a cold fire.
    """
    try:
        value = float(str(row.get("frp", "")).strip())
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def fmt_power(mw):
    """Format a fire radiative power total, or '' when nothing was reported."""
    if not mw:
        return ""
    return f"{mw:.1f} MW" if mw < 10 else f"{mw:.0f} MW"


def load_excluded_zones():
    """Load known false-positive spots (hot factories, flares, landfills).

    Never raises. A missing, unreadable or malformed file means "no zones", so
    a typo here can only cost noise — it can never silently suppress a real
    fire. Bad individual entries are skipped one by one for the same reason.
    """
    if not os.path.exists(ZONES_FILE):
        return []
    try:
        # utf-8-sig: a Windows text editor may save this file with a BOM, and a
        # BOM would otherwise make every zone silently vanish.
        with open(ZONES_FILE, encoding="utf-8-sig") as f:
            raw = json.load(f)
    except (json.JSONDecodeError, ValueError, OSError) as e:
        print(f"Warning: could not read {ZONES_FILE} ({e}); excluding nothing.")
        return []
    if not isinstance(raw, list):
        print(f"Warning: {ZONES_FILE} is not a list; excluding nothing.")
        return []

    zones = []
    for i, entry in enumerate(raw, start=1):
        try:
            max_frp = entry.get("max_frp")
            zones.append({
                "name": str(entry.get("name") or f"zone {i}"),
                "lat": float(entry["lat"]),
                "lon": float(entry["lon"]),
                "radius_km": float(entry.get("radius_km", EXCLUDE_RADIUS_KM)),
                # Optional safety valve: a detection hotter than this is let
                # through anyway, so a real wildfire at a factory still reaches
                # you. Absent means the zone always suppresses, as before.
                "max_frp": None if max_frp is None else float(max_frp),
            })
        except (AttributeError, KeyError, TypeError, ValueError):
            print(f"Warning: skipping malformed entry #{i} in {ZONES_FILE}.")
    return zones


def zone_containing(row, zones):
    """Return the excluded zone this detection falls inside, else None.

    Whether that zone actually suppresses it is decided by the caller, because a
    zone carrying a max_frp lets genuinely large fires through.
    """
    if not zones:
        return None
    try:
        lat, lon = float(row["latitude"]), float(row["longitude"])
    except (KeyError, TypeError, ValueError):
        return None
    for z in zones:
        if km_between(lat, lon, z["lat"], z["lon"]) <= z["radius_km"]:
            return z
    return None


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


def min_confidence_percent():
    """MIN_CONFIDENCE on the MODIS 0-100 scale, whether it is set as a word or a number."""
    raw = str(MIN_CONFIDENCE).strip().lower()
    if raw.isdigit():
        return int(raw)
    return CONF_PERCENT[CONF_ORDER.get(raw, 1)]


def min_confidence_class():
    """MIN_CONFIDENCE on the VIIRS low/nominal/high scale, as a rank 0-2."""
    raw = str(MIN_CONFIDENCE).strip().lower()
    if raw.isdigit():
        pct = int(raw)
        return 2 if pct >= CONF_PERCENT[2] else (1 if pct >= CONF_PERCENT[1] else 0)
    return CONF_ORDER.get(raw, 1)


def confident_enough(row):
    """Compare a detection against MIN_CONFIDENCE on whichever scale it reports.

    Previously a word threshold fell through to a hardcoded 80 for MODIS, so
    MIN_CONFIDENCE="nominal" quietly held MODIS to "high" while VIIRS ran at
    nominal - one of the four satellites was far stricter than configured.
    """
    conf = str(row.get("confidence", "")).strip().lower()
    if conf.isdigit():
        return int(conf) >= min_confidence_percent()
    return CONF_ORDER.get(conf, 0) >= min_confidence_class()


def detection_id(row):
    return f"{row.get('latitude')}_{row.get('longitude')}_{row.get('acq_date')}_{row.get('acq_time')}"


def acquired_at(detection_id_str):
    """Sort key ordering detection ids oldest first, by when they were acquired.

    detection_id() puts the coordinates first, so sorting the raw strings orders
    them by latitude. Trimming on that in save_seen() would discard the
    southernmost detections rather than the oldest, and every fire in southern
    Bulgaria would eventually be re-reported as new. Pull the date and time out
    instead. FIRMS drops leading zeros from acq_time (00:43 arrives as "43"), so
    it has to be zero-padded before it will compare correctly.
    """
    parts = detection_id_str.split("_")
    if len(parts) != 4:
        return ("", "")          # unrecognised id: treat as oldest, trim it first
    return (parts[2], parts[3].zfill(4))


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
                key = stamp_of(r)
                if key > c["last_seen"]:
                    c["last_seen"] = key
                frp = row_frp(r)
                if frp is not None:
                    c["frp"] = (c["frp"] or 0.0) + frp
                placed = True
                break
        if not placed:
            clusters.append({
                "lat": lat, "lon": lon, "count": 1,
                "last_seen": stamp_of(r),
                # Summed over the cluster: total power this fire is radiating.
                "frp": row_frp(r),
            })
    return clusters


def load_seen():
    if os.path.exists(SEEN_FILE):
        try:
            with open(SEEN_FILE, encoding="utf-8-sig") as f:
                return set(json.load(f))
        except (json.JSONDecodeError, ValueError):
            print("Warning: seen file was empty or corrupted, starting fresh.")
            return set()
    return set()


def save_seen(seen):
    # Keep the most recent ids, not the northernmost ones - see acquired_at().
    # The window only has to outlast the FIRMS query range (DAY_RANGE) for dedup
    # to work; 5000 is several days of normal activity.
    with open(SEEN_FILE, "w") as f:
        json.dump(sorted(seen, key=acquired_at)[-5000:], f)


def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    resp = requests.post(url, data={
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "disable_web_page_preview": True,
    }, timeout=30)
    resp.raise_for_status()


def send_location(lat, lon):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendLocation"
    resp = requests.post(url, data={
        "chat_id": TELEGRAM_CHAT_ID,
        "latitude": lat,
        "longitude": lon,
    }, timeout=30)
    resp.raise_for_status()


def draw_fire_marker(jpeg_bytes):
    """Draw a red marker (circle + crosshair) at the center of the image.

    Returns the original bytes unchanged if Pillow is unavailable or drawing
    fails, so the alert still goes out with an unmarked image.
    """
    try:
        from PIL import Image, ImageDraw

        img = Image.open(io.BytesIO(jpeg_bytes)).convert("RGB")
        draw = ImageDraw.Draw(img)
        cx, cy = img.width // 2, img.height // 2
        r = 20
        red = (255, 40, 40)
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], outline=red, width=5)
        for dx, dy in ((0, -1), (0, 1), (-1, 0), (1, 0)):
            draw.line(
                [cx + dx * (r + 5), cy + dy * (r + 5),
                 cx + dx * (r + 22), cy + dy * (r + 22)],
                fill=red, width=5,
            )
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=85)
        return out.getvalue()
    except Exception as e:
        print(f"Could not draw marker on image: {e}")
        return jpeg_bytes


def send_satellite_photo(lat, lon, caption):
    """Fetch a satellite image centered on the fire and post it to Telegram."""
    d = MAP_HALF_SPAN_DEG
    img_url = (
        "https://server.arcgisonline.com/ArcGIS/rest/services/"
        "World_Imagery/MapServer/export"
        f"?bbox={lon - d},{lat - d},{lon + d},{lat + d}"
        "&bboxSR=4326&imageSR=3857&size=600,600&format=jpg&f=image"
    )
    img = requests.get(img_url, timeout=60)
    img.raise_for_status()
    if not img.headers.get("Content-Type", "").startswith("image"):
        raise RuntimeError(f"Imagery server did not return an image: {img.text[:200]}")
    photo = draw_fire_marker(img.content)
    keyboard = json.dumps({
        "inline_keyboard": [[{
            "text": "📍 Open map",
            "url": f"https://maps.google.com/?q={lat:.5f},{lon:.5f}",
        }]]
    })
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
    resp = requests.post(
        url,
        data={"chat_id": TELEGRAM_CHAT_ID, "caption": caption,
              "reply_markup": keyboard},
        files={"photo": ("map.jpg", photo)},
        timeout=60,
    )
    resp.raise_for_status()


def send_map_pins(clusters):
    """Send a satellite map picture for the nearest fires (pin as fallback).

    Takes the list already ordered by by_distance(), so the photos match the top
    of the text message instead of following a separate biggest-first ranking.
    """
    for c in clusters[:MAX_MAP_PINS]:
        caption = (
            f"🔥 Fire at image center — {describe_place(c)}\n"
            f"{c['lat']:.5f},{c['lon']:.5f} ({c['count']} detection(s){fmt_size(c)})"
        )
        try:
            send_satellite_photo(c["lat"], c["lon"], caption)
        except Exception as e:
            print(f"Satellite image failed for {c['lat']:.3f},{c['lon']:.3f}: {e}")
            try:
                send_location(c["lat"], c["lon"])
            except Exception as e2:
                print(f"Fallback map pin also failed: {e2}")


def fmt_title(clusters, summary, calm_icon):
    """Build the first line, which is all a phone notification preview shows.

    When something is burning close by, that goes first - ahead of the counts,
    which are what matters on an ordinary day but not on this one.
    """
    nearest = clusters[0]
    if is_near(nearest):
        return f"🚨 FIRE {describe_place(nearest).upper()} — {summary}:"
    return f"{calm_icon} {summary} — nearest {describe_place(nearest)}:"


def fmt_clusters(clusters, title):
    """Format an already-distance-sorted cluster list, nearest fire first."""
    lines = [title]
    for c in clusters[:MAX_ITEMS]:
        lines.append(
            f"{marker(c)} Fire {describe_place(c)} "
            f"({c['lat']:.3f},{c['lon']:.3f}) — "
            f"{c['count']} detection(s){fmt_size(c)}, last seen {fmt_seen(c['last_seen'])}\n"
            f"  https://maps.google.com/?q={c['lat']:.5f},{c['lon']:.5f}"
        )
    if len(clusters) > MAX_ITEMS:
        lines.append(f"...and {len(clusters) - MAX_ITEMS} more fires, further away.")
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

    zones = load_excluded_zones()
    good = {}
    skipped = 0
    suppressed = {}
    overridden = {}
    for row in all_rows:
        if not confident_enough(row):
            continue
        if not in_area(row):
            skipped += 1
            continue
        # Drop per detection, not per cluster: a real fire within CLUSTER_DEG of
        # a factory would otherwise be swallowed by the same cluster and lost.
        zone = zone_containing(row, zones)
        if zone:
            frp = row_frp(row)
            if zone["max_frp"] is not None and frp is not None and frp >= zone["max_frp"]:
                # Hotter than this zone tolerates: a real fire at the factory,
                # not the factory. Let it through.
                overridden[zone["name"]] = max(overridden.get(zone["name"], 0.0), frp)
            else:
                suppressed[zone["name"]] = suppressed.get(zone["name"], 0) + 1
                continue
        good[detection_id(row)] = row
    if skipped:
        print(f"Filtered out {skipped} detection(s) outside Bulgaria (+{BUFFER_KM} km buffer).")
    if suppressed:
        detail = ", ".join(f"{name} ({n})" for name, n in sorted(suppressed.items()))
        print(f"Suppressed {sum(suppressed.values())} detection(s) in excluded zones: {detail}")
    if overridden:
        detail = ", ".join(f"{name} (up to {fmt_power(mw)})"
                           for name, mw in sorted(overridden.items()))
        print(f"Reported despite an excluded zone, over its max_frp: {detail}")

    seen = load_seen()
    new_hits = [r for did, r in good.items() if did not in seen]
    seen.update(good.keys())

    area = "Bulgaria" if FILTER_TO_POLYGON else (BBOX or COUNTRY)
    print(f"Distances measured from {CENTER_NAME} ({CENTER_LAT}, {CENTER_LON}).")
    if REPORT_MODE:
        clusters = by_distance(cluster_fires(list(good.values())))
        if clusters:
            msg = fmt_clusters(clusters, fmt_title(
                clusters,
                f"Report: {len(clusters)} active fire(s) "
                f"({len(good)} detections) in {area}, last 24h",
                "📋"))
        else:
            msg = f"📋 Report: no active fires detected in {area} in the last 24h. ✅"
        send_telegram(msg)
        if clusters:
            send_map_pins(clusters)
        print("Report sent.")
    elif new_hits:
        clusters = by_distance(cluster_fires(new_hits))
        send_telegram(fmt_clusters(clusters, fmt_title(
            clusters,
            f"{len(clusters)} fire(s) with new activity "
            f"({len(new_hits)} new detections) in {area}",
            "🔥")))
        send_map_pins(clusters)
        print(f"Alert sent: {len(clusters)} fires, {len(new_hits)} detections, "
              f"nearest {describe_place(clusters[0])}.")
    else:
        print("No new detections.")

    save_seen(seen)


if __name__ == "__main__":
    main()
