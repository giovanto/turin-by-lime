"""Place photos on the rides from metadata only (EXIF time + GPS via exiftool). Never opens the images.

    python3 scripts/photos.py list  /path/to/photo/folder     # → data/derived/photos.csv (+ nearest spot)
    python3 scripts/photos.py export /path/to/photo/folder    # web copies of the photos named in data/spots.csv

`list`: GPS from EXIF when present; otherwise the position on the ride at the photo's time (EXIF
DateTimeOriginal + OffsetTimeOriginal, falling back to the file name, e.g. IMG_20261016_093512.jpg).
`export`: 1600 px copies into web/photos/ with all metadata stripped (no GPS on the public site).
"""
import csv
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from common import DERIVED, ROOT, SPOTS_CSV, dist_m, load_rides

EXT = {".jpg", ".jpeg", ".heic", ".png", ".webp", ".mp4", ".mov"}
VIDEO = {".mp4", ".mov"}
NAME_TIME = re.compile(r"(20\d{2})(\d{2})(\d{2})[_-]?(\d{2})(\d{2})(\d{2})")


def exif(folder):
    out = subprocess.run(["exiftool", "-json", "-n", "-q", "-DateTimeOriginal", "-OffsetTimeOriginal", "-CreateDate",
                          "-GPSLatitude", "-GPSLongitude", "-ImageWidth", "-ImageHeight", "-ext", "jpg", "-ext", "jpeg",
                          "-ext", "heic", "-ext", "png", "-ext", "webp", "-ext", "mp4", "-ext", "mov", "-Duration", str(folder)], capture_output=True, text=True)
    return json.loads(out.stdout or "[]")


def when(rec):
    name = Path(rec["SourceFile"]).name
    m = NAME_TIME.search(name)
    if name.startswith("PXL_") and Path(name).suffix.lower() in VIDEO and m:
        # Pixel videos: CreateDate is the end of recording; the file name (UTC) is the start, which matches the track
        return datetime.strptime("".join(m.groups()) + "+0000", "%Y%m%d%H%M%S%z").timestamp()
    raw = rec.get("DateTimeOriginal") or rec.get("CreateDate")
    if raw:
        video = Path(rec["SourceFile"]).suffix.lower() in VIDEO
        # QuickTime CreateDate is UTC; photos carry their own offset (Turin in October is CEST)
        off = "+00:00" if video and not rec.get("DateTimeOriginal") else rec.get("OffsetTimeOriginal") or "+02:00"
        try:
            return datetime.strptime(raw[:19] + off, "%Y:%m:%d %H:%M:%S%z").timestamp()
        except ValueError:
            pass
    m = NAME_TIME.search(Path(rec["SourceFile"]).name)
    if m:  # Pixel names (PXL_…) are in UTC, most others in local time
        tz = "+0000" if Path(rec["SourceFile"]).name.startswith("PXL_") else "+0200"
        return datetime.strptime("".join(m.groups()) + tz, "%Y%m%d%H%M%S%z").timestamp()
    return None


def on_ride(t, rides):
    for r in rides:
        if r["times"][0] - 120 <= t <= r["times"][-1] + 120:
            k = min(range(len(r["times"])), key=lambda i: abs(r["times"][i] - t))
            return r["coords"][k], r["id"], abs(r["times"][k] - t)
    return None, None, None


def cmd_list(folder):
    rides = [r for r in load_rides() if r["source"] != "demo"] or load_rides()
    spots = list(csv.DictReader(open(DERIVED / "spots_context.csv"))) if (DERIVED / "spots_context.csv").exists() else []
    rows = []
    for rec in exif(folder):
        t = when(rec)
        lat, lon, how, ride = rec.get("GPSLatitude"), rec.get("GPSLongitude"), "exif-gps", ""
        if lat is None and t:
            p, ride, lag = on_ride(t, rides)
            if p:
                lon, lat, how = p[0], p[1], f"ride {ride} at photo time (±{lag:.0f} s)"
        near = ""
        if lat is not None and spots:
            d, s = min((dist_m((lon, lat), (float(s["lon"]), float(s["lat"]))), s) for s in spots)
            near = f"{s['id']} ({d:.0f} m)" if d <= 60 else ""
        rows.append({"file": Path(rec["SourceFile"]).name, "time": datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M:%S") if t else "",
                     "lat": round(lat, 6) if lat is not None else "", "lon": round(lon, 6) if lon is not None else "",
                     "placed_by": how if lat is not None else "unplaced", "nearest_spot": near,
                     "size": f"{rec.get('ImageWidth', '')}x{rec.get('ImageHeight', '')}",
                     "video_s": round(rec["Duration"], 1) if isinstance(rec.get("Duration"), (int, float)) else ""})
    rows.sort(key=lambda r: r["time"])
    DERIVED.mkdir(parents=True, exist_ok=True)
    with open(DERIVED / "photos.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]) if rows else ["file"])
        w.writeheader()
        w.writerows(rows)
    for r in rows:
        print(f"{r['time']:19}  {r['file']:32}  {r['placed_by']:28}  {r['nearest_spot']}")
    print(f"→ data/derived/photos.csv ({len(rows)} photos)")


def cmd_export(folder):
    chosen = {r["photo"] for r in csv.DictReader(open(SPOTS_CSV)) if r.get("photo")}
    dest = ROOT / "web" / "photos"
    dest.mkdir(parents=True, exist_ok=True)
    for name in sorted(chosen):
        src = Path(folder) / name
        if not src.exists():
            print(f"missing: {name}")
            continue
        out = dest / (Path(name).stem + ".jpg")
        subprocess.run(["sips", "-s", "format", "jpeg", "-s", "formatOptions", "82", "-Z", "1600", str(src), "--out", str(out)],
                       capture_output=True, check=True)
        subprocess.run(["exiftool", "-q", "-overwrite_original", "-all=", str(out)], check=True)
        print(f"web/photos/{out.name}  (1600 px, metadata stripped)")


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] not in ("list", "export"):
        raise SystemExit(__doc__)
    (cmd_list if sys.argv[1] == "list" else cmd_export)(sys.argv[2])
