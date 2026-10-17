"""Local OSM for Turin from the Geofabrik extract, instead of the Overpass API.

    python3 scripts/osm_extract.py            # uses data/raw/geofabrik/nord-ovest-latest.osm.pbf
    python3 scripts/osm_extract.py --download # fetch the extract first (~590 MB, refreshed daily)

osmium cuts the Turin box, keeps the tags the analysis needs, exports GeoJSON lines, and this script
rewrites them in Overpass `out geom` shape (data/raw/osm/local_lines.json, local_points.json), so
build.py and locate.py read them the same way as an Overpass answer. SB's own Overpass doesn't cover Italy.
"""
import argparse
import json
import subprocess
from datetime import datetime, timezone

from common import RAW

PBF_URL = "https://download.geofabrik.de/europe/italy/nord-ovest-latest.osm.pbf"
GEOFABRIK = RAW / "geofabrik"
SRC = GEOFABRIK / "nord-ovest-latest.osm.pbf"
TORINO_BOX = (7.55, 44.98, 7.80, 45.15)  # W, S, E, N: the city plus a margin
FILTERS = ["w/highway", "w/railway=tram,light_rail",
           "n/highway=traffic_signals,crossing,stop,give_way",
           "n/railway=tram_crossing,tram_level_crossing,level_crossing,crossing,tram_stop"]


def run(cmd):
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise SystemExit(f"{cmd[0]} failed: {p.stderr.strip()[:400]}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--download", action="store_true")
    args = ap.parse_args()
    GEOFABRIK.mkdir(parents=True, exist_ok=True)
    if args.download or not SRC.exists():
        run(["curl", "-sL", "-A", "turin-by-lime/0.1", "-o", str(SRC), PBF_URL])

    box = GEOFABRIK / "torino.osm.pbf"
    sel = GEOFABRIK / "torino-mobility.osm.pbf"
    seq = GEOFABRIK / "torino-mobility.geojsonseq"
    run(["osmium", "extract", "-b", ",".join(map(str, TORINO_BOX)), "-s", "complete_ways", str(SRC), "-o", str(box), "-O"])
    run(["osmium", "tags-filter", str(box), *FILTERS, "-o", str(sel), "-O"])
    run(["osmium", "export", str(sel), "-f", "geojsonseq", "--geometry-types=point,linestring",
         "--add-unique-id=type_id", "-o", str(seq), "-O"])

    lines, points = [], []
    for row in open(seq):
        row = row.strip().lstrip("\x1e")
        if not row:
            continue
        f = json.loads(row)
        props, g = f["properties"], f["geometry"]
        fid = str(f.get("id") or props.pop("@id", ""))
        tags = {k: v for k, v in props.items() if not k.startswith("@")}
        if g["type"] == "LineString" and fid.startswith("w"):
            lines.append({"type": "way", "id": int(fid[1:]), "tags": tags,
                          "geometry": [{"lat": y, "lon": x} for x, y in g["coordinates"]]})
        elif g["type"] == "Point" and fid.startswith("n"):
            x, y = g["coordinates"]
            points.append({"type": "node", "id": int(fid[1:]), "lat": y, "lon": x, "tags": tags})
    stamp = datetime.fromtimestamp(SRC.stat().st_mtime, timezone.utc).strftime("%Y-%m-%d")
    meta = {"source": f"Geofabrik {PBF_URL.split('/')[-1]} (downloaded {stamp}), osmium extract {TORINO_BOX}"}
    (RAW / "osm").mkdir(parents=True, exist_ok=True)
    (RAW / "osm" / "local_lines.json").write_text(json.dumps({**meta, "elements": lines}))
    (RAW / "osm" / "local_points.json").write_text(json.dumps({**meta, "elements": points}))
    trams = sum(1 for e in lines if e["tags"].get("railway") in ("tram", "light_rail"))
    print(f"Turin: {len(lines)} ways ({trams} tram), {len(points)} nodes → data/raw/osm/local_*.json")


if __name__ == "__main__":
    main()
