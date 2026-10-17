"""Shared paths and helpers. Stdlib only; GDAL comes from the QGIS bundle."""
import json
import math
import os
import shutil
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"            # caches (git-ignored)
RIDES = ROOT / "data" / "rides"        # one GeoJSON per ride, LineString + properties.times
DERIVED = ROOT / "data" / "derived"
OUT = ROOT / "out"
SPOTS_CSV = ROOT / "data" / "spots.csv"
GPKG = OUT / "turin-by-lime.gpkg"
WEB_DATA = ROOT / "web" / "data"

UA = "turin-by-lime/0.1 (https://github.com/giovanto; personal analysis)"
QGIS = Path("/Applications/QGIS-final-4_2_1.app/Contents")
OGR = shutil.which("ogr2ogr") or str(QGIS / "MacOS" / "ogr2ogr")
OGR_ENV = dict(os.environ)
if not shutil.which("ogr2ogr"):  # the QGIS-bundled binary needs its data dirs spelled out
    OGR_ENV.update(GDAL_DATA=str(QGIS / "Resources/qgis/gdal"), PROJ_DATA=str(QGIS / "Resources/qgis/proj"),
                   PROJ_LIB=str(QGIS / "Resources/qgis/proj"))
OVERPASS = ["https://overpass-api.de/api/interpreter",
            "https://overpass.kumi.systems/api/interpreter",
            "https://overpass.private.coffee/api/interpreter"]

# Local metric frame around Turin (equirectangular; fine at the 30 m scale)
LAT0, LON0 = 45.07, 7.68
KX = 111320.0 * math.cos(math.radians(LAT0))
KY = 110540.0


def xy(lon, lat):
    return ((lon - LON0) * KX, (lat - LAT0) * KY)


def lonlat(x, y):
    return (x / KX + LON0, y / KY + LAT0)


def dist_m(a, b):
    """a, b = (lon, lat)"""
    ax, ay = xy(*a)
    bx, by = xy(*b)
    return math.hypot(ax - bx, ay - by)


def seg_point_dist(p, a, b):
    """Distance in metres from point p to segment ab, all (x, y) in metres. Returns (d, t)."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    L2 = dx * dx + dy * dy
    t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / L2))
    qx, qy = a[0] + t * dx, a[1] + t * dy
    return math.hypot(p[0] - qx, p[1] - qy), t


def line_dist(p, coords_xy):
    """Min distance from point p (x, y) to a polyline [(x, y), ...]; returns (d, segment index)."""
    best, idx = float("inf"), -1
    for i in range(len(coords_xy) - 1):
        d, _ = seg_point_dist(p, coords_xy[i], coords_xy[i + 1])
        if d < best:
            best, idx = d, i
    return best, idx


def bearing(a, b):
    """Compass bearing in degrees from a to b, (x, y) metres."""
    return (math.degrees(math.atan2(b[0] - a[0], b[1] - a[1])) + 360) % 360


def crossing_angle(b1, b2):
    """Acute angle between two undirected lines with bearings b1, b2 (0..90)."""
    d = abs(b1 - b2) % 180
    return min(d, 180 - d)


def seg_intersect(p1, p2, q1, q2):
    """Intersection point of segments p1p2 and q1q2 in (x, y), or None."""
    r = (p2[0] - p1[0], p2[1] - p1[1])
    s = (q2[0] - q1[0], q2[1] - q1[1])
    den = r[0] * s[1] - r[1] * s[0]
    if abs(den) < 1e-9:
        return None
    qp = (q1[0] - p1[0], q1[1] - p1[1])
    t = (qp[0] * s[1] - qp[1] * s[0]) / den
    u = (qp[0] * r[1] - qp[1] * r[0]) / den
    if 0 <= t <= 1 and 0 <= u <= 1:
        return (p1[0] + t * r[0], p1[1] + t * r[1]), t
    return None


def http_get_json(url, cache: Path, refresh=False, data=None, tries=3, pause=6):
    """GET (or POST if data) JSON with a cache file. One flaky source never blocks a rebuild."""
    if cache.exists() and not refresh:
        return json.loads(cache.read_text())
    cache.parent.mkdir(parents=True, exist_ok=True)
    last = None
    for _ in range(tries):
        try:
            body = urllib.parse.urlencode(data).encode() if data else None
            req = urllib.request.Request(url, data=body, headers={"User-Agent": UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=240) as r:
                txt = r.read().decode()
            obj = json.loads(txt)
            cache.write_text(txt)
            return obj
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(pause)
    raise RuntimeError(f"fetch failed: {url} ({last})")


def overpass(query, cache: Path, refresh=False):
    if cache.exists() and not refresh:
        return json.loads(cache.read_text())
    last = None
    for ep in OVERPASS * 2:
        try:
            return http_get_json(ep, cache, refresh=True, data={"data": query}, tries=1)
        except Exception as e:  # noqa: BLE001
            last = e
            print(f"   overpass {ep.split('/')[2]}: {e}; next")
            time.sleep(8)
    raise RuntimeError(f"overpass failed ({last})")


def load_rides(rides_dir=None):
    """All rides as dicts: id, source, coords [(lon, lat)], times [epoch s], props."""
    rides = []
    for f in sorted(Path(rides_dir or RIDES).glob("*.geojson")):
        feat = json.loads(f.read_text())
        if feat.get("type") == "FeatureCollection":
            feat = feat["features"][0]
        p = feat["properties"]
        rides.append({"id": p["ride_id"], "source": p["source"], "coords": [tuple(c[:2]) for c in feat["geometry"]["coordinates"]],
                      "times": p["times"], "props": p})
    return rides


def write_ride(ride_id, coords, times, source, out_dir=None, **extra):
    out_dir = Path(out_dir) if out_dir else RIDES
    out_dir.mkdir(parents=True, exist_ok=True)
    feat = {"type": "Feature",
            "geometry": {"type": "LineString", "coordinates": [[round(x, 6), round(y, 6)] for x, y in coords]},
            "properties": {"ride_id": ride_id, "source": source, "times": [int(t) for t in times], **extra}}
    (out_dir / f"{ride_id}.geojson").write_text(json.dumps(feat))
    return out_dir / f"{ride_id}.geojson"
