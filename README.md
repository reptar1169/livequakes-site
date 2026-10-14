# LiveQuakes

**[livequakes.com](https://livequakes.com)** is a free, real-time earthquake tracker built on public
USGS data, with a community network of citizen-run seismometers.

- **Live map and list** of recent earthquakes worldwide, filterable by magnitude, time range,
  place, and distance from you, with auto-refresh and browser notifications.
- **Earthquake details** with shareable links (`livequakes.com/#quake=<USGS id>`), felt reports,
  tsunami and PAGER alert flags, and a plain-language description of each magnitude.
- **State pages** for California, Nevada, Oregon, Washington, Alaska, and Hawaii.
- **Fault lines**: an optional plate-boundary overlay, plus a detailed base map with state borders
  when zoomed in (up to 48×).
- **Earthquake risk by ZIP code**: the long-term chance of strong shaking from the USGS National
  Seismic Hazard Model, with local quake history. These are forecasts, not predictions.
- **Community stations**: anyone with a Raspberry Shake or DIY sensor can register it and report
  readings, which are automatically cross-checked against the USGS catalog. The reference DIY
  build (Raspberry Pi Pico 2 W + LSM6DSOX) lives in
  [livequakes-pico-sensor](https://github.com/reptar1169/livequakes-pico-sensor).

## How it works

```
Browser ──► USGS GeoJSON feeds          (live quakes, fetched directly in the browser)
   │
   ├──► Firebase Hosting                (static site: public/)
   │       └── /api/risk ──► areaRisk   (Cloud Function → USGS hazard model + catalog,
   │                                     cached in Firestore and at the CDN)
   └──► Firestore                       (community stations; read-only from the browser)

Stations ──► ingestEvent (Cloud Function, per-station API key)
checkUsgsCoincidence (every 5 min) ──► matches station events to real USGS quakes
```

There's no build step for the site itself: `public/index.html` is a single hand-written page.
The state pages are generated from it.

## Repository layout

| Path | What it is |
|---|---|
| `public/index.html` | The whole site (HTML, CSS, JS). **Edit this, not the state pages.** |
| `public/<state>.html` | Generated state pages; don't edit by hand. |
| `public/plates.json` | Plate boundaries, pre-projected for the map (generated). |
| `public/map-detail.json` | Detailed coastline and borders for zoomed-in views (generated). |
| `public/sitemap.xml` | Generated alongside the state pages. |
| `functions/index.js` | Cloud Functions: station registration, event ingest, USGS cross-check, area risk. |
| `functions/data/zcta.json` | ZIP code → location lookup for the risk feature. |
| `scripts/` | Generators for the files above. |
| `firestore.rules`, `firestore.indexes.json` | Firestore security rules and indexes. |
| `Pi Pico/`, `example_station_client.py` | Sensor firmware and a minimal example client. |

## Development

Preview the site locally:

```bash
python3 -m http.server 5055 --directory public
```

Then open `http://localhost:5055/`. The live earthquake map works locally. The risk lookup
(`/api/risk`) and community stations need the deployed Firebase backend.

Functions run on **Node.js 22**:

```bash
cd functions && npm install
```

## Deploying

After editing `public/index.html`, regenerate the state pages and sitemap, then deploy everything:

```bash
python3 scripts/build_regions.py
firebase deploy
```

`firebase deploy` with no `--only` flag deploys hosting, functions, Firestore rules, **and**
indexes. The USGS cross-check depends on the index in `firestore.indexes.json`.

### Adding a state page

1. Add an entry to `REGIONS` in `scripts/build_regions.py`. Each entry has a slug, a name, a regex
   matching USGS place names (e.g. `", CA$"`), a map bounding box, and a short intro.
2. Add a link to the `.region-nav` list in `public/index.html`.
3. Run `python3 scripts/build_regions.py` and deploy.

### Changing the risk API's response

Bump `HISTORY_VERSION` in `functions/index.js` **and** `RISK_API_VERSION` in `public/index.html`
together. This refreshes cached results in Firestore, browsers, and the CDN.

### Regenerating map data

These only need re-running if the source data changes:

```bash
# Plate boundaries, from PB2002_boundaries.json (github.com/fraxen/tectonicplates)
python3 scripts/build_plates.py PB2002_boundaries.json public/plates.json

# Detailed base map, from a folder holding Natural Earth's ne_50m_land,
# ne_50m_admin_0_boundary_lines_land and ne_50m_admin_1_states_provinces_lines GeoJSON
python3 scripts/build_map_detail.py path/to/natural-earth public/map-detail.json
```

## Community station API

Register a station on [livequakes.com](https://livequakes.com) (Google sign-in) to get an API key.
It's shown once, so save it. Then POST trigger events:

```bash
curl -X POST https://us-central1-livequakes.cloudfunctions.net/ingestEvent \
  -H "Content-Type: application/json" \
  -H "X-Api-Key: YOUR_STATION_KEY" \
  -d '{"amplitude": 42, "durationMs": 8000}'
```

| Field | Required | Notes |
|---|---|---|
| `amplitude` | yes | Relative, unitless trigger strength, 0–1000 |
| `durationMs` | yes | 0–600000 |
| `ts` | no | Epoch ms or ISO string; defaults to now |
| `note` | no | Up to 200 characters |

About 10–65 minutes after each event, it's checked against the USGS catalog (within ±10 minutes
and 500 km of the station) and marked as confirmed if a real quake matches.

Never commit a real API key or Wi-Fi password. `Pi Pico/config.py` and
`example_station_client.py` contain placeholders only.

## Data sources and credits

- **Earthquakes, hazard model, and catalog:** [USGS Earthquake Hazards Program](https://earthquake.usgs.gov/)
  (public domain). LiveQuakes is independent and not affiliated with or endorsed by USGS.
- **Plate boundaries:** Bird, P. (2003), *An updated digital model of plate boundaries*, G³ 4(3);
  GeoJSON by Hugo Ahlenius / Nordpil ([fraxen/tectonicplates](https://github.com/fraxen/tectonicplates)),
  [ODC-BY 1.0](https://opendatacommons.org/licenses/by/1-0/).
- **Base map:** [Natural Earth](https://www.naturalearthdata.com/) (public domain).
- **ZIP code locations:** U.S. Census Bureau ZCTA Gazetteer, 2024 (public domain).

## License

The code is released under the [MIT License](LICENSE). The generated data files keep their
sources' terms. `public/plates.json` is derived from PB2002 and stays under ODC-BY 1.0, which
requires attribution. `public/map-detail.json` and `functions/data/zcta.json` are derived from
public-domain sources.
