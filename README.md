# Turin by Lime

I rode Lime bikes between my hotel and [Utopian Hours](https://utopianhours.it/en/) in Turin (16–17 October 2026). This repo turns those three rides into a small map story: where it felt off to someone used to cycling in Amsterdam, and what OpenStreetMap says is there.

**→ [giovanto.github.io/turin-by-lime](https://giovanto.github.io/turin-by-lime/)** (made for phones too: swipe or tap Next)

## What came out

- **Pedestrian streets.** The navigation kept sending me into pedestrian areas. About 1.2 km of my rides ran through them: OSM says bikes are allowed on 759 m of that and says nothing on the other 459 m. Allowed, yes (a firefighter confirmed it), but riding between people on foot felt unpleasant, and risky for them more than for me.
- **Tram tracks.** I crossed them 17 times. Twice at under 30°, where a wheel can drop into the rail: at Ponte Bologna (about 19°, right next to the conference) and at Piazza Carlo Emanuele II (about 26°, on cobbles).
- **Mixed traffic.** Between 55% and 74% of each ride was in traffic, with no cycle track or painted lane. On the second ride I took a detour to stay out of the pedestrian streets of the historic centre. It worked, but there were still plenty of tram tracks, mixed traffic and cobbles. Very annoying to ride.
- **One long wait.** 2 minutes standing still at Piazza Castello / Via Roma.

Every number is approximate: GPS wobbles, and OSM is a map of the street, not the street.

## How it works

1. **Track.** Lime doesn't let riders export their rides (its [data-sharing repo](https://github.com/limebike/data-sharing) is the feed for cities). So the track is my phone's own location history (Google Timeline export), cut by the start and end times on my Lime receipts: 25–31 points per ride, one every 30–40 seconds.
2. **Map matching.** The points are matched to streets with [OSRM](https://project-osrm.org/)'s bike profile on the FOSSGIS server.
3. **Stops and speed** with [MovingPandas](https://movingpandas.org/) by Anita Graser. With a point every ~35 s it finds waits of half a minute or more, not a 10-second red light.
4. **Street context** from OpenStreetMap ([Geofabrik](https://download.geofabrik.de/) extract, cut with osmium): cycleways and lanes, pedestrian streets with their `bicycle` tag, tram tracks, crossings, signals, speed limits, lanes, surface. Each stretch of a ride takes the type of the nearest OSM way within 12 m. A tram crossing's angle is measured on 20 m either side.
5. **Photos and video** are placed by their own time and GPS. The web copies have no metadata.
6. **Privacy.** The first and last 150 m of every ride are dropped before anything gets saved.

Lime says the three rides were 2.2, 2.7 and 3.0 km. The matched street path comes out about 15% longer, a straight line through my points a bit shorter. Read distances as ±15%.

## Run it

```bash
make osm                                     # OSM for Turin from Geofabrik (~600 MB download, a few seconds to cut)
make timeline EXPORT=Timeline.json           # rides from a Google Timeline export, cut by the Lime receipts
uv venv .venv && uv pip install --python .venv/bin/python -r requirements-trajectory.txt
.venv/bin/python scripts/trajectory.py       # stops and speed (MovingPandas)
make build                                   # spots, GeoPackage, spot pages, web data
make serve                                   # http://127.0.0.1:8765/
```

Everything except `trajectory.py` is Python standard library plus GDAL (`ogr2ogr`, I use the one inside QGIS), `osmium` and `exiftool`.

| Path | What |
|---|---|
| `scripts/track_from_timeline.py` | Cuts rides out of a Google Timeline export by receipt times, map-matches them, trims the ends |
| `scripts/trajectory.py` | Stops and speed with MovingPandas |
| `scripts/osm_extract.py` | OSM for Turin from Geofabrik via osmium |
| `scripts/build.py` | Ride profile, tram crossings, spots with what OSM has within 30 m, GeoPackage, spot pages, web data |
| `scripts/photos.py`, `scripts/locate.py` | Photo placement from metadata; "Corso X / Via Y" to coordinates |
| `data/` | Lime receipts, rides, photo moments, derived tables |
| `out/turin-by-lime.gpkg` | Everything as a styled GeoPackage for QGIS |
| `spots/` | One page per spot |
| `web/` | The story: plain HTML, MapLibre and deck.gl, no build step. Captions in `data/moments.csv`, text in `web/data/text.js` |

## Licence

- Code: [MIT](LICENSE).
- Data derived from OpenStreetMap (`out/`, `data/derived/`, `spots/`, `web/data/story.js`): © OpenStreetMap contributors, [ODbL 1.0](https://opendatacommons.org/licenses/odbl/1-0/).
- My rides, photos, video and text: [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).

Thanks to the people behind MovingPandas, OSRM and the FOSSGIS routing server, Geofabrik, OpenFreeMap, MapLibre, deck.gl and every OpenStreetMap mapper in Turin.
