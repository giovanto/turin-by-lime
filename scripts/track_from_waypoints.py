"""Rebuild a ride from waypoints with a bike router (BRouter) when there is no recorded track.

    python3 scripts/track_from_waypoints.py data/waypoints.csv [--source reconstructed|demo]

waypoints.csv columns: ride_id, seq, lat, lon, label, time (optional "YYYY-MM-DD HH:MM", local time).
Times are interpolated along the routed line between waypoints that have one; without any, the ride
starts at the first given time (or 2026-10-16 09:00) and runs at --kmh. Every ride written this way is
labelled with its source so the map never passes a reconstruction off as a recording.
"""
import argparse
import csv
import hashlib
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from common import RAW, http_get_json, write_ride, dist_m

BROUTER = "https://brouter.de/brouter"


def route(points, profile):
    lonlats = "|".join(f"{lon:.6f},{lat:.6f}" for lon, lat in points)
    key = hashlib.sha1(f"{lonlats}|{profile}".encode()).hexdigest()[:12]
    data = http_get_json(f"{BROUTER}?lonlats={lonlats}&profile={profile}&alternativeidx=0&format=geojson",
                         RAW / "brouter" / f"{key}.json")
    return [tuple(c[:2]) for c in data["features"][0]["geometry"]["coordinates"]]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv")
    ap.add_argument("--source", default="reconstructed")
    ap.add_argument("--profile", default="trekking")
    ap.add_argument("--kmh", type=float, default=14.0)
    args = ap.parse_args()

    by_ride = defaultdict(list)
    for row in csv.DictReader(open(args.csv)):
        by_ride[row["ride_id"]].append(row)

    for rid, rows in by_ride.items():
        rows.sort(key=lambda r: int(r["seq"]))
        coords, wp_idx = [], []
        for a, b in zip(rows, rows[1:]):
            leg = route([(float(a["lon"]), float(a["lat"])), (float(b["lon"]), float(b["lat"]))], args.profile)
            if coords:
                leg = leg[1:]
            wp_idx.append(len(coords))
            coords += leg
        wp_idx.append(len(coords) - 1)

        cum = [0.0]
        for k in range(1, len(coords)):
            cum.append(cum[-1] + dist_m(coords[k - 1], coords[k]))
        # known times at waypoints → interpolate; else constant speed
        parse = lambda v: datetime.strptime(v, "%Y-%m-%d %H:%M:%S" if v.count(":") == 2 else "%Y-%m-%d %H:%M").timestamp()
        known = [(cum[i], parse(r["time"])) for i, r in zip(wp_idx, rows) if r.get("time")]
        if len(known) < 2:
            t_start = known[0][1] if known else datetime(2026, 10, 16, 9, 0).timestamp()
            known = [(0.0, t_start), (cum[-1], t_start + cum[-1] / (args.kmh / 3.6))]
        times = []
        for d in cum:
            for (d0, t0), (d1, t1) in zip(known, known[1:]):
                if d <= d1 or (d1, t1) == known[-1]:
                    times.append(t0 + (t1 - t0) * ((d - d0) / (d1 - d0) if d1 > d0 else 0))
                    break
        start = datetime.fromtimestamp(times[0]).strftime("%Y-%m-%d %H:%M")
        p = write_ride(rid, coords, times, args.source, start=start, duration_min=round((times[-1] - times[0]) / 60, 1),
                       router=f"brouter/{args.profile}", waypoints=" → ".join(r["label"] for r in rows))
        print(f"{rid}: {len(coords)} pts, {cum[-1] / 1000:.2f} km, {args.source} → {p.relative_to(Path.cwd()) if p.is_relative_to(Path.cwd()) else p}")


if __name__ == "__main__":
    main()
