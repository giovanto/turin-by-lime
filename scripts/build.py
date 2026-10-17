"""Turin by Lime: rides → OSM context → spots table, GeoPackage, per-spot READMEs and web data.

    python3 scripts/build.py [--refresh] [--only-source google-timeline,reconstructed]

Steps
  1. clean each ride (drop GPS jumps > 40 km/h) and find stops (stayed within 15 m for >= 20 s)
  2. fetch OSM for the rides' bbox + 250 m (Overpass, cached in data/raw/osm/)
  3. classify every ride segment by the nearest OSM way within 12 m (cycleway / painted lane / mixed traffic ...)
  4. find where the ride crosses tram tracks and at what angle
  5. spots = Gio's spots (data/spots.csv) + candidates (stops, sharp tram crossings); summarise OSM within 30 m
  6. write out/turin-by-lime.gpkg (styled), spots/*.md, data/derived/*.csv, web/data/story.js
"""
import argparse
import csv
import json
import math
import re
import sqlite3
import subprocess
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from common import (DERIVED, GPKG, OGR, OGR_ENV, OUT, RAW, ROOT, SPOTS_CSV, WEB_DATA, bearing, crossing_angle, dist_m,
                    load_rides, lonlat, overpass, seg_intersect, seg_point_dist, xy)

SPOT_RADIUS = 30      # m, context around each spot
MATCH_RADIUS = 12     # m, ride segment → nearest OSM way
STOP_RADIUS, STOP_MIN_S = 15, 20
MAX_KMH = 40
TRAM_SAME_CROSSING = 15   # m, merge the two rails/tracks of one crossing
SHARP, OBLIQUE = 30, 60   # degrees

# ───────────────────────── 1. clean + stops ─────────────────────────

def clean(ride):
    """Drop same-time duplicates and GPS spikes, then smooth recorded rides lightly."""
    c, t = ride["coords"], ride["times"]
    pts = []
    for p, tt in zip(c, t):
        if pts and tt <= pts[-1][1]:
            continue
        pts.append((p, tt))
    kmh = lambda a, b: dist_m(a[0], b[0]) / max(1, b[1] - a[1]) * 3.6
    changed = True
    while changed and len(pts) > 2:  # a spike is too fast both coming in and going out
        changed, keep = False, [pts[0]]
        for k in range(1, len(pts) - 1):
            if kmh(keep[-1], pts[k]) > MAX_KMH and kmh(pts[k], pts[k + 1]) > MAX_KMH:
                changed = True
                continue
            keep.append(pts[k])
        pts = keep + [pts[-1]]
    ride["dropped"] = len(c) - len(pts)
    coords = [p for p, _ in pts]
    if ride["source"] == "google-timeline":
        coords = smooth(coords)
    ride["coords"], ride["times"] = coords, [tt for _, tt in pts]
    return ride


def smooth(coords, half=1):
    """Centred moving average over 2*half+1 fixes (3 by default: enough for zigzag, keeps corners); ends stay put."""
    P = [xy(*p) for p in coords]
    out = list(coords[:half])
    for k in range(half, len(P) - half):
        win = P[k - half:k + half + 1]
        out.append(lonlat(sum(x for x, _ in win) / len(win), sum(y for _, y in win) / len(win)))
    return out + list(coords[len(P) - half:]) if len(P) > 2 * half else list(coords)


def trim_ends(ride, metres):
    """Privacy: cut the first and last `metres` of a real ride, so hotel and home don't show."""
    if metres <= 0 or ride["source"] == "demo" or ride["props"].get("trimmed_m"):
        return ride
    c, t = ride["coords"], ride["times"]
    cum = [0.0]
    for k in range(1, len(c)):
        cum.append(cum[-1] + dist_m(c[k - 1], c[k]))
    if cum[-1] <= 3 * metres:
        return ride
    keep = [k for k in range(len(c)) if metres <= cum[k] <= cum[-1] - metres]
    ride["coords"], ride["times"] = [c[k] for k in keep], [t[k] for k in keep]
    ride["trimmed_m"] = metres
    return ride


def stops(ride):
    if ride["source"] in ("demo", "reconstructed"):
        return []  # synthetic times: no real stops
    if ride["props"].get("fixes"):  # matched line: stops come from the phone's own fixes
        fx = ride["props"]["fixes"]
        c, t = [(f[0], f[1]) for f in fx], [f[2] for f in fx]
    else:
        c, t = ride["coords"], ride["times"]
    out, i = [], 0
    while i < len(c):
        j = i
        while j + 1 < len(c) and dist_m(c[i], c[j + 1]) <= STOP_RADIUS:
            j += 1
        dur = t[j] - t[i]
        if dur >= STOP_MIN_S:
            lon = sum(p[0] for p in c[i:j + 1]) / (j - i + 1)
            lat = sum(p[1] for p in c[i:j + 1]) / (j - i + 1)
            edge = "start" if i == 0 else "end" if j == len(c) - 1 else ""
            out.append({"ride_id": ride["id"], "lon": lon, "lat": lat, "t_start": t[i], "dur_s": round(dur), "edge": edge})
            i = j + 1
        else:
            i += 1
    return out

