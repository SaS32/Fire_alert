"""Find recurring detection locations — candidate industrial false positives.

Reads the alert history in seen_fires.json, groups detections that sit close
together, and reports the ones that keep coming back day after day. A wildfire
burns for a few days and stops; a cement works, smelter, flare or landfill
shows up nearly every single day at the same spot. That difference is the whole
signal this script looks for.

Nothing is written. Suggested entries are printed for you to check on a map and
paste into excluded_zones.json yourself — a zone silences that spot for real
fires too, so a human should look before one is added.

    python find_hotspots.py                  # normal use
    python find_hotspots.py --min-days 3     # widen the net
    python find_hotspots.py --all            # list every location, once-off included

Note: once a zone is active its detections stop being recorded, so newly
suppressed spots fade out of this report over the following weeks.
"""

import argparse
import json
import math
import os
import sys

SEEN_FILE = "seen_fires.json"
ZONES_FILE = "excluded_zones.json"

DEFAULT_RECUR_KM = 0.5   # detections this close are treated as one source
DEFAULT_MIN_DAYS = 4     # distinct days before a location is worth reporting
RADIUS_MARGIN = 1.35     # suggested radius = observed spread * this
MIN_SUGGESTED_KM = 0.2   # never suggest a ring tighter than the script default


def km_between(lat1, lon1, lat2, lon2):
    kx = 111.32 * math.cos(math.radians((lat1 + lat2) / 2))
    ky = 110.57
    return math.hypot((lon2 - lon1) * kx, (lat2 - lat1) * ky)


def load_detections(path):
    """Parse seen_fires.json into (lat, lon, date) triples, skipping junk."""
    try:
        with open(path, encoding="utf-8-sig") as f:
            ids = json.load(f)
    except FileNotFoundError:
        sys.exit(f"No {path} here. Run this from the repository root.")
    except (json.JSONDecodeError, ValueError) as e:
        sys.exit(f"{path} is not readable JSON: {e}")

    points, bad = [], 0
    for did in ids:
        try:
            lat, lon, date, _time = str(did).split("_")
            points.append((float(lat), float(lon), date))
        except ValueError:
            bad += 1
    if bad:
        print(f"(skipped {bad} unparseable id(s) in {path})")
    return points


def load_zones(path):
    """Load existing exclusion zones so already-handled spots can be marked."""
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8-sig") as f:
            raw = json.load(f)
    except (json.JSONDecodeError, ValueError):
        print(f"Warning: {path} is not readable; treating every spot as unexcluded.\n")
        return []
    if not isinstance(raw, list):
        return []

    zones = []
    for entry in raw:
        try:
            zones.append({
                "name": str(entry.get("name") or "unnamed"),
                "lat": float(entry["lat"]),
                "lon": float(entry["lon"]),
                "radius_km": float(entry.get("radius_km", MIN_SUGGESTED_KM)),
            })
        except (AttributeError, KeyError, TypeError, ValueError):
            continue
    return zones


def covering_zone(lat, lon, zones):
    for z in zones:
        if km_between(lat, lon, z["lat"], z["lon"]) <= z["radius_km"]:
            return z
    return None


def cluster(points, recur_km):
    """Group nearby detections, tracking a running centroid per group."""
    groups = []
    for lat, lon, date in points:
        for g in groups:
            if km_between(lat, lon, g["lat"], g["lon"]) <= recur_km:
                g["points"].append((lat, lon))
                g["days"].add(date)
                n = len(g["points"])
                g["lat"] += (lat - g["lat"]) / n
                g["lon"] += (lon - g["lon"]) / n
                break
        else:
            groups.append({"lat": lat, "lon": lon,
                           "points": [(lat, lon)], "days": {date}})

    for g in groups:
        g["spread_km"] = max(
            (km_between(g["lat"], g["lon"], p[0], p[1]) for p in g["points"]),
            default=0.0,
        )
    groups.sort(key=lambda g: (len(g["days"]), len(g["points"])), reverse=True)
    return groups


def suggested_radius(spread_km):
    return max(MIN_SUGGESTED_KM, round(spread_km * RADIUS_MARGIN + 0.049, 1))


def describe(g, total_days, zone):
    days, hits = len(g["days"]), len(g["points"])
    mark = "already excluded" if zone else f"{days}/{total_days} days"
    print(f"\n  {g['lat']:.5f},{g['lon']:.5f}   {hits} detection(s), {mark}")
    print(f"    scatter {g['spread_km'] * 1000:.0f} m   "
          f"seen {', '.join(sorted(d[5:] for d in g['days'])[:10])}"
          f"{' ...' if days > 10 else ''}")
    print(f"    https://maps.google.com/?q={g['lat']:.5f},{g['lon']:.5f}")
    if zone:
        print(f"    covered by: {zone['name']}")
    else:
        radius = suggested_radius(g["spread_km"])
        print('    {"name": "CHECK THE MAP FIRST", '
              f'"lat": {g["lat"]:.5f}, "lon": {g["lon"]:.5f}, '
              f'"radius_km": {radius}}},')


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--min-days", type=int, default=DEFAULT_MIN_DAYS,
                    help=f"days a spot must recur to be reported (default {DEFAULT_MIN_DAYS})")
    ap.add_argument("--recur-km", type=float, default=DEFAULT_RECUR_KM,
                    help=f"how close counts as the same source (default {DEFAULT_RECUR_KM})")
    ap.add_argument("--all", action="store_true",
                    help="list every location, including one-day fires")
    ap.add_argument("--seen", default=SEEN_FILE, help=f"default {SEEN_FILE}")
    ap.add_argument("--zones", default=ZONES_FILE, help=f"default {ZONES_FILE}")
    args = ap.parse_args()

    points = load_detections(args.seen)
    if not points:
        sys.exit("No detections recorded yet — nothing to analyse.")
    zones = load_zones(args.zones)

    all_days = sorted({d for _, _, d in points})
    total_days = len(all_days)
    print(f"{len(points)} detections over {total_days} day(s): "
          f"{all_days[0]} to {all_days[-1]}")
    print(f"{len(zones)} exclusion zone(s) currently active")

    groups = cluster(points, args.recur_km)
    threshold = 1 if args.all else args.min_days

    reported = watch = 0
    suppressed_hits = 0
    for g in groups:
        zone = covering_zone(g["lat"], g["lon"], zones)
        if zone:
            suppressed_hits += len(g["points"])
        if len(g["days"]) >= threshold:
            if reported == 0:
                print(f"\n=== Recurring on {threshold}+ separate days "
                      "— likely industrial ===")
            describe(g, total_days, zone)
            reported += 1
        elif len(g["days"]) >= 2:
            watch += 1

    if not reported:
        print(f"\nNothing recurred on {threshold}+ days. No new zones suggested.")
    if watch and not args.all:
        print(f"\n{watch} location(s) seen on 2-{threshold - 1} days — too few to "
              "call. Re-run in a few weeks, or use --min-days 2 to see them.")

    once = sum(1 for g in groups if len(g["days"]) == 1)
    print(f"\n{once} location(s) seen on a single day — ordinary fire behaviour.")
    if suppressed_hits:
        pct = 100 * suppressed_hits / len(points)
        print(f"{suppressed_hits} of {len(points)} stored detections ({pct:.0f}%) "
              "fall inside existing zones.")
    print("\nCheck any suggestion on the map before adding it. A zone hides real "
          "fires there too.")


if __name__ == "__main__":
    main()
