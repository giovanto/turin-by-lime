"""Cut the bike rides out of a Google Maps Timeline export (Android, on-device format).

    python3 scripts/track_from_timeline.py /path/to/Timeline.json --from 2026-10-16 --to 2026-10-17

Reads `semanticSegments` (activity type CYCLING, plus its `timelinePath`) and densifies each ride with any
`rawSignals` positions inside the ride's time window. Also accepts the older Takeout format
(`timelineObjects` / `activitySegment`). The export itself never enters the repo: only the 16-17 Oct rides do.

With --windows data/lime-receipts.csv the rides are cut by the Lime receipt times instead of Google's activity
guess (Google called the Friday ride "in passenger vehicle"). With --snap the fixes (one every ~35 s) are
joined along streets by BRouter in one request per ride, times are interpolated between fixes, and the
first/last --trim-ends metres (hotel) are dropped before anything is written.
"""
import argparse
import csv
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from common import RAW, http_get_json, write_ride, dist_m

ROME = ZoneInfo("Europe/Rome")
BROUTER = "https://brouter.de/brouter"

CYCLE_TYPES = {"CYCLING", "IN_BICYCLE", "BICYCLE"}
LATLNG = re.compile(r"(-?\d+(?:\.\d+)?)°?,\s*(-?\d+(?:\.\d+)?)°?")


def ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def parse_point(s):
    m = LATLNG.search(s)
    return (float(m.group(2)), float(m.group(1))) if m else None  # (lon, lat)


def from_semantic(data):
    rides, raw = [], []
    for seg in data.get("semanticSegments", []):
        act = seg.get("activity")
        if act and act.get("topCandidate", {}).get("type", "").upper() in CYCLE_TYPES:
            rides.append({"start": ts(seg["startTime"]), "end": ts(seg["endTime"]),
                          "prob": act["topCandidate"].get("probability"), "dist": act.get("distanceMeters"),
                          "pts": [], "a": parse_point(act["start"]["latLng"]), "b": parse_point(act["end"]["latLng"])})
    for seg in data.get("semanticSegments", []):
        for p in seg.get("timelinePath", []):
            pt = parse_point(p["point"])
            if pt:
                raw.append((ts(p["time"]), pt, None, "path", None))
    for s in data.get("rawSignals", []):
        pos = s.get("position")
        if pos:
            pt = parse_point(pos["LatLng"])
            if pt:
                raw.append((ts(pos["timestamp"]), pt, pos.get("accuracyMeters"), "raw", pos.get("speedMetersPerSecond")))
    return rides, raw


