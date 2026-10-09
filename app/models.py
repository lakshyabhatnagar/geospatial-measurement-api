from datetime import UTC, datetime

from sqlalchemy import JSON, CheckConstraint, Float, ForeignKey, Integer, LargeBinary, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def now() -> str:
    return datetime.now(UTC).isoformat()


class Base(DeclarativeBase):
    pass


class FileRecord(Base):
    __tablename__ = "files"
    __table_args__ = (
        CheckConstraint("status IN ('PROCESSING','COMPLETED','COMPLETED_WITH_ERRORS','FAILED')"),
        CheckConstraint(
            "feature_count >= 0 AND measured_count >= 0 AND error_count >= 0 "
            "AND not_applicable_count >= 0 AND unsupported_count >= 0"
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    filename: Mapped[str] = mapped_column(String(255))
    format: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(32), default="PROCESSING")
    crs: Mapped[str | None] = mapped_column(Text)
    crs_wkt: Mapped[str | None] = mapped_column(Text)
    crs_origin: Mapped[str | None] = mapped_column(String(32))
    layers: Mapped[list] = mapped_column(JSON, default=list)
    feature_count: Mapped[int] = mapped_column(Integer, default=0)
    measured_count: Mapped[int] = mapped_column(Integer, default=0)
    error_count: Mapped[int] = mapped_column(Integer, default=0)
    not_applicable_count: Mapped[int] = mapped_column(Integer, default=0)
    unsupported_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[str] = mapped_column(String(40), default=now)
    completed_at: Mapped[str | None] = mapped_column(String(40))
    processing_ms: Mapped[float | None] = mapped_column(Float)
    warnings: Mapped[list] = mapped_column(JSON, default=list)
    error: Mapped[dict | None] = mapped_column(JSON)
    policy_version: Mapped[str] = mapped_column(String(32), default="local-utm-v1")


class FeatureRecord(Base):
    __tablename__ = "features"
    __table_args__ = (
        CheckConstraint("feature_index >= 0 AND layer_index >= 0"),
        CheckConstraint(
            "measurement_status IN ('MEASURED','NOT_APPLICABLE','UNSUPPORTED','ERROR')"
        ),
        CheckConstraint(
            "(measurement_status = 'MEASURED' AND measurement_value IS NOT NULL "
            "AND measurement_value >= 0 AND measurement_kind IS NOT NULL "
            "AND measurement_unit IS NOT NULL AND measurement_crs IS NOT NULL) OR "
            "(measurement_status != 'MEASURED' AND measurement_value IS NULL "
            "AND measurement_kind IS NULL AND measurement_unit IS NULL "
            "AND measurement_crs IS NULL)"
        ),
    )
    file_id: Mapped[str] = mapped_column(
        ForeignKey("files.id", ondelete="CASCADE"), primary_key=True
    )
    feature_index: Mapped[int] = mapped_column(Integer, primary_key=True)
    layer_index: Mapped[int] = mapped_column(Integer)
    layer_name: Mapped[str] = mapped_column(Text)
    source_feature_id: Mapped[str | None] = mapped_column(Text)
    geometry_type: Mapped[str | None] = mapped_column(String(64))
    original_wkb: Mapped[bytes | None] = mapped_column(LargeBinary)
    geometry: Mapped[dict | None] = mapped_column(JSON)
    properties: Mapped[dict] = mapped_column(JSON)
    measurement_status: Mapped[str] = mapped_column(String(32))
    measurement_kind: Mapped[str | None] = mapped_column(String(16))
    measurement_value: Mapped[float | None] = mapped_column(Float)
    measurement_unit: Mapped[str | None] = mapped_column(String(8))
    measurement_crs: Mapped[str | None] = mapped_column(String(32))
    errors: Mapped[list] = mapped_column(JSON, default=list)
    warnings: Mapped[list] = mapped_column(JSON, default=list)