def load_mpd_stops(rides):
    """data/derived/stops.csv (MovingPandas TrajectoryStopDetector) → same shape as stops()."""
    f = DERIVED / "stops.csv"
    if not f.exists() or f.stat().st_size == 0:
        return {}
    from zoneinfo import ZoneInfo
    rome = ZoneInfo("Europe/Rome")
    out = defaultdict(list)
    for r in rides:
        out[r["id"]] = []  # a ride with fixes but no stops has an empty list, not a fallback
    for row in csv.DictReader(open(f)):
        t0 = datetime.strptime(row["start"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=rome).timestamp()
        ride = next((r for r in rides if r["id"] == row["ride_id"]), None)
        fx = (ride or {}).get("props", {}).get("fixes") or []
        edge = ""
        if fx and t0 <= fx[0][2] + 1:
            edge = "start"
        elif fx and t0 + int(row["dur_s"]) >= fx[-1][2] - 1:
            edge = "end"
        out[row["ride_id"]].append({"ride_id": row["ride_id"], "lon": float(row["lon"]), "lat": float(row["lat"]),
                                    "t_start": t0, "dur_s": int(row["dur_s"]), "edge": edge, "method": "movingpandas"})
    print(f"stops: MovingPandas ({sum(len(v) for v in out.values())})")
    return {k: v for k, v in out.items() if any(r["id"] == k and r["props"].get("fixes") for r in rides)}

# ───────────────────────── 2. OSM ─────────────────────────

def bbox(rides, pad_m=250):
    xs, ys = zip(*[xy(*p) for r in rides for p in r["coords"]])
    w, s = lonlat(min(xs) - pad_m, min(ys) - pad_m)
    e, n = lonlat(max(xs) + pad_m, max(ys) + pad_m)
    return round(s, 4), round(w, 4), round(n, 4), round(e, 4)


ROAD_HW = {"trunk", "trunk_link", "primary", "primary_link", "secondary", "secondary_link", "tertiary", "tertiary_link",
           "unclassified", "residential", "living_street", "pedestrian", "cycleway", "service", "busway"}
PATH_HW = {"footway", "path", "track", "bridleway"}
POINT_HW = {"traffic_signals", "crossing", "stop", "give_way"}
POINT_RW = {"tram_crossing", "tram_level_crossing", "level_crossing", "crossing", "tram_stop"}


def local_osm(bb):
    """The Geofabrik/osmium extract (scripts/osm_extract.py), same filters as the Overpass query below."""
    lf, pf = RAW / "osm" / "local_lines.json", RAW / "osm" / "local_points.json"
    if not (lf.exists() and pf.exists()):
        return None
    S, W, N, E = bb
    inside = lambda lat, lon: S <= lat <= N and W <= lon <= E
    L, P = json.loads(lf.read_text()), json.loads(pf.read_text())
    lines = [e for e in L["elements"]
             if (e["tags"].get("highway") in ROAD_HW
                 or (e["tags"].get("highway") in PATH_HW and e["tags"].get("bicycle") in ("yes", "designated", "permissive"))
                 or e["tags"].get("railway") in ("tram", "light_rail"))
             and any(inside(g["lat"], g["lon"]) for g in e["geometry"])]
    points = [e for e in P["elements"] if (e["tags"].get("highway") in POINT_HW or e["tags"].get("railway") in POINT_RW)
              and inside(e["lat"], e["lon"])]
    print(f"OSM source: {L.get('source', 'local extract')}")
    return lines, points


def fetch_osm(bb, refresh, source="auto"):
    if source in ("auto", "local"):
        got = local_osm(bb)
        if got:
            return got
        if source == "local":
            raise SystemExit("no local extract: run scripts/osm_extract.py")
    S, W, N, E = bb
    key = f"{S}_{W}_{N}_{E}".replace(".", "p")
    print("OSM source: Overpass API")
    lines = overpass(f"""[out:json][timeout:180];
(
 way["highway"~"^(trunk|trunk_link|primary|primary_link|secondary|secondary_link|tertiary|tertiary_link|unclassified|residential|living_street|pedestrian|cycleway|service|busway)$"]({S},{W},{N},{E});
 way["highway"~"^(footway|path|track|bridleway)$"]["bicycle"~"^(yes|designated|permissive)$"]({S},{W},{N},{E});
 way["railway"~"^(tram|light_rail)$"]({S},{W},{N},{E});
);
out tags geom;""", RAW / "osm" / f"lines_{key}.json", refresh)
    points = overpass(f"""[out:json][timeout:120];
(
 node["highway"~"^(traffic_signals|crossing|stop|give_way)$"]({S},{W},{N},{E});
 node["railway"~"^(tram_crossing|tram_level_crossing|level_crossing|crossing|tram_stop)$"]({S},{W},{N},{E});
);
out;""", RAW / "osm" / f"points_{key}.json", refresh)
    return lines["elements"], points["elements"]


def maxspeed(tags):
    v = tags.get("maxspeed", "")
    if v in ("IT:urban", "urban"):
        return 50
    m = re.match(r"(\d+)", v)
    return int(m.group(1)) if m else None


def bike_kind(tags):
    """What the way offers a cyclist, from OSM tags only."""
    hw = tags.get("highway", "")
    if tags.get("railway") in ("tram", "light_rail"):
        return "tram"
    if hw == "cycleway" or (hw in ("footway", "path", "pedestrian") and tags.get("bicycle") == "designated"):
        return "cycleway"
    cw = " ".join(tags.get(k, "") for k in ("cycleway", "cycleway:both", "cycleway:left", "cycleway:right"))
    if "track" in cw:
        return "cycle track"
    if "lane" in cw and "shared_lane" not in cw:
        return "painted lane"
    if "separate" in cw:
        return "parallel cycleway"
    if "shared_lane" in cw or "share_busway" in cw or tags.get("bicycle_road") == "yes" or tags.get("cyclestreet") == "yes":
        return "shared lane"
    if hw in ("pedestrian", "footway"):
        return "pedestrian street"
    if hw in ("path", "track", "bridleway"):
        return "path / track"
    if hw in ("living_street", "service"):
        return "quiet street"
    return "mixed traffic"


class Ways:
    """OSM ways in the local metric frame with a 50 m grid index over their segments."""
    CELL = 50.0

    def __init__(self, elements):
        self.ways, self.grid = [], defaultdict(list)
        for el in elements:
            if el["type"] != "way" or "geometry" not in el:
                continue
            g = [(p["lon"], p["lat"]) for p in el["geometry"] if p]
            if len(g) < 2:
                continue
            tags = el.get("tags", {})
            w = {"id": el["id"], "tags": tags, "ll": g, "xy": [xy(*p) for p in g], "kind": bike_kind(tags)}
            k = len(self.ways)
            self.ways.append(w)
            for i in range(len(g) - 1):
                (ax, ay), (bx, by) = w["xy"][i], w["xy"][i + 1]
                for cx in range(int(min(ax, bx) // self.CELL), int(max(ax, bx) // self.CELL) + 1):
                    for cy in range(int(min(ay, by) // self.CELL), int(max(ay, by) // self.CELL) + 1):
                        self.grid[(cx, cy)].append((k, i))

    def near(self, p, r):
        """[(dist, way, seg_index)] for ways within r metres of p (x, y), nearest segment per way."""
        best = {}
        for cx in range(int((p[0] - r) // self.CELL), int((p[0] + r) // self.CELL) + 1):
            for cy in range(int((p[1] - r) // self.CELL), int((p[1] + r) // self.CELL) + 1):
                for k, i in self.grid.get((cx, cy), ()):
                    w = self.ways[k]
                    d, _ = seg_point_dist(p, w["xy"][i], w["xy"][i + 1])
                    if d <= r and (k not in best or d < best[k][0]):
                        best[k] = (d, i)
        return sorted(((d, self.ways[k], i) for k, (d, i) in best.items()), key=lambda x: x[0])

    def candidates_for_segment(self, a, b):
        ks = set()
        for cx in range(int(min(a[0], b[0]) // self.CELL) - 1, int(max(a[0], b[0]) // self.CELL) + 2):
            for cy in range(int(min(a[1], b[1]) // self.CELL) - 1, int(max(a[1], b[1]) // self.CELL) + 2):
                ks.update(self.grid.get((cx, cy), ()))
        return ks

# ───────────────────────── 3. segments ─────────────────────────

KIND_ORDER = ["cycleway", "cycle track", "painted lane", "parallel cycleway", "shared lane", "quiet street",
              "pedestrian street", "path / track", "mixed traffic", "unmatched"]


def classify_segments(ride, ways):
    segs = []
    pts = [xy(*p) for p in ride["coords"]]
    for k in range(len(pts) - 1):
        a, b = pts[k], pts[k + 1]
        L = math.hypot(b[0] - a[0], b[1] - a[1])
        if L < 0.5:
            continue
        mid = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
        hb = bearing(a, b)
        near = [(d, w, i) for d, w, i in ways.near(mid, MATCH_RADIUS) if w["kind"] != "tram"]
        # prefer ways running the same direction as the ride; then dedicated infra; then distance
        def score(item):
            d, w, i = item
            ang = crossing_angle(hb, bearing(w["xy"][i], w["xy"][i + 1]))
            infra = w["kind"] in ("cycleway", "cycle track")
            return d + ang * 0.3 - (4 if infra else 0)
        pick = min(near, key=score) if near else None
        kind = pick[1]["kind"] if pick else "unmatched"
        tags = pick[1]["tags"] if pick else {}
        tram_along = any(w["kind"] == "tram" and d <= 8 and
                         crossing_angle(hb, bearing(w["xy"][i], w["xy"][i + 1])) < 20
                         for d, w, i in ways.near(mid, 8))
        segs.append({"ride_id": ride["id"], "k": k, "a": ride["coords"][k], "b": ride["coords"][k + 1], "len_m": L,
                     "kind": kind, "street": tags.get("name", ""), "highway": tags.get("highway", ""),
                     "maxspeed": maxspeed(tags), "lanes": tags.get("lanes", ""), "surface": tags.get("surface", ""),
                     "tram_along": tram_along, "t": ride["times"][k], "bicycle": tags.get("bicycle", "")})
    return segs

# ───────────────────────── 4. tram crossings ─────────────────────────

CHORD = 20  # m before and after a crossing: the ride must change side of the track over this stretch


def tram_crossings(ride, ways):
    pts = [xy(*p) for p in ride["coords"]]
    cum = [0.0]
    for k in range(1, len(pts)):
        cum.append(cum[-1] + math.hypot(pts[k][0] - pts[k - 1][0], pts[k][1] - pts[k - 1][1]))

    def at(s):  # position at distance s along the ride (clamped)
        s = max(0.0, min(cum[-1], s))
        for k in range(1, len(cum)):
            if cum[k] >= s:
                f = (s - cum[k - 1]) / (cum[k] - cum[k - 1]) if cum[k] > cum[k - 1] else 0
                return (pts[k - 1][0] + f * (pts[k][0] - pts[k - 1][0]), pts[k - 1][1] + f * (pts[k][1] - pts[k - 1][1]))
        return pts[-1]

    hits = []
    for k in range(len(pts) - 1):
        a, b = pts[k], pts[k + 1]
        for wk, i in ways.candidates_for_segment(a, b):
            w = ways.ways[wk]
            if w["kind"] != "tram":
                continue
            r = seg_intersect(a, b, w["xy"][i], w["xy"][i + 1])
            if not r:
                continue
            (x, y), t = r
            # a real crossing puts the ride on the other side of the track 20 m later; GPS zigzag along a
            # tram street doesn't. The angle is taken on that ±20 m chord, not on one noisy segment.
            s = cum[k] + t * (cum[k + 1] - cum[k])
            before, after = at(s - CHORD), at(s + CHORD)
            A, B = w["xy"][i], w["xy"][i + 1]
            AB = math.hypot(B[0] - A[0], B[1] - A[1]) or 1
            side = lambda P: ((B[0] - A[0]) * (P[1] - A[1]) - (B[1] - A[1]) * (P[0] - A[0])) / AB
            sb, sa = side(before), side(after)
            if sb * sa >= 0 or min(abs(sb), abs(sa)) < 1.5:
                continue
            ang = crossing_angle(bearing(before, after), bearing(A, B))
            tt = ride["times"][k] + t * (ride["times"][k + 1] - ride["times"][k])
            hits.append({"x": x, "y": y, "angle": ang, "t": tt, "way": w["id"], "k": k})
    hits.sort(key=lambda h: h["t"])
    merged = []
    for h in hits:  # one crossing = the rails of both directions within ~15 m along the ride
        if merged and math.hypot(h["x"] - merged[-1]["x"], h["y"] - merged[-1]["y"]) <= TRAM_SAME_CROSSING:
            m = merged[-1]
            m["n_tracks"] += 1
            m["angle"] = min(m["angle"], h["angle"])
            continue
        merged.append({**h, "n_tracks": 1})
    out = []
    for m in merged:
        lon, lat = lonlat(m["x"], m["y"])
        cls = "sharp (<30°)" if m["angle"] < SHARP else "oblique (30–60°)" if m["angle"] < OBLIQUE else "square-ish (≥60°)"
        out.append({"ride_id": ride["id"], "lon": lon, "lat": lat, "angle": round(m["angle"]), "class": cls,
                    "n_tracks": m["n_tracks"], "t": m["t"], "k": m["k"]})
    return out

# ───────────────────────── 5. spots ─────────────────────────

def load_gio_spots():
    if not SPOTS_CSV.exists():
        return []
    rows = [r for r in csv.DictReader(open(SPOTS_CSV)) if r.get("lat") and r.get("lon")]
    for r in rows:
        r["lat"], r["lon"], r["origin"] = float(r["lat"]), float(r["lon"]), "gio"
    return rows


def spot_context(spot, ways, points, rides):
    p = xy(spot["lon"], spot["lat"])
    near = ways.near(p, SPOT_RADIUS)
    roads = [(d, w) for d, w, _ in near if w["kind"] != "tram"]
    trams = [(d, w) for d, w, _ in near if w["kind"] == "tram"]
    names = []
    for d, w in roads:
        n = w["tags"].get("name")
        if n and n not in names:
            names.append(n)
    kinds = Counter(w["kind"] for _, w in roads)
    speeds = [maxspeed(w["tags"]) for _, w in roads if maxspeed(w["tags"])]
    lanes = [int(m.group(1)) for _, w in roads for m in [re.match(r"(\d+)", w["tags"].get("lanes", ""))] if m]
    surfaces = sorted({w["tags"]["surface"] for _, w in roads if w["tags"].get("surface")})
    pts = [(dist_m((n["lon"], n["lat"]), (spot["lon"], spot["lat"])), n.get("tags", {})) for n in points]
    pts = [(d, t) for d, t in pts if d <= SPOT_RADIUS]
    signals = sum(1 for _, t in pts if t.get("highway") == "traffic_signals")
    crossings = Counter(t.get("crossing", "unspecified") for _, t in pts if t.get("highway") == "crossing")
    bike_x = sum(1 for _, t in pts if t.get("highway") == "crossing" and t.get("bicycle") in ("yes", "designated"))
    tram_stop = any(t.get("railway") == "tram_stop" for _, t in pts)
    has_infra = any(k in kinds for k in ("cycleway", "cycle track", "painted lane"))
    ctx = {
        "streets": " / ".join(names[:3]),
        "tram_tracks_30m": len(trams),
        "tram_min_dist_m": round(min(d for d, _ in trams)) if trams else None,
        "cycle_infra_30m": ", ".join(k for k in KIND_ORDER if k in kinds and k in ("cycleway", "cycle track", "painted lane", "parallel cycleway", "shared lane")) or "none",
        "has_cycle_infra": has_infra,
        "maxspeed_max": max(speeds) if speeds else None,
        "lanes_max": max(lanes) if lanes else None,
        "surfaces": ", ".join(surfaces),
        "signals_30m": signals,
        "crossings_30m": sum(crossings.values()),
        "crossing_types": ", ".join(f"{k} ×{v}" for k, v in crossings.most_common()),
        "bike_crossings_30m": bike_x,
        "tram_stop_30m": tram_stop,
        "road_classes": ", ".join(sorted({w["tags"].get("highway", "") for _, w in roads if w["tags"].get("highway")})),
    }
    # nearest point on a ride → story time
    best = (float("inf"), None, None)
    for r in rides:
        for k, q in enumerate(r["coords"]):
            d = dist_m(q, (spot["lon"], spot["lat"]))
            if d < best[0]:
                best = (d, r["id"], r["times"][k])
    ctx["ride_id"], ctx["ride_dist_m"], ctx["t"] = best[1], round(best[0]), best[2]
    return ctx


def osm_says(c, tram_x=None):
    """Plain-language facts from OSM for a spot (no judgement)."""
    bits = []
    if c["tram_tracks_30m"]:
        s = f"{c['tram_tracks_30m']} tram track way{'s' if c['tram_tracks_30m'] > 1 else ''} within 30 m"
        if tram_x:
            s += f", crossed at about {tram_x['angle']}°"
        bits.append(s)
    bits.append("cycle infra within 30 m: " + c["cycle_infra_30m"])
    if c["maxspeed_max"]:
        bits.append(f"speed limit up to {c['maxspeed_max']} km/h")
    if c["lanes_max"]:
        bits.append(f"up to {c['lanes_max']} lanes (OSM `lanes`)")
    if c["surfaces"]:
        bits.append("surface: " + c["surfaces"])
    if c["signals_30m"] or c["crossings_30m"]:
        bits.append(f"{c['signals_30m']} traffic signal node(s), {c['crossings_30m']} crossing node(s)"
                    + (f" ({c['crossing_types']})" if c["crossing_types"] else ""))
    return bits

# ───────────────────────── 6. outputs ─────────────────────────

def fc(features):
    return {"type": "FeatureCollection", "features": features}


def pt(lon, lat, props):
    return {"type": "Feature", "geometry": {"type": "Point", "coordinates": [lon, lat]}, "properties": props}


def ln(coords, props):
    return {"type": "Feature", "geometry": {"type": "LineString", "coordinates": [list(c) for c in coords]}, "properties": props}


def write_layer(features, layer):
    if not features:
        return 0
    tmp = OUT / f"_{layer}.geojson"
    tmp.write_text(json.dumps(fc(features), ensure_ascii=False))
    cmd = [OGR, "-f", "GPKG", "-nln", layer, "-t_srs", "EPSG:4326", "-lco", "GEOMETRY_NAME=geom", "-lco", "FID=fid"]
    if GPKG.exists():
        cmd.insert(3, "-update")
    p = subprocess.run(cmd + [str(GPKG), str(tmp)], capture_output=True, text=True, env=OGR_ENV)
    tmp.unlink(missing_ok=True)
    if p.returncode != 0:
        raise RuntimeError(f"ogr2ogr {layer}: {p.stderr.strip()[:400]}")
    return len(features)


def pedestrian_by_tag(segs):
    """Metres ridden on pedestrian streets, by OSM bicycle=* (yes / designated / dismount / no / untagged)."""
    out = Counter()
    for s in segs:
        if s["kind"] == "pedestrian street":
            out[s["bicycle"] or "untagged"] += s["len_m"]
    return {k: round(v) for k, v in out.most_common()}


def merge_runs(segs):
    """Join consecutive segments of the same kind into longer lines (for the map and the GPKG)."""
    runs = []
    for s in segs:
        if (runs and runs[-1]["ride_id"] == s["ride_id"] and runs[-1]["kind"] == s["kind"] and runs[-1]["last_k"] == s["k"] - 1
                and runs[-1]["bicycle"] == s["bicycle"]):
            r = runs[-1]
            r["coords"].append(s["b"])
            r["len_m"] += s["len_m"]
            r["last_k"] = s["k"]
            r["tram_along"] = r["tram_along"] or s["tram_along"]
            if s["street"] and s["street"] not in r["streets"]:
                r["streets"].append(s["street"])
        else:
            runs.append({"ride_id": s["ride_id"], "kind": s["kind"], "bicycle": s["bicycle"], "coords": [s["a"], s["b"]], "len_m": s["len_m"],
                         "last_k": s["k"], "tram_along": s["tram_along"], "streets": [s["street"]] if s["street"] else []})
    return runs


def slug(s):
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return s[:40] or "spot"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--only-source", default="", help="comma list, e.g. google-timeline,reconstructed")
    ap.add_argument("--rides", default=None, help="rides dir (default data/rides)")
    ap.add_argument("--osm", default="auto", choices=["auto", "local", "overpass"],
                    help="auto = local Geofabrik extract if present, else Overpass")
    ap.add_argument("--trim-ends", type=float, default=150, help="privacy: drop the first/last N metres of real rides")
    args = ap.parse_args()

    rides = load_rides(args.rides)
    if args.only_source:
        keep = set(args.only_source.split(","))
        rides = [r for r in rides if r["source"] in keep]
    if not rides:
        raise SystemExit("no rides in data/rides/: run track_from_timeline.py or track_from_waypoints.py first")
    real = [r for r in rides if r["source"] != "demo"]
    if real:  # never mix the demo into a real build
        rides = real
    rides = [trim_ends(clean(r), args.trim_ends) for r in rides]
    is_demo = all(r["source"] == "demo" for r in rides)

    bb = bbox(rides)
    print(f"rides: {', '.join(r['id'] + '/' + r['source'] for r in rides)}; bbox S,W,N,E = {bb}")
    lines, points = fetch_osm(bb, args.refresh, args.osm)
    ways = Ways(lines)
    print(f"OSM: {len(ways.ways)} ways ({Counter(w['kind'] for w in ways.ways).most_common()}), {len(points)} nodes")

    mpd_stops = load_mpd_stops(rides)  # from scripts/trajectory.py (MovingPandas), if it has been run
    all_stops, all_segs, all_tx = [], [], []
    for r in rides:
        all_stops += mpd_stops[r["id"]] if r["id"] in mpd_stops else stops(r)
        all_segs += classify_segments(r, ways)
        all_tx += tram_crossings(r, ways)

    # A ride that runs along a tram street wobbles across the track centreline (GPS noise, or a routed line on
    # the road axis). If the ride is alongside tram tracks for most of the 25 m before and after, call it
    # "along" and leave it out of the crossing counts and candidates.
    for x in all_tx:
        segs = [s for s in all_segs if s["ride_id"] == x["ride_id"]]
        pos = next((i for i, s in enumerate(segs) if s["k"] >= x["k"]), len(segs) - 1)
        win, acc, i = [], 0.0, pos
        while i >= 0 and acc < 25:
            win.append(segs[i]); acc += segs[i]["len_m"]; i -= 1
        acc, i = 0.0, pos + 1
        while i < len(segs) and acc < 25:
            win.append(segs[i]); acc += segs[i]["len_m"]; i += 1
        L = sum(s["len_m"] for s in win) or 1
        if sum(s["len_m"] for s in win if s["tram_along"]) / L > 0.6 and x["angle"] < OBLIQUE:
            x["class"] = "along tracks (not counted)"
    along = [x for x in all_tx if x["class"].startswith("along")]
    all_tx = [x for x in all_tx if not x["class"].startswith("along")]
    print(f"tram crossings: {len(all_tx)} kept, {len(along)} dropped as riding along the tracks")

    # spots: Gio's + candidates
    spots = load_gio_spots()
    cands = []
    for s in all_stops:
        if not s["edge"]:
            cands.append({"title": f"Stop {s['dur_s']} s", "lat": s["lat"], "lon": s["lon"], "origin": "stop",
                          "felt": "", "photo": "", "why_candidate": f"stood still {s['dur_s']} s"})
    for x in all_tx:
        if x["angle"] < OBLIQUE:
            cands.append({"title": f"Tram tracks at {x['angle']}°", "lat": x["lat"], "lon": x["lon"], "origin": "tram",
                          "felt": "", "photo": "", "why_candidate": f"crossed tram tracks at {x['angle']}° ({x['class']})"})
    # drop candidates that sit on one of Gio's spots; merge candidates at the same junction (keep the first:
    # stops come before tram crossings, and the sharpest crossing wins among crossings)
    cands = [c for c in cands if all(dist_m((c["lon"], c["lat"]), (g["lon"], g["lat"])) > SPOT_RADIUS for g in spots)]
    cands.sort(key=lambda c: (c["origin"] != "stop", int(re.sub(r"\D", "", c["title"]) or 0)))
    merged = []
    for c in cands:
        if all(dist_m((c["lon"], c["lat"]), (m["lon"], m["lat"])) > SPOT_RADIUS for m in merged):
            merged.append(c)
    cands = merged
    for i, s in enumerate(spots, 1):
        s["id"] = s.get("id") or f"S{i:02d}"
    for i, c in enumerate(cands, 1):
        c["id"] = f"C{i:02d}"
    every = spots + cands
    for s in every:
        s.update(spot_context(s, ways, points, rides))
        near_tx = [x for x in all_tx if dist_m((x["lon"], x["lat"]), (s["lon"], s["lat"])) <= SPOT_RADIUS]
        s["tram_cross_angle"] = min(x["angle"] for x in near_tx) if near_tx else None
        s["osm_says"] = "; ".join(osm_says(s, min(near_tx, key=lambda x: x["angle"]) if near_tx else None))
    every.sort(key=lambda s: (s["origin"] != "gio", s["t"] or 0))

    # ride stats
    stats = []
    for r in rides:
        segs = [s for s in all_segs if s["ride_id"] == r["id"]]
        L = sum(s["len_m"] for s in segs) or 1
        by = Counter()
        for s in segs:
            by[s["kind"]] += s["len_m"]
        tx = [x for x in all_tx if x["ride_id"] == r["id"]]
        stats.append({"ride_id": r["id"], "source": r["source"], "start": r["props"].get("start", ""),
                      "km": round(L / 1000, 2), "minutes": round((r["times"][-1] - r["times"][0]) / 60, 1),
                      "share_dedicated_pct": round(100 * (by["cycleway"] + by["cycle track"]) / L),
                      "share_painted_pct": round(100 * by["painted lane"] / L),
                      "share_mixed_pct": round(100 * by["mixed traffic"] / L),
                      "km_with_tram_alongside": round(sum(s["len_m"] for s in segs if s["tram_along"]) / 1000, 2),
                      "tram_crossings": len(tx), "tram_crossings_sharp": sum(1 for x in tx if x["angle"] < SHARP),
                      "stops_20s": sum(1 for s in all_stops if s["ride_id"] == r["id"] and not s["edge"]),
                      "km_pedestrian": round(by["pedestrian street"] / 1000, 2),
                      "pedestrian_by_bicycle_tag_m": pedestrian_by_tag(segs),
                      "gps_points_dropped": r.get("dropped", 0),
                      "by_kind_m": {k: round(v) for k, v in by.items()}})
    receipts = ROOT / "data" / "lime-receipts.csv"
    if receipts.exists():  # what Lime's app says, next to what we rebuilt
        rec = {r["ride_id"]: r for r in csv.DictReader(open(receipts))}
        for s in stats:
            if s["ride_id"] in rec:
                r = rec[s["ride_id"]]
                s.update(lime_km=float(r["lime_km"]), lime_duration=r["lime_duration"], lime_when=f"{r['date']} {r['start']}–{r['end']}",
                         anchors=r["anchors"])
    for s in stats:
        print(f"  {s['ride_id']}: {s['km']} km, {s['minutes']} min, dedicated {s['share_dedicated_pct']}%, painted "
              f"{s['share_painted_pct']}%, mixed {s['share_mixed_pct']}%, tram alongside {s['km_with_tram_alongside']} km, "
              f"tram crossings {s['tram_crossings']} (sharp {s['tram_crossings_sharp']}), stops {s['stops_20s']}")

    # ── tables
    DERIVED.mkdir(parents=True, exist_ok=True)
    cols = ["id", "origin", "title", "lat", "lon", "streets", "felt", "why_candidate", "osm_says", "tram_tracks_30m",
            "tram_min_dist_m", "tram_cross_angle", "cycle_infra_30m", "maxspeed_max", "lanes_max", "surfaces",
            "signals_30m", "crossings_30m", "crossing_types", "bike_crossings_30m", "tram_stop_30m", "road_classes",
            "ride_id", "ride_dist_m", "photo"]
    with open(DERIVED / "spots_context.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for s in every:
            w.writerow({k: (round(s[k], 6) if k in ("lat", "lon") else s.get(k, "")) for k in cols})
    (DERIVED / "ride_stats.json").write_text(json.dumps(stats, indent=1))

    # ── GeoPackage
    OUT.mkdir(exist_ok=True)
    if GPKG.exists():
        GPKG.unlink()
    runs = merge_runs(all_segs)
    n = {}
    n["01_rides"] = write_layer([ln(r["coords"], {"ride_id": r["id"], "source": r["source"], "start": r["props"].get("start", "")}) for r in rides], "01_rides")
    n["02_ride_by_infra"] = write_layer([ln(r["coords"], {"ride_id": r["ride_id"], "kind": r["kind"], "len_m": round(r["len_m"]),
                                                        "tram_alongside": int(r["tram_along"]), "bicycle_tag": r["bicycle"],
                                                        "streets": ", ".join(r["streets"])}) for r in runs], "02_ride_by_infra")
    n["03_spots"] = write_layer([pt(s["lon"], s["lat"], {k: s.get(k) for k in cols if k not in ("lat", "lon")}) for s in every], "03_spots")
    n["04_tram_crossings"] = write_layer([pt(x["lon"], x["lat"], {k: x[k] for k in ("ride_id", "angle", "class", "n_tracks")}) for x in all_tx + along], "04_tram_crossings")
    n["05_stops"] = write_layer([pt(s["lon"], s["lat"], {"ride_id": s["ride_id"], "dur_s": s["dur_s"], "edge": s["edge"],
                                                      "time": datetime.fromtimestamp(s["t_start"]).strftime("%H:%M:%S")}) for s in all_stops], "05_stops")
    osm_feats = lambda pred: [ln(w["ll"], {"osm_id": w["id"], "kind": w["kind"], "name": w["tags"].get("name", ""),
                                           "highway": w["tags"].get("highway", ""), "maxspeed": w["tags"].get("maxspeed", ""),
                                           "lanes": w["tags"].get("lanes", ""), "surface": w["tags"].get("surface", ""),
                                           "cycleway": w["tags"].get("cycleway", w["tags"].get("cycleway:right", w["tags"].get("cycleway:both", "")))})
                              for w in ways.ways if pred(w)]
    n["10_osm_cycle_infra"] = write_layer(osm_feats(lambda w: w["kind"] in ("cycleway", "cycle track", "painted lane", "shared lane")), "10_osm_cycle_infra")
    n["11_osm_tram_tracks"] = write_layer(osm_feats(lambda w: w["kind"] == "tram"), "11_osm_tram_tracks")
    n["12_osm_roads"] = write_layer(osm_feats(lambda w: w["kind"] not in ("tram",)), "12_osm_roads")
    n["13_osm_signals_crossings"] = write_layer([pt(e["lon"], e["lat"], {"osm_id": e["id"], "highway": e.get("tags", {}).get("highway", ""),
                                                                       "railway": e.get("tags", {}).get("railway", ""),
                                                                       "crossing": e.get("tags", {}).get("crossing", "")}) for e in points], "13_osm_signals_crossings")
    embed_styles()
    print("GPKG:", {k: v for k, v in n.items()})

    # ── per-spot READMEs
    sd = ROOT / "spots"
    for old in sd.glob("*.md"):
        old.unlink()
    for s in every:
        f = sd / f"{s['id']}-{slug(s['title'])}.md"
        f.write_text(spot_md(s, is_demo))
    (sd / "README.md").write_text(spots_index(every, is_demo))

    # ── web data
    write_story(rides, runs, ways, all_tx, all_stops, every, stats, is_demo)


def spot_md(s, is_demo):
    lat, lon = s["lat"], s["lon"]
    lines = [f"# {s['id']} · {s['title']}", ""]
    if is_demo:
        lines += ["> **Demo data.** This spot comes from a demo route, not from Gio's ride.", ""]
    lines += [f"**Where:** {s['streets'] or 'unnamed'} · [{lat:.5f}, {lon:.5f}](https://www.openstreetmap.org/?mlat={lat:.5f}&mlon={lon:.5f}#map=19/{lat:.5f}/{lon:.5f})",
              f"**Origin:** {'Gio' if s['origin'] == 'gio' else 'candidate, ' + s.get('why_candidate', '')}",
              "", "## What Gio felt", "", s.get("felt") or "_to fill in_", "",
              "## What OSM says (within 30 m)", ""]
    lines += [f"- {b}" for b in s["osm_says"].split("; ")]
    lines += ["", f"Road classes: {s['road_classes'] or 'n/a'}. Tram stop within 30 m: {'yes' if s['tram_stop_30m'] else 'no'}.",
              "", "## Photo", "", s.get("photo") or "_none yet_", "",
              "---", "Source: © OpenStreetMap contributors (ODbL), fetched via Overpass; see `data/raw/osm/` for the cached extract. "
              "OSM tells what is mapped, not what is built or how it feels: check on the ground."]
    return "\n".join(lines) + "\n"


def spots_index(every, is_demo):
    out = ["# Spots", "", "> Demo data: replace with Gio's rides and spots." if is_demo else "", "",
           "| id | origin | where | what Gio felt | what OSM says |", "|---|---|---|---|---|"]
    for s in every:
        out.append(f"| [{s['id']}]({s['id']}-{slug(s['title'])}.md) | {s['origin']} | {s['streets']} | {s.get('felt', '')} | {s['osm_says']} |")
    return "\n".join(out) + "\n"

# ───────────────────────── styles (SB palette, ABC Diatype) ─────────────────────────

FONT = "ABC Diatype"
CITRON, MAGENTA, ANTWERP, HOT, SALVIA, INK, GREY = "#A5CE39", "#E62E73", "#068AA1", "#FF8C42", "#97BFE6", "#121315", "#9CA0A1"
KIND_COLOURS = {"cycleway": "#2E7D32", "cycle track": "#2E7D32", "painted lane": CITRON, "parallel cycleway": "#7FAF4F",
                "shared lane": SALVIA, "quiet street": "#B9BCBD", "pedestrian street": "#B1477A", "path / track": "#D3D5D6", "mixed traffic": MAGENTA,
                "unmatched": GREY}


def _line(color, width, style="solid"):
    return (f'<layer class="SimpleLine" enabled="1"><Option type="Map"><Option type="QString" name="line_color" value="{color}"/>'
            f'<Option type="QString" name="line_width" value="{width}"/><Option type="QString" name="line_width_unit" value="MM"/>'
            f'<Option type="QString" name="line_style" value="{style}"/><Option type="QString" name="capstyle" value="round"/></Option></layer>')


def _marker(color, size, shape="circle"):
    return (f'<layer class="SimpleMarker" enabled="1"><Option type="Map"><Option type="QString" name="name" value="{shape}"/>'
            f'<Option type="QString" name="color" value="{color}"/><Option type="QString" name="outline_color" value="#ffffff"/>'
            f'<Option type="QString" name="outline_width" value="0.4"/><Option type="QString" name="size" value="{size}"/>'
            f'<Option type="QString" name="size_unit" value="MM"/></Option></layer>')


def _sym(kind, name, body):
    return f'<symbol type="{kind}" name="{name}" alpha="1" clip_to_extent="1" force_rhr="0">{body}</symbol>'


def _label(field, size=9):
    return (f'<labeling type="simple"><settings><text-style fontFamily="{FONT}" fontSize="{size}" fontBold="1" textColor="#121315" '
            f'fieldName="{field}"><text-buffer bufferDraw="1" bufferSize="0.9" bufferSizeUnits="MM" bufferColor="#ffffff"/></text-style>'
            f'<placement placement="1" xOffset="2" yOffset="-2" offsetUnits="MM"/></settings></labeling>')


def qml_single(kind, body, label=""):
    return f'<qgis styleCategories="Symbology|Labeling" version="3.40"><renderer-v2 type="singleSymbol"><symbols>{_sym(kind, "0", body)}</symbols></renderer-v2>{label}</qgis>'


def qml_cat(kind, attr, cats, label=""):
    cx = "".join(f'<category render="true" symbol="{i}" value="{v}" label="{v}" type="string"/>' for i, (v, _) in enumerate(cats))
    sx = "".join(_sym(kind, str(i), b) for i, (_, b) in enumerate(cats))
    return f'<qgis styleCategories="Symbology|Labeling" version="3.40"><renderer-v2 type="categorizedSymbol" attr="{attr}"><categories>{cx}</categories><symbols>{sx}</symbols></renderer-v2>{label}</qgis>'


def present(con, layer, col):
    try:
        return [r[0] for r in con.execute(f'SELECT DISTINCT "{col}" FROM "{layer}" WHERE "{col}" IS NOT NULL ORDER BY 1')]
    except sqlite3.OperationalError:
        return []


def embed_styles():
    con = sqlite3.connect(GPKG)
    con.execute("""CREATE TABLE IF NOT EXISTS layer_styles (id INTEGER PRIMARY KEY AUTOINCREMENT, f_table_catalog TEXT,
        f_table_schema TEXT, f_table_name TEXT, f_geometry_column TEXT, styleName TEXT, styleQML TEXT, styleSLD TEXT,
        useAsDefault BOOLEAN, description TEXT, owner TEXT, ui TEXT, update_time DATETIME DEFAULT CURRENT_TIMESTAMP)""")
    con.execute("DELETE FROM layer_styles")
    kinds = lambda layer: [(k, _line(KIND_COLOURS.get(k, GREY), "1.4" if layer.startswith("02") else "0.6")) for k in present(con, layer, "kind")]
    styles = {
        "01_rides": qml_single("line", _line(INK, "0.5", "dash")),
        "02_ride_by_infra": qml_cat("line", "kind", kinds("02_ride_by_infra")),
        "03_spots": qml_cat("marker", "origin", [(o, _marker({"gio": MAGENTA, "tram": HOT, "stop": ANTWERP}.get(o, GREY), "4.2" if o == "gio" else "3", "circle" if o == "gio" else "diamond"))
                                                 for o in present(con, "03_spots", "origin")], _label("id", 10)),
        "04_tram_crossings": qml_cat("marker", "class", [(c, _marker({"sharp (<30°)": MAGENTA, "oblique (30–60°)": HOT}.get(c, GREY), "2.6", "triangle"))
                                                         for c in present(con, "04_tram_crossings", "class")]),
        "05_stops": qml_single("marker", _marker(ANTWERP, "2.2")),
        "10_osm_cycle_infra": qml_cat("line", "kind", kinds("10_osm_cycle_infra")),
        "11_osm_tram_tracks": qml_single("line", _line("#5b5e5f", "0.45", "dash")),
        "12_osm_roads": qml_single("line", _line("#C9CBCC", "0.25")),
        "13_osm_signals_crossings": qml_cat("marker", "highway", [(h, _marker({"traffic_signals": INK, "crossing": GREY}.get(h, "#D3D5D6"), "1.6"))
                                                                  for h in present(con, "13_osm_signals_crossings", "highway")]),
    }
    for layer, qml in styles.items():
        if con.execute("SELECT 1 FROM gpkg_contents WHERE table_name=?", (layer,)).fetchone():
            con.execute("INSERT INTO layer_styles (f_table_catalog,f_table_schema,f_table_name,f_geometry_column,styleName,styleQML,styleSLD,useAsDefault,description,owner,ui) "
                        "VALUES ('','',?,'geom',?,?,'',1,'Turin by Lime, SB palette','giovanto','')", (layer, layer[3:].replace("_", " "), qml))
    if not con.execute("SELECT 1 FROM gpkg_contents WHERE table_name='layer_styles'").fetchone():
        con.execute("INSERT INTO gpkg_contents (table_name,data_type,identifier,description,last_change) VALUES "
                    "('layer_styles','attributes','layer_styles','QGIS layer styles',strftime('%Y-%m-%dT%H:%M:%fZ','now'))")
    con.commit()
    con.close()

# ───────────────────────── web ─────────────────────────

def write_story(rides, runs, ways, all_tx, all_stops, every, stats, is_demo):
    """web/data/story.js: one global (window.STORY), so the page also works from file://."""
    # one continuous story clock: rides back to back, 20 s gap between them
    t_off, clock, out_rides = 0.0, {}, []
    for r in sorted(rides, key=lambda r: r["times"][0]):
        t0 = r["times"][0]
        clock[r["id"]] = (t0, t_off)
        out_rides.append({"id": r["id"], "source": r["source"], "start": r["props"].get("start", ""), "t0": t0, "offset": t_off,
                          "fixes": [[f[0], f[1], round(f[2] - t0 + t_off, 1)] for f in r["props"].get("fixes", [])],
                          "path": [[round(x, 6), round(y, 6)] for x, y in r["coords"]],
                          "timestamps": [round(t - t0 + t_off, 1) for t in r["times"]]})
        t_off += (r["times"][-1] - t0) + 20
    story_t = lambda rid, t: (t - clock[rid][0] + clock[rid][1]) if rid in clock and t is not None else None

    def clip(w, pad=150):
        xs, ys = zip(*[xy(*p) for r in rides for p in r["coords"]])
        x0, x1, y0, y1 = min(xs) - pad, max(xs) + pad, min(ys) - pad, max(ys) + pad
        return any(x0 <= x <= x1 and y0 <= y <= y1 for x, y in w["xy"])

    spots = []
    for s in every:
        spots.append({"id": s["id"], "origin": s["origin"], "title": s["title"], "lon": round(s["lon"], 6), "lat": round(s["lat"], 6),
                      "streets": s["streets"], "felt": s.get("felt", ""), "osm": s["osm_says"],
                      # images: the web copy made by photos.py export; videos stay off the web for now
                      "photo": (s["photo"] if Path(s["photo"]).suffix.lower() in (".mp4", ".mov") else Path(s["photo"]).stem + ".jpg") if s.get("photo") else "",
                      "t": story_t(s["ride_id"], s["t"]), "tram_angle": s.get("tram_cross_angle")})
    moments = []
    mf = ROOT / "data" / "moments.csv"
    if mf.exists():  # photos and video, placed on the ride clock by their own time (see scripts/photos.py)
        from zoneinfo import ZoneInfo
        for m in csv.DictReader(open(mf)):
            t = datetime.strptime(m["time"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=ZoneInfo("Europe/Rome")).timestamp()
            lon, lat = float(m["lon"]), float(m["lat"])
            near = min(((dist_m((lon, lat), (s["lon"], s["lat"])), s) for s in every), key=lambda x: x[0], default=(1e9, None))
            moments.append({"id": m["id"], "t": story_t(m["ride_id"], t), "lon": lon, "lat": lat, "ride_id": m["ride_id"],
                            "media": m["media"].split("|"), "caption": m["caption"], "clock": m["time"][11:16],
                            "osm": near[1]["osm_says"] if near[1] and near[0] <= 35 else "",
                            "streets": near[1]["streets"] if near[1] and near[0] <= 35 else ""})
    data = {
        "moments": moments,
        "meta": {"title": "Turin by Lime", "demo": is_demo, "reconstructed": any(r["source"] == "reconstructed" for r in rides),
                 "matched": any(r["source"] == "timeline+router" for r in rides),
                 "built": datetime.now().strftime("%Y-%m-%d %H:%M"),
                 "sources": ["Rides: " + ", ".join(sorted({r['source'] for r in rides})),
                             "Context: © OpenStreetMap contributors (ODbL)", "Basemap: OpenFreeMap, OpenMapTiles"]},
        "stats": stats, "rides": out_rides,
        "infra": [{"kind": r["kind"], "bicycle": r["bicycle"], "path": [[round(x, 6), round(y, 6)] for x, y in r["coords"]], "len": round(r["len_m"]),
                   "streets": ", ".join(r["streets"])} for r in runs],
        "osm_cycle": [{"kind": w["kind"], "path": [[round(x, 6), round(y, 6)] for x, y in w["ll"]]}
                      for w in ways.ways if w["kind"] in ("cycleway", "cycle track", "painted lane") and clip(w)],
        "trams": [[[round(x, 6), round(y, 6)] for x, y in w["ll"]] for w in ways.ways if w["kind"] == "tram" and clip(w)],
        "tram_crossings": [{"lon": round(x["lon"], 6), "lat": round(x["lat"], 6), "angle": x["angle"], "class": x["class"],
                            "t": story_t(x["ride_id"], x["t"])} for x in all_tx],
        "stops": [{"lon": round(s["lon"], 6), "lat": round(s["lat"], 6), "dur": s["dur_s"], "t": story_t(s["ride_id"], s["t_start"]),
                   "ride_id": s["ride_id"],
                   "streets": next((e["streets"] for e in every if e["origin"] == "stop" and dist_m((e["lon"], e["lat"]), (s["lon"], s["lat"])) < 30), "")}
                  for s in all_stops if not s["edge"]],
        "spots": spots,
        "kind_colours": KIND_COLOURS,
    }
    WEB_DATA.mkdir(parents=True, exist_ok=True)
    (WEB_DATA / "story.js").write_text("window.STORY = " + json.dumps(data, ensure_ascii=False, separators=(",", ":")) + ";\n")
    print(f"web/data/story.js: {len(out_rides)} rides, {len(spots)} spots, {len(data['trams'])} tram ways, "
          f"{(WEB_DATA / 'story.js').stat().st_size // 1024} KB")


if __name__ == "__main__":
    main()
