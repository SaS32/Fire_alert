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

    for attemp
