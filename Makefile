PY := python3
EXPORT ?= Timeline.json
PHOTOS ?= photos

.PHONY: osm timeline demo build serve photos photos-web

osm:        ## local OSM for Turin from Geofabrik (faster and steadier than Overpass)
	$(PY) scripts/osm_extract.py

timeline:   ## cut the rides out of a Google Timeline export by Lime receipt times, map-match, trim
	$(PY) scripts/track_from_timeline.py "$(EXPORT)" --windows data/lime-receipts.csv --snap

demo:       ## demo ride (routed, not recorded) so the map has something to show
	$(PY) scripts/track_from_waypoints.py data/waypoints-demo.csv --source demo

build:      ## OSM context, spots, GeoPackage, per-spot READMEs, web data
	$(PY) scripts/build.py

serve:      ## local preview on http://127.0.0.1:8765/
	$(PY) -m http.server 8765 --bind 127.0.0.1 --directory web

photos:     ## place photos by EXIF time/GPS (metadata only) → data/derived/photos.csv
	$(PY) scripts/photos.py list "$(PHOTOS)"

photos-web: ## 1600 px, metadata-stripped copies of the photos named in data/spots.csv
	$(PY) scripts/photos.py export "$(PHOTOS)"
