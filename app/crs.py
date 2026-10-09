import math
import re
from dataclasses import dataclass

import numpy as np
import shapely
from pyproj import CRS, Geod, Transformer
from pyproj.exceptions import CRSError, ProjError

from app.config import Settings
from app.errors import FeatureIssue, ServiceError
from app.uploads import Dataset


@dataclass(frozen=True)
class SourceCRS:
    crs: CRS
    origin: str

    @property
    def label(self):
        authority = self.crs.to_authority()
        return ":".join(authority) if authority else self.crs.to_wkt()


def resolve_source(dataset: Dataset, supplied: str | None) -> SourceCRS:
    explicit = None
    if supplied is not None:
        if not re.fullmatch(r"EPSG:[1-9][0-9]{0,6}", supplied, re.IGNORECASE):
            raise ServiceError(
                "INVALID_SOURCE_CRS", "source_crs must be an EPSG identifier, e.g. EPSG:4326."
            )
        try:
            explicit = CRS.from_user_input(supplied)
        except CRSError as exc:
            raise ServiceError(
                "INVALID_SOURCE_CRS", "The supplied EPSG identifier is unknown."
            ) from exc
    embedded = None
    if dataset.format == "KML":
        embedded = CRS.from_epsg(4326)
    else:
        prj = dataset.path.with_suffix(".prj")
        if prj.exists():
            try:
                if prj.stat().st_size > 65536:
                    raise ServiceError("INVALID_SOURCE_CRS", "CRS metadata exceeds 64 KiB.")
                embedded = CRS.from_wkt(prj.read_text(encoding="utf-8-sig"))
            except (CRSError, UnicodeError):
                embedded = None
    if embedded and explicit and not embedded.equals(explicit, ignore_axis_order=True):
        raise ServiceError(
            "CRS_CONFLICT", "source_crs conflicts with the file's coordinate system."
        )
    selected = embedded or explicit
    if selected is None:
        raise ServiceError("CRS_REQUIRED", "Include a valid .prj file or provide source_crs.")
    if not (selected.is_projected or selected.is_geographic) or selected.is_compound:
        raise ServiceError(
            "UNSUPPORTED_SOURCE_CRS", "A geographic or projected horizontal CRS is required."
        )
    origin = (
        ("KML_STANDARD" if dataset.format == "KML" else "FILE") if embedded else "USER_SUPPLIED"
    )
    return SourceCRS(selected, origin)


def reproject(geometry, source: CRS, target: CRS):
    try:
        transformer = Transformer.from_crs(
            source.to_2d(), target.to_2d(), always_xy=True, allow_ballpark=False, only_best=True
        )

        def checked(x, y, z=None):
            return transformer.transform(x, y, errcheck=True)

        result = shapely.transform(geometry, checked, interleaved=False)
    except ProjError as exc:
        raise FeatureIssue(
            "TRANSFORMATION_UNAVAILABLE", "Required coordinate transformation is unavailable."
        ) from exc
    if not np.isfinite(shapely.get_coordinates(result)).all():
        raise FeatureIssue("INVALID_COORDINATES", "Transformation produced non-finite coordinates.")
    return result


def geographic_geometry(geometry, source: CRS):
    result = reproject(shapely.force_2d(geometry), source, CRS.from_epsg(4326))
    coordinates = shapely.get_coordinates(result)
    if (np.abs(coordinates[:, 0]) > 180).any() or (np.abs(coordinates[:, 1]) > 90).any():
        raise FeatureIssue(
            "INVALID_COORDINATES", "Coordinates fall outside longitude/latitude bounds."
        )
    return result


def select_utm(geometry, settings: Settings) -> CRS:
    left, bottom, right, top = geometry.bounds
    if right - left > 180 or bottom < -80 or top > 84:
        raise FeatureIssue(
            "OUTSIDE_MEASUREMENT_SCOPE",
            "Polar and antimeridian-crossing features are outside the local UTM policy.",
        )
    geod = Geod(ellps="WGS84")
    diagonals = [geod.inv(left, bottom, right, top)[2], geod.inv(left, top, right, bottom)[2]]
    if max(diagonals) > settings.max_extent_km * 1000:
        raise FeatureIssue(
            "OUTSIDE_MEASUREMENT_SCOPE", f"Feature extent exceeds {settings.max_extent_km:g} km."
        )
    longitude = (left + right) / 2
    latitude = (bottom + top) / 2
    zone = min(60, max(1, math.floor((longitude + 180) / 6) + 1))
    return CRS.from_epsg((32600 if latitude >= 0 else 32700) + zone)