def from_takeout(data):
    rides, raw = [], []
    for o in data.get("timelineObjects", []):
        a = o.get("activitySegment")
        if not a or a.get("activityType", "").upper() not in CYCLE_TYPES:
            continue
        r = {"start": ts(a["duration"]["startTimestamp"]), "end": ts(a["duration"]["endTimestamp"]),
             "prob": None, "dist": a.get("distance"), "pts": [],
             "a": (a["startLocation"]["longitudeE7"] / 1e7, a["startLocation"]["latitudeE7"] / 1e7),
             "b": (a["endLocation"]["longitudeE7"] / 1e7, a["endLocation"]["latitudeE7"] / 1e7)}
        for p in a.get("simplifiedRawPath", {}).get("points", []) + a.get("waypointPath", {}).get("waypoints", []):
            if "timestamp" in p:
                raw.append((ts(p["timestamp"]), (p["lngE7"] / 1e7, p["latE7"] / 1e7), p.get("accuracyMeters"), "path", None))
        rides.append(r)
    return rides, raw


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("export")
    ap.add_argument("--from", dest="d0", default="2026-10-16")
    ap.add_argument("--to", dest="d1", default="2026-10-17")
    ap.add_argument("--max-acc", type=float, default=60, help="drop raw fixes less accurate than this (m)")
    ap.add_argument("--out", default=None, help="output dir (default data/rides)")
    ap.add_argument("--windows", default=None, help="CSV of Lime receipts: cut rides by their time windows")
    ap.add_argument("--snap", action="store_true", help="join the fixes along streets with BRouter")
    ap.add_argument("--profile", default="trekking")
    ap.add_argument("--trim-ends", type=float, default=150, help="privacy: drop the first/last N metres")
    args = ap.parse_args()

    data = json.loads(Path(args.export).read_text())
    rides, raw = from_semantic(data) if "semanticSegments" in data else from_takeout(data)
    if args.windows:
        return by_windows(args, rides, raw)
    t0 = datetime.fromisoformat(args.d0).replace(tzinfo=timezone.utc).timestamp() - 3 * 3600
    t1 = datetime.fromisoformat(args.d1).replace(tzinfo=timezone.utc).timestamp() + 27 * 3600
    rides = [r for r in rides if t0 <= r["start"] <= t1]
    print(f"{len(rides)} cycling segments between {args.d0} and {args.d1}; {len(raw)} timed points in the export")

    for i, r in enumerate(sorted(rides, key=lambda r: r["start"]), 1):
        pts = sorted({(t, p) for t, p, acc, *_ in raw
                      if r["start"] - 30 <= t <= r["end"] + 30 and (acc is None or acc <= args.max_acc)})
        if r["a"] and (not pts or pts[0][0] > r["start"]):
            pts.insert(0, (r["start"], r["a"]))
        if r["b"] and (not pts or pts[-1][0] < r["end"]):
            pts.append((r["end"], r["b"]))
        coords = [p for _, p in pts]
        times = [t for t, _ in pts]
        gaps = [dist_m(coords[k], coords[k + 1]) for k in range(len(coords) - 1)] or [0]
        rid = f"R{i:02d}"
        start = datetime.fromtimestamp(r["start"]).astimezone().strftime("%Y-%m-%d %H:%M")
        write_ride(rid, coords, times, "google-timeline", out_dir=args.out, start=start,
                   duration_min=round((r["end"] - r["start"]) / 60, 1), google_distance_m=r["dist"],
                   google_probability=r["prob"], n_points=len(coords), median_gap_m=round(sorted(gaps)[len(gaps) // 2]))
        print(f"  {rid} {start}  {len(coords):4d} pts  median gap {sorted(gaps)[len(gaps) // 2]:.0f} m  "
              f"google dist {r['dist']}")


# ───────────────────────── receipt windows + snapping ─────────────────────────

def by_windows(args, segs, raw):
    for w in csv.DictReader(open(args.windows)):
        start = datetime.strptime(f"{w['date']} {w['start']}", "%Y-%m-%d %H:%M").replace(tzinfo=ROME).timestamp()
        mm, ss = (int(x) for x in w["lime_duration"].split(":"))
        end = start + 60 * mm + ss
        fixes = sorted({(t, p, acc, spd) for t, p, acc, kind, spd in raw
                        if start - 60 <= t <= end + 90 and kind == "raw" and (acc is None or acc <= args.max_acc)})
        google = [g for g in segs if g["start"] < end and g["end"] > start]
        act = max(google, key=lambda g: min(end, g["end"]) - max(start, g["start"]), default=None)
        if len(fixes) < 2:
            print(f"  {w['ride_id']}: only {len(fixes)} fixes in the window, skipped")
            continue
        props = dict(start=datetime.fromtimestamp(start, ROME).strftime("%Y-%m-%d %H:%M"), lime_km=float(w["lime_km"]),
                     lime_duration=w["lime_duration"], n_fixes=len(fixes),
                     google_distance_m=act["dist"] if act else None)
        if args.snap:
            fixes = [f for i, f in enumerate(fixes) if i == 0 or int(f[0]) > int(fixes[i - 1][0])]
            moving = [f for i, f in enumerate(fixes) if i == 0 or dist_m(f[1], fixes[i - 1][1]) > 5]  # standing still breaks the matcher
            line = match(moving)
            if line:
                coords, times, kept, how = line, with_times(line, moving), fixes, "osrm-match/bike"
            else:
                (coords, times, kept), how = snap(fixes, args.profile), f"brouter/{args.profile}"
            coords, times, kept = trim(coords, times, kept, args.trim_ends)
            props.update(fixes=[[round(p[0], 6), round(p[1], 6), int(t), acc, spd] for t, p, acc, spd in kept],
                         router=how, trimmed_m=args.trim_ends)
            source = "timeline+router"
        else:
            coords, times, source = [f[1] for f in fixes], [f[0] for f in fixes], "google-timeline"
        L = sum(dist_m(coords[k], coords[k + 1]) for k in range(len(coords) - 1))
        write_ride(w["ride_id"], coords, times, source, out_dir=args.out, **props)
        print(f"  {w['ride_id']} {props['start']}: {len(fixes)} fixes → {len(coords)} pts, {L / 1000:.2f} km after trim "
              f"(Lime {w['lime_km']} km, Google {props['google_distance_m']} m)")


def route(points, profile):
    lonlats = "|".join(f"{lon:.6f},{lat:.6f}" for lon, lat in points)
    key = hashlib.sha1(f"{lonlats}|{profile}".encode()).hexdigest()[:12]
    data = http_get_json(f"{BROUTER}?lonlats={lonlats}&profile={profile}&alternativeidx=0&format=geojson",
                         RAW / "brouter" / f"{key}.json")
    return [tuple(c[:2]) for c in data["features"][0]["geometry"]["coordinates"]]


OSRM_MATCH = "https://routing.openstreetmap.de/routed-bike/match/v1/driving"  # FOSSGIS, bike profile; max 10 points


def match(fixes, chunk=10):
    """Map-match the fixes (OSRM, bike profile) in chunks of 10 that share their end fix; None if it fails."""
    import time
    import urllib.parse
    line = []
    i = 0
    while i < len(fixes) - 1:
        part = fixes[i:i + chunk]
        coords = ";".join(f"{p[0]:.6f},{p[1]:.6f}" for _, p, _, _ in part)
        q = urllib.parse.urlencode({"timestamps": ";".join(str(int(t)) for t, *_ in part),
                                    "radiuses": ";".join(str(max(8, min(25, int(1.2 * (acc or 15))))) for _, _, acc, _ in part),
                                    "geometries": "geojson", "overview": "full", "gaps": "ignore", "tidy": "true"})
        key = hashlib.sha1(f"{coords}|{q}".encode()).hexdigest()[:12]
        cache = RAW / "osrm" / f"{key}.json"
        try:
            r = http_get_json(f"{OSRM_MATCH}/{coords}?{q}", cache, tries=2, pause=2)
        except RuntimeError as e:
            print(f"   map matching failed ({e}); falling back to BRouter")
            return None
        if not cache.exists() or r.get("code") != "Ok":
            return None
        seg = [tuple(c) for m in r["matchings"] for c in m["geometry"]["coordinates"]]
        line += seg[1:] if line else seg
        i += chunk - 1
        time.sleep(1)  # be gentle with the FOSSGIS server
    return line


def with_times(line, kept):
    """Times along a line through the fixes, interpolated by distance between each fix's nearest vertex."""
    cum = [0.0]
    for k in range(1, len(line)):
        cum.append(cum[-1] + dist_m(line[k - 1], line[k]))
    idx, j = [], 0
    for f in kept:
        best = min(range(j, len(line)), key=lambda v: dist_m(line[v], f[1]) + (0 if v - j < 400 else 1e9))
        idx.append(best)
        j = best
    times = []
    for v in range(len(line)):
        k = max(0, min(len(idx) - 2, next((i for i in range(len(idx) - 1) if idx[i + 1] >= v), len(idx) - 2)))
        a, b = idx[k], idx[k + 1]
        f = (cum[v] - cum[a]) / (cum[b] - cum[a]) if cum[b] > cum[a] else 0
        times.append(kept[k][0] + max(0.0, min(1.0, f)) * (kept[k + 1][0] - kept[k][0]))
    for v in range(1, len(times)):
        times[v] = max(times[v], times[v - 1])
    return times


def snap(fixes, profile, max_rounds=4):
    """Route through the fixes; drop a fix when the leg into it is a detour (> 2x straight + 60 m)."""
    kept = [f for i, f in enumerate(fixes) if i == 0 or dist_m(f[1], fixes[i - 1][1]) > 12 or i == len(fixes) - 1]
    for _ in range(max_rounds):
        line = route([f[1] for f in kept], profile)
        cum = [0.0]
        for k in range(1, len(line)):
            cum.append(cum[-1] + dist_m(line[k - 1], line[k]))
        idx, j = [], 0
        for f in kept:  # monotonic nearest route vertex for each fix
            best = min(range(j, len(line)), key=lambda v: dist_m(line[v], f[1]) + (0 if v - j < 400 else 1e9))
            idx.append(best)
            j = best
        bad = [i for i in range(1, len(kept))
               if cum[idx[i]] - cum[idx[i - 1]] > 2 * dist_m(kept[i - 1][1], kept[i][1]) + 60 and 0 < i < len(kept) - 1]
        if not bad:
            break
        kept = [f for i, f in enumerate(kept) if i not in set(bad[:1])]
    # times along the line, interpolated by distance between matched fixes
    times = []
    for v in range(len(line)):
        k = max(0, min(len(idx) - 2, next((i for i in range(len(idx) - 1) if idx[i + 1] >= v), len(idx) - 2)))
        a, b = idx[k], idx[k + 1]
        f = (cum[v] - cum[a]) / (cum[b] - cum[a]) if cum[b] > cum[a] else 0
        times.append(kept[k][0] + max(0.0, min(1.0, f)) * (kept[k + 1][0] - kept[k][0]))
    for v in range(1, len(times)):  # keep time monotonic
        times[v] = max(times[v], times[v - 1])
    return line, times, [(f[0], f[1], f[2], f[3]) for f in kept]


def trim(coords, times, fixes, metres):
    if metres <= 0:
        return coords, times, fixes
    cum = [0.0]
    for k in range(1, len(coords)):
        cum.append(cum[-1] + dist_m(coords[k - 1], coords[k]))
    keep = [k for k in range(len(coords)) if metres <= cum[k] <= cum[-1] - metres]
    a, b = coords[keep[0]], coords[keep[-1]]
    head, tail = coords[0], coords[-1]
    fx = [f for f in fixes if dist_m(f[1], head) > metres and dist_m(f[1], tail) > metres]
    return [coords[k] for k in keep], [times[k] for k in keep], fx


if __name__ == "__main__":
    main()
