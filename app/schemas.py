from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, FiniteFloat


class Issue(BaseModel):
    code: str
    message: str


class FileResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    filename: str
    format: str
    status: Literal["PROCESSING", "COMPLETED", "COMPLETED_WITH_ERRORS", "FAILED"]
    crs: str | None
    crs_wkt: str | None
    crs_origin: str | None
    layers: list[dict[str, Any]]
    feature_count: int
    measured_count: int
    error_count: int
    not_applicable_count: int
    unsupported_count: int
    created_at: str
    completed_at: str | None
    processing_ms: float | None
    warnings: list[Issue]
    error: Issue | None
    policy_version: str


class Measurement(BaseModel):
    kind: Literal["area", "length"]
    value: FiniteFloat
    unit: Literal["m", "m2"]
    measurement_crs: str
    method: str = "projected_utm_2d"


class FeatureResponse(BaseModel):
    feature_index: int
    source_feature_id: str | None
    layer_index: int
    layer_name: str
    geometry_type: str | None
    geometry: dict[str, Any] | None
    geometry_crs: str = "OGC:CRS84"
    source_crs: str
    properties: dict[str, Any]
    measurement_status: Literal["MEASURED", "NOT_APPLICABLE", "ERROR", "UNSUPPORTED"]
    measurement: Measurement | None
    errors: list[Issue]
    warnings: list[Issue]


class MeasurementPage(BaseModel):
    file_id: str
    total: int
    limit: int
    offset: int
    items: list[FeatureResponse]


class ErrorDetail(BaseModel):
    code: str
    message: str
    request_id: str
    file_id: str | None = None
    details: list[dict[str, Any]] = []


class ErrorResponse(BaseModel):
    error: ErrorDetail
