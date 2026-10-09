# Original synthetic survey fixtures

These samples are authored for this project using `scripts/generate_samples.py`; they contain no real survey or personal data and require no external downloads.

| File | Contents / expected behavior |
|---|---|
| `projected_polygons.zip` | EPSG:32643; 100 × 100 m square, square with a 20 × 20 m hole, two disjoint squares; areas 10000, 9600, 20000 m² |
| `projected_lines.zip` | EPSG:32643; offsets of 300 m and 400 m; length 500 m |
| `geographic_polygons.zip` | Same square transformed to EPSG:4326; area approximately 10000 m² |
| `missing_crs.zip` | Geographic square without `.prj`; requires `source_crs=EPSG:4326` |
| `survey.kml` | Two folders/layers, square, line and Z-enabled point; arbitrary ExtendedData |

ZIP components live in a subdirectory to exercise archive discovery. Properties differ between samples. Expected values are also provided in `expected.json`. The source reference square begins at (500000, 2000000) in UTM zone 43N. KML coordinates use longitude, latitude, elevation. The point has no area/length measurement.

Generation fixes ZIP timestamps and DBF dates for repeatability with the locked reader/writer versions. Regenerate from the root with `uv run python -m scripts.generate_samples`.
