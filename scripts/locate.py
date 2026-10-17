"""Where two Turin streets meet, from OSM: turns "Corso X / Via Y" into lat/lon for data/spots.csv.

    python3 scripts/locate.py "Corso Regina Margherita" "Corso Giulio Cesare"
    python3 scripts/locate.py "Piazza della Repubblica"          # one name → its centre

Matches names case- and accent-insensitively, by substring. Fetches the named ways from Overpass for
the whole city (cached in data/raw/osm/names/), then takes the closest approach between the two.
"""
import hashlib
import json
import sys
import unicodedata

from common import RAW, lonlat, overpass, seg_point_dist, xy

TURIN = (44.99, 7.57, 45.15, 7.78)  # S, W, N, E


def norm(s):
    return "".join(c for c in unicodedata.normalize("NFKD", s.lower()) if not unicodedata.combining(c))


def ways_named(name):
    local = RAW / "osm" / "local_lines.json"
    if local.exists():  # the Geofabrik extract (scripts/osm_extract.py): no network
        return [(e["tags"]["name"], [xy(g["lon"], g["lat"]) for g in e["geometry"]])
                for e in json.loads(local.read_text())["elements"] if norm(name) in norm(e["tags"].get("name", ""))]
    S, W, N, E = TURIN
    key = hashlib.sha1(norm(name).encode()).hexdigest()[:12]
    q = f'[out:json][timeout:90];(way["name"~"{name}",i]["highway"]({S},{W},{N},{E});way["name"~"{name}",i]["place"]({S},{W},{N},{E}););out tags geom;'
    data = overpass(q, RAW / "osm" / "names" / f"{key}.json")
    out = []
    for el in data["elements"]:
        if norm(name) in norm(el.get("tags", {}).get("name", "")) and el.get("geometry"):
            out.append((el["tags"]["name"], [xy(p["lon"], p["lat"]) for p in el["geometry"]]))
    return out


def main():
    names = sys.argv[1:]
    if not names:
        raise SystemExit(__doc__)
    a = ways_named(names[0])
    if not a:
        raise SystemExit(f"no OSM way named like {names[0]!r} in Turin")
    if len(names) == 1:
        pts = [p for _, g in a for p in g]
        lon, lat = lonlat(sum(x for x, _ in pts) / len(pts), sum(y for _, y in pts) / len(pts))
        print(f"{lat:.6f},{lon:.6f}  # centre of {a[0][0]} ({len(a)} ways)")
        return
    b = ways_named(names[1])
    if not b:
        raise SystemExit(f"no OSM way named like {names[1]!r} in Turin")
    best = (float("inf"), None, "", "")
    for na, ga in a:
        for nb, gb in b:
            for p in ga:
                for i in range(len(gb) - 1):
                    d, t = seg_point_dist(p, gb[i], gb[i + 1])
                    if d < best[0]:
                        best = (d, p, na, nb)
    d, p, na, nb = best
    lon, lat = lonlat(*p)
    print(f"{lat:.6f},{lon:.6f}  # {na} / {nb}, gap {d:.0f} m" + ("  ⚠️ they don't meet: closest approach" if d > 15 else ""))


if __name__ == "__main__":
    main()
