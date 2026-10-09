from datetime import date

import numpy as np
import pytest
import shapely
from pyproj import CRS, Geod
from pyproj.exceptions import ProjError
from shapely.geometry import (
    GeometryCollection,
    LineString,
    MultiLineString,
    MultiPoint,
    Point,
    Polygon,
)

from app.config import Settings
from app.measurement import normalize_value, process_feature
from scripts.generate_samples import square


def measure(geometry, crs=4326, **settings):
    raw = None if geometry is None else shapely.to_wkb(geometry)
    return process_feature(raw, {}, CRS.from_user_input(crs), Settings(**settings))[0]


@pytest.mark.parametrize(
    "geometry,code",
    [
        (None, "MISSING_GEOMETRY"),
        (Point(), "EMPTY_GEOMETRY"),
        (
            Polygon([(75, 18), (75.001, 18.001), (75, 18.001), (75.001, 18), (75, 18)]),
            "INVALID_GEOMETRY",
        ),
        (LineString([(75, 18), (77, 18)]), "OUTSIDE_MEASUREMENT_SCOPE"),
        (LineString([(179.99, 0), (-179.99, 0)]), "OUTSIDE_MEASUREMENT_SCOPE"),
        (LineString([(0, 85), (0.01, 85)]), "OUTSIDE_MEASUREMENT_SCOPE"),
    ],
)
def test_geometry_errors(geometry, code):
    result = measure(geometry)
    assert result["measurement_status"] == "ERROR"
    assert result["errors"][0]["code"] == code
    assert result["measurement_value"] is None


def test_raw_decode_failure():
    result, count = process_feature(b"broken", {"name": "retained"}, CRS(4326), Settings())
    assert result["original_wkb"] == b"broken"
    assert result["properties"] == {"name": "retained"}
    assert result["errors"][0]["code"] == "GEOMETRY_DECODE_FAILED"
    assert count == 0


@pytest.mark.parametrize("geometry", [Point(75, 18), MultiPoint([(75, 18), (75.001, 18)])])
def test_points(geometry):
    assert measure(geometry)["measurement_status"] == "NOT_APPLICABLE"


def test_collection_and_complexity():
    result = measure(GeometryCollection([Point(75, 18)]))
    assert result["measurement_status"] == "UNSUPPORTED"
    assert result["geometry"] is not None
    result = measure(square(), 32643, max_feature_coordinates=4)
    assert result["errors"][0]["code"] == "FEATURE_COMPLEXITY_LIMIT"


@pytest.mark.parametrize("crs,y", [(32643, 2000000), (32743, 8000000)])
def test_hemispheres(crs, y):
    result = measure(square(y=y), crs)
    assert result["measurement_value"] == pytest.approx(10000, abs=0.00001)
    assert result["measurement_crs"] == f"EPSG:{crs}"


def test_feet_and_custom_wkt():
    feet = CRS.from_proj4("+proj=utm +zone=43 +datum=WGS84 +units=ft +no_defs")
    geometry = shapely.transform(square(), lambda xy: xy / 0.3048)
    result = measure(geometry, feet.to_wkt())
    assert result["measurement_value"] == pytest.approx(10000, abs=0.00001)


def test_multiline_and_z():
    line = LineString([(500000, 2000000, 0), (500300, 2000400, 1000)])
    result = measure(MultiLineString([line, line]), 32643)
    assert result["measurement_value"] == pytest.approx(1000, abs=0.00001)
    assert shapely.has_z(shapely.from_wkb(result["original_wkb"]))
    assert not shapely.has_z(shapely.geometry.shape(result["geometry"]))


def test_zone_boundary_and_geodesic_comparison():
    line = LineString([(5.999, 18), (6.001, 18)])
    result = measure(line)
    assert result["measurement_crs"] == "EPSG:32632"
    independent = Geod(ellps="WGS84").geometry_length(line)
    assert result["measurement_value"] == pytest.approx(independent, rel=0.002)


def test_unavailable_transformation(monkeypatch):
    def unavailable(*args, **kwargs):
        raise ProjError("missing grid")

    monkeypatch.setattr("app.crs.Transformer.from_crs", unavailable)
    result = measure(square(), 32643)
    assert result["errors"][0]["code"] == "TRANSFORMATION_UNAVAILABLE"
    assert result["geometry"] is None


def test_attributes():
    warnings = []
    value = normalize_value(
        {
            "unicode": "測量",
            "date": date(2020, 1, 2),
            "number": np.int64(7),
            "nan": float("nan"),
            "null": None,
        },
        warnings,
    )
    assert value == {
        "unicode": "測量",
        "date": "2020-01-02",
        "number": 7,
        "nan": None,
        "null": None,
    }
    assert warnings[0]["code"] == "NONFINITE_ATTRIBUTE"
