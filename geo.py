"""Shared geography helpers: distance, direction, the reference point, and zones.

Imported by both fire_alerts_action.py (to rank fires by how close they are) and
find_hotspots.py (to place recurring hotspots), so that "34 km NE of Sofia"
means the same thing in an alert and in a hotspot report, and an exclusion zone
is parsed exactly once instead of twice with subtly different rules.

Stdlib only, and it reads no secrets, so find_hotspots.py can import it without
a NASA key in the environment.
"""

import json
import math
import os

DEFAULT_CENTER_NAME = "Sofia"
DEFAULT_CENTER_LAT = 42.6977
DEFAULT_CENTER_LON = 23.3219

COMPASS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
           "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]


def env_float(name, default, low, high):
    """Read a numeric setting from the environment, falling back to the default.

    Never raises: a missing, empty, non-numeric or out-of-range value is reported
    and ignored. A typo in the workflow must not stop fire alerts going out.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        print(f"Warning: {name}={raw!r} is not a number; using {default}.")
        return default
    if not low <= value <= high:
        print(f"Warning: {name}={value} is outside {low}..{high}; using {default}.")
        return default
    return value


def load_center():
    """The (name, lat, lon) every fire is measured from. Sofia unless overridden."""
    name = os.environ.get("FIRE_CENTER_NAME", "").strip() or DEFAULT_CENTER_NAME
    lat = env_float("FIRE_CENTER_LAT", DEFAULT_CENTER_LAT, -90.0, 90.0)
    lon = env_float("FIRE_CENTER_LON", DEFAULT_CENTER_LON, -180.0, 180.0)
    return name, lat, lon


def km_between(lat1, lon1, lat2, lon2):
    """Flat-earth distance in km. Cheap, and accurate enough over a few km."""
    kx = 111.32 * math.cos(math.radians((lat1 + lat2) / 2))
    ky = 110.57
    return math.hypot((lon2 - lon1) * kx, (lat2 - lat1) * ky)


def great_circle_km(lat1, lon1, lat2, lon2):
    """Haversine distance in km.

    Separate from km_between(): the flat approximation is fine over the few
    hundred metres of an excluded zone, but a fire can be 400 km from the centre
    point and it drifts at that range.
    """
    earth_r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * earth_r * math.asin(math.sqrt(a))


def compass_from(center_lat, center_lon, lat, lon):
    """Direction of a point as seen from the centre, to the nearest 22.5 degrees."""
    p1, p2 = math.radians(center_lat), math.radians(lat)
    dl = math.radians(lon - center_lon)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    bearing = (math.degrees(math.atan2(y, x)) + 360.0) % 360.0
    return COMPASS[round(bearing / 22.5) % 16]


def fmt_distance(km):
    """Round sensibly: metres matter when it is close, not when it is 200 km away."""
    if km < 1:
        return f"{km * 1000:.0f} m"
    if km < 10:
        return f"{km:.1f} km"
    return f"{km:.0f} km"


def describe_offset(center, lat, lon):
    """e.g. '34 km NE of Sofia', given a (name, lat, lon) centre."""
    name, clat, clon = center
    return (f"{fmt_distance(great_circle_km(clat, clon, lat, lon))} "
            f"{compass_from(clat, clon, lat, lon)} of {name}")


def load_zones(path, default_radius_km=0.2):
    """Load exclusion zones shared by the action and the hotspot finder.

    Fails open, like every other state file in this project: a missing,
    unreadable or malformed file means "no zones", so a typo can only cost
    noise — it can never silently suppress a real fire. Bad individual entries
    are skipped one by one for the same reason.

    A zone can carry an optional max_frp: a detection hotter than that value is
    let through anyway (a real wildfire at a factory, not the factory). If the
    value is unreadable the zone degrades to a plain silent zone with a warning
    rather than being dropped — dropping it would leak its factory's daily
    detections back into the alerts.
    """
    if not os.path.exists(path):
        return []
    try:
        # utf-8-sig: a Windows text editor may save this file with a BOM, and a
        # BOM would otherwise make every zone silently vanish.
        with open(path, encoding="utf-8-sig") as f:
            raw = json.load(f)
    except (json.JSONDecodeError, ValueError, OSError) as e:
        print(f"Warning: could not read {path} ({e}); excluding nothing.")
        return []
    if not isinstance(raw, list):
        print(f"Warning: {path} is not a list; excluding nothing.")
        return []

    zones = []
    for i, entry in enumerate(raw, start=1):
        try:
            name = str(entry.get("name") or f"zone {i}")
            lat = float(entry["lat"])
            lon = float(entry["lon"])
        except (AttributeError, KeyError, TypeError, ValueError):
            print(f"Warning: skipping malformed entry #{i} in {path}.")
            continue
        try:
            radius_km = float(entry.get("radius_km", default_radius_km))
        except (TypeError, ValueError):
            radius_km = default_radius_km
        max_frp = None
        raw_frp = entry.get("max_frp")
        if raw_frp is not None:
            try:
                max_frp = float(raw_frp)
                if max_frp < 0:
                    max_frp = None
            except (TypeError, ValueError):
                print(f"Warning: entry #{i} ({name}) in {path} has an unreadable "
                      "max_frp; the zone stays absolutely silent.")
        zones.append({"name": name, "lat": lat, "lon": lon,
                      "radius_km": radius_km, "max_frp": max_frp})
    return zones


def covering_zone(lat, lon, zones):
    """Return the first exclusion zone containing the point, else None."""
    for z in zones:
        if km_between(lat, lon, z["lat"], z["lon"]) <= z["radius_km"]:
            return z
    return None