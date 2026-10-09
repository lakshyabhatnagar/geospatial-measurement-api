import base64
import math
from datetime import date, datetime

import numpy as np
import shapely
from pyproj import CRS
from shapely.errors import GEOSException
from shapely.geometry import mapping

from app.config import Settings
from app.crs import geographic_geometry, reproject, select_utm
from app.errors import FeatureIssue, issue


def normalize_value(value, warnings: list):
    if isinstance(value, np.datetime64):
        return None if np.isnat(value) else str(value)
    if isinstance(value, np.generic):
        value = value.item()
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        warnings.append(
            issue("NONFINITE_ATTRIBUTE", "A non-finite attribute was converted to null.")
        )
        return None
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, (list, tuple, np.ndarray)):
        return [normalize_value(item, warnings) for item in value]
    if isinstance(value, dict):
        return {str(key): normalize_value(item, warnings) for key, item in value.items()}
    if isinstance(value, bytes):
        warnings.append(issue("BINARY_ATTRIBUTE", "A binary attribute was encoded as base64."))
        return base64.b64encode(value).decode("ascii")
    raise TypeError(f"Unexpected attribute type: {type(value).__name__}")


def process_feature(raw_wkb: bytes | None, properties: dict, source: CRS, settings: Settings):
    result = dict(
        original_wkb=raw_wkb,
        geometry_type=None,
        geometry=None,
        properties={},
        measurement_status="ERROR",
        measurement_kind=None,
        measurement_value=None,
        measurement_unit=None,
        measurement_crs=None,
        errors=[],
        warnings=[],
    )
    result["properties"] = normalize_value(properties, result["warnings"])
    coordinate_count = 0
    try:
        if raw_wkb is None:
            raise FeatureIssue("MISSING_GEOMETRY", "The feature has no geometry.")
        try:
            geometry = shapely.from_wkb(raw_wkb)
        except GEOSException as exc:
            raise FeatureIssue("GEOMETRY_DECODE_FAILED", "The geometry cannot be decoded.") from exc
        result["geometry_type"] = geometry.geom_type
        coordinate_count = int(shapely.get_num_coordinates(geometry))
        if coordinate_count > settings.max_feature_coordinates:
            raise FeatureIssue(
                "FEATURE_COMPLEXITY_LIMIT", "The feature contains too many coordinates."
            )
        if geometry.is_empty:
            raise FeatureIssue("EMPTY_GEOMETRY", "The geometry is empty.")
        if not np.isfinite(shapely.get_coordinates(geometry)).all():
            raise FeatureIssue("INVALID_COORDINATES", "Geometry coordinates must be finite.")
        if shapely.has_z(geometry) or shapely.has_m(geometry):
            result["warnings"].append(
                issue("EXTRA_DIMENSIONS_IGNORED", "Measurements and output geometry use XY only.")
            )
        geographic = geographic_geometry(geometry, source)
        result["geometry"] = mapping(geographic)
        if not geometry.is_valid:
            raise FeatureIssue("INVALID_GEOMETRY", str(shapely.is_valid_reason(geometry)))
        if geometry.geom_type in {"Point", "MultiPoint"}:
            result["measurement_status"] = "NOT_APPLICABLE"
            return result, coordinate_count
        if geometry.geom_type not in {"Polygon", "MultiPolygon", "LineString", "MultiLineString"}:
            result["measurement_status"] = "UNSUPPORTED"
            result["errors"].append(
                issue("UNSUPPORTED_GEOMETRY", "No measurement is defined for this geometry type.")
            )
            return result, coordinate_count
        target = select_utm(geographic, settings)
        projected = reproject(geographic, CRS.from_epsg(4326), target)
        if not projected.is_valid:
            raise FeatureIssue("INVALID_PROJECTED_GEOMETRY", "The transformed geometry is invalid.")
        area = geometry.geom_type in {"Polygon", "MultiPolygon"}
        value = float(projected.area if area else projected.length)
        if not math.isfinite(value) or value < 0:
            raise FeatureIssue("INVALID_MEASUREMENT", "Measurement is non-finite or negative.")
        result.update(
            measurement_status="MEASURED",
            measurement_kind="area" if area else "length",
            measurement_value=value,
            measurement_unit="m2" if area else "m",
            measurement_crs=target.to_string(),
        )
    except FeatureIssue as exc:
        result["errors"].append(issue(exc.code, exc.message))
    except GEOSException:
        result["errors"].append(issue("GEOMETRY_OPERATION_FAILED", "A geometry operation failed."))
    return result, coordinate_count
