import warnings

import pyogrio
from pyogrio import raw
from pyogrio.errors import (
    CRSError,
    DataLayerError,
    DataSourceError,
    FeatureError,
    FieldError,
    GeometryError,
)

from app.config import Settings
from app.errors import ServiceError
from app.uploads import Dataset, check_deadline

READ_ERRORS = (CRSError, DataLayerError, DataSourceError, FeatureError, FieldError, GeometryError)


def read_layers(dataset: Dataset, settings: Settings, deadline: float):
    """Yield one bounded raw layer at a time; driver failures invalidate the file."""
    total = 0
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            layers = pyogrio.list_layers(dataset.path)
            if len(layers) > settings.max_layers:
                raise ServiceError("LAYER_LIMIT", "Too many dataset layers.", 413)
            for index, (name, _) in enumerate(layers):
                check_deadline(deadline)
                info = pyogrio.read_info(dataset.path, layer=index)
                expected_driver = "LIBKML" if dataset.format == "KML" else "ESRI Shapefile"
                if info["driver"] != expected_driver:
                    raise ServiceError(
                        "FORMAT_MISMATCH", "File content does not match its expected driver."
                    )
                remaining = settings.max_features - total
                if info["features"] > remaining:
                    raise ServiceError("FEATURE_LIMIT", "Too many features in this upload.", 413)
                metadata, fids, geometries, fields = raw.read(
                    dataset.path,
                    layer=index,
                    max_features=remaining + 1,
                    return_fids=True,
                    datetime_as_string=True,
                )
                count = len(fids)
                total += count
                if total > settings.max_features:
                    raise ServiceError("FEATURE_LIMIT", "Too many features in this upload.", 413)
                if info["features"] >= 0 and count != info["features"]:
                    raise ServiceError(
                        "FEATURE_EXTRACTION_MISMATCH",
                        "The reader did not return all layer features.",
                    )
                layer = {
                    "index": index,
                    "name": str(name),
                    "feature_count": count,
                    "fields": [
                        {"name": str(n), "type": str(t)}
                        for n, t in zip(metadata["fields"], metadata["dtypes"], strict=True)
                    ],
                }
                yield layer, fids, geometries, fields
                check_deadline(deadline)
            # Driver warnings can indicate silently skipped or changed geometry. Fail closed.
            if caught:
                raise ServiceError(
                    "DATASET_READ_WARNING",
                    "The reader reported a warning; re-export the dataset "
                    "to avoid incomplete extraction.",
                )
        if dataset.placemark_count is not None and total != dataset.placemark_count:
            raise ServiceError(
                "FEATURE_EXTRACTION_MISMATCH", "KML placemark and extracted-feature counts differ."
            )
    except READ_ERRORS as exc:
        raise ServiceError("DATASET_READ_FAILED", "The dataset cannot be read completely.") from exc
