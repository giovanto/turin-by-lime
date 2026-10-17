"""Stops and speed from the phone's own fixes with MovingPandas (Anita Graser, AIT): https://movingpandas.org

    .venv/bin/python scripts/trajectory.py        # needs the project venv: uv venv .venv && uv pip install movingpandas

Reads properties.fixes of every ride in data/rides/ (written by track_from_timeline.py --snap: lon, lat,
epoch s, accuracy m, speed m/s, already privacy-trimmed) and writes data/derived/stops.csv and
data/derived/fixes_speed.csv. build.py uses stops.csv when it exists (stdlib fallback otherwise).
The fixes are 30-40 s apart, so a stop is "stayed within 25 m across at least two fixes, 25 s or more":
it can tell a 2-minute wait from riding, not a 10-second red light.
"""
import csv
import json
import warnings
from datetime import timedelta

warnings.filterwarnings("ignore", message="Missing optional dependencies")

import geopandas as gpd  # noqa: E402
import movingpandas as mpd  # noqa: E402
import pandas as pd  # noqa: E402
from pathlib import Path  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
RIDES, DERIVED = ROOT / "data" / "rides", ROOT / "data" / "derived"
MAX_DIAMETER_M, MIN_DURATION = 25, timedelta(seconds=25)


def main():
    stops, speeds = [], []
    for f in sorted(RIDES.glob("*.geojson")):
        p = json.loads(f.read_text())["properties"]
        fx = p.get("fixes") or []
        if len(fx) < 3:
            continue
        df = pd.DataFrame(fx, columns=["lon", "lat", "t", "acc", "speed_phone"])
        df["time"] = pd.to_datetime(df["t"], unit="s", utc=True).dt.tz_convert("Europe/Rome").dt.tz_localize(None)
        gdf = gpd.GeoDataFrame(df, geometry=gpd.points_from_xy(df.lon, df.lat), crs="EPSG:4326").set_index("time")
        traj = mpd.Trajectory(gdf, traj_id=p["ride_id"])
        traj.add_speed(overwrite=True, units=("km", "h"))
        for t, row in traj.df.iterrows():
            speeds.append({"ride_id": p["ride_id"], "time": t.strftime("%H:%M:%S"), "lon": round(row.lon, 6), "lat": round(row.lat, 6),
                           "kmh_between_fixes": round(row.speed, 1), "kmh_phone": round(3.6 * row.speed_phone, 1) if pd.notna(row.speed_phone) else ""})
        det = mpd.TrajectoryStopDetector(traj)
        for _, s in det.get_stop_points(min_duration=MIN_DURATION, max_diameter=MAX_DIAMETER_M).iterrows():
            stops.append({"ride_id": p["ride_id"], "start": s.start_time.strftime("%Y-%m-%d %H:%M:%S"),
                          "end": s.end_time.strftime("%H:%M:%S"), "dur_s": round(s.duration_s),
                          "lon": round(s.geometry.x, 6), "lat": round(s.geometry.y, 6)})
        print(f"{p['ride_id']}: {len(fx)} fixes, {sum(1 for s in stops if s['ride_id'] == p['ride_id'])} stop(s)")
    DERIVED.mkdir(parents=True, exist_ok=True)
    for name, rows in (("stops.csv", stops), ("fixes_speed.csv", speeds)):
        with open(DERIVED / name, "w", newline="") as fh:
            if rows:
                w = csv.DictWriter(fh, fieldnames=list(rows[0]))
                w.writeheader()
                w.writerows(rows)
    for s in stops:
        print(f"  {s['ride_id']} {s['start'][11:]}–{s['end']}  {s['dur_s']:4d} s  {s['lat']}, {s['lon']}")


if __name__ == "__main__":
    main()
