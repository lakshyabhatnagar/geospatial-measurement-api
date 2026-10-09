# Assignment evaluation notes

This report maps the assignment brief to the implementation and gives an evaluator a short, reproducible walkthrough. The service is designed for a local survey upload and completes processing synchronously.

## Requirements checklist

| Assignment requirement | Evidence in this repository |
|---|---|
| Use Django/DRF or FastAPI | FastAPI application in `app/main.py`; OpenAPI at `/docs` |
| Accept `.kml` and zipped Shapefile | `POST /api/files/`; validated staging in `app/uploads.py` |
| Extract feature ID/index, geometry type, geometry, CRS, and properties | `feature_index`, source ID and layer identity; source WKB and WGS84 GeoJSON; arbitrary properties and field schema |
| Handle unsupported geometry gracefully | Per-feature `UNSUPPORTED`/`NOT_APPLICABLE` status and issue list |
| Measure polygons and lines in projected coordinates | Shapely measurements after strict PyProj transformation to a per-feature UTM CRS |
| Do not measure in longitude/latitude degrees | CRS selection and transformation in `app/crs.py`; calculations in `app/measurement.py` |
| Upload, file information, measurements | `POST /api/files/`, `GET /api/files/{id}/`, `GET /api/files/{id}/measurements/` |
| Document setup, API examples, architecture, flow, CRS, and decisions | Root `README.md` |
| Explain learning and future scope | “Learning outcomes and future scope” in the README |
| Public GitHub submission | [github.com/lakshyabhatnagar/geospatial-measurement-api](https://github.com/lakshyabhatnagar/geospatial-measurement-api) |
| Live Render deployment | [https://geospatial-api-72jz.onrender.com](https://geospatial-api-72jz.onrender.com); readiness, health, docs and end-to-end KML measurement were checked on 2026-10-09. |

## Run the evaluator walkthrough

Local setup is in the README. Once the service is running, upload the included KML and inspect the measurements:

```bash
curl -i -F 'file=@samples/survey.kml' http://127.0.0.1:8000/api/files/
curl http://127.0.0.1:8000/api/files/FILE_ID/
curl 'http://127.0.0.1:8000/api/files/FILE_ID/measurements/?limit=50&offset=0'
```

The sample contains a polygon, a line, and a point across two KML folders. Expected outcomes are three features, two measurements, and one point with `NOT_APPLICABLE` status. `samples/expected.json` records the known reference values. The zipped Shapefile fixtures cover holes, multipart polygons, geographic input, missing CRS metadata, and attribute preservation.

For Render, use the live Docker Web Service at `https://geospatial-api-72jz.onrender.com`. Readiness is available at `/ready/`, and interactive API docs are at `/docs`. A persistent disk mounted at `/app/data` is required if uploaded records must survive service restarts and deploys.

## Verification evidence

The committed CI workflow runs lint and formatting checks, the API and geometry suite, Alembic migration checks, and a Docker build. The latest verified run passed [GitHub Actions run 37904727364](https://github.com/lakshyabhatnagar/geospatial-measurement-api/actions/runs/37904727364), including 52 passing tests and the Docker build.

The live smoke test returned HTTP 201 for a one-feature KML polygon, then HTTP 200 for its file and measurements lookups. It reported one completed area measurement in EPSG:32619 and no feature errors. This confirms request handling and persistence during the live process; it does not by itself verify that a Render persistent disk is configured or that data survives a redeploy.

An additional manual integration check used the 12-feature Police Districts Shapefile from [Kaggle's Geospatial Learn Course Data](https://www.kaggle.com/datasets/alexisbcook/geospatial-learn-course-data). The API returned `COMPLETED`, measured all 12 polygons, and reported zero feature errors. The source CRS was EPSG:4326 and the selected measurement CRS was EPSG:32619. Independent recalculation in that projected CRS matched the API areas to under 0.001 m². Those downloaded files are kept in the ignored local `data/kaggle/` directory rather than committed; Kaggle reports the source dataset license as unknown.

## Scope and limitations

The measurement policy is deliberately scoped to local features: each measurable feature must be within UTM latitude bounds, avoid antimeridian crossing, and have a geographic bounding-box diagonal of at most 100 km. Measurements are two-dimensional projected estimates, not survey-grade cadastral determinations. Points have no required measurement. Invalid geometries are not automatically repaired.

Approach A is implemented. Durable queue workers (Approach B) and PostGIS (Approach C) are documented as future architectures, not claimed as delivered. Other known boundaries, including KML presentation handling, no authentication/ownership, synchronous request duration, and native GDAL timeout limitations, are documented in the README.
