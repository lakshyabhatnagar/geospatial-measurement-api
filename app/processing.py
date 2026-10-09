import logging
import tempfile
import time
import uuid
from pathlib import Path

from sqlalchemy import insert
from sqlalchemy.exc import SQLAlchemyError

from app.config import Settings
from app.crs import resolve_source
from app.database import get_file, storage_error
from app.errors import ServiceError, issue
from app.measurement import process_feature
from app.models import FeatureRecord, FileRecord, now
from app.reader import read_layers
from app.uploads import check_deadline, cleanup_directory, stage_upload

logger = logging.getLogger(__name__)


class ProcessingService:
    def __init__(self, settings: Settings, sessions):
        self.settings = settings
        self.sessions = sessions

    def process(self, stream, filename: str, supplied_crs: str | None, request_id: str):
        started = time.monotonic()
        deadline = started + self.settings.processing_seconds
        directory = None
        file_id = None
        try:
            directory = Path(tempfile.mkdtemp(prefix="upload-", dir=self.settings.uploads_dir))
            dataset = stage_upload(stream, filename, directory, self.settings, deadline)
            file_id = str(uuid.uuid4())
            with self.sessions.begin() as session:
                session.add(FileRecord(id=file_id, filename=filename, format=dataset.format))
            logger.info(
                "processing_started",
                extra={"file_id": file_id, "request_id": request_id, "stage": "read"},
            )
            source = resolve_source(dataset, supplied_crs)
            if source.origin == "USER_SUPPLIED":
                dataset.path.with_suffix(".prj").write_text(source.crs.to_wkt(), encoding="utf-8")
                dataset.warnings.append(
                    issue("SOURCE_CRS_SUPPLIED", "Source CRS was supplied by the uploader.")
                )
            features = []
            layers = []
            coordinates = 0
            for layer, fids, geometries, fields in read_layers(dataset, self.settings, deadline):
                layers.append(layer)
                for index, fid in enumerate(fids):
                    check_deadline(deadline)
                    properties = {
                        field["name"]: values[index]
                        for field, values in zip(layer["fields"], fields, strict=True)
                    }
                    wkb = (
                        None
                        if geometries is None or geometries[index] is None
                        else bytes(geometries[index])
                    )
                    result, count = process_feature(wkb, properties, source.crs, self.settings)
                    coordinates += count
                    if coordinates > self.settings.max_coordinates:
                        raise ServiceError(
                            "COORDINATE_LIMIT", "Upload exceeds its total coordinate budget.", 413
                        )
                    result.update(
                        file_id=file_id,
                        feature_index=len(features),
                        layer_index=layer["index"],
                        layer_name=layer["name"],
                        source_feature_id=str(fid) if fid is not None else None,
                    )
                    features.append(result)
            if not features:
                raise ServiceError("EMPTY_DATASET", "The file contains no vector features.")
            check_deadline(deadline)
            statuses = [item["measurement_status"] for item in features]
            counts = {
                status: statuses.count(status)
                for status in ("MEASURED", "NOT_APPLICABLE", "ERROR", "UNSUPPORTED")
            }
            duration = (time.monotonic() - started) * 1000
            with self.sessions.begin() as session:
                record = session.get(FileRecord, file_id)
                record.crs = source.label
                record.crs_wkt = source.crs.to_wkt()
                record.crs_origin = source.origin
                record.layers = layers
                record.feature_count = len(features)
                record.measured_count = counts["MEASURED"]
                record.not_applicable_count = counts["NOT_APPLICABLE"]
                record.error_count = counts["ERROR"]
                record.unsupported_count = counts["UNSUPPORTED"]
                record.status = (
                    "COMPLETED_WITH_ERRORS"
                    if counts["ERROR"] + counts["UNSUPPORTED"]
                    else "COMPLETED"
                )
                record.completed_at = now()
                record.processing_ms = duration
                record.warnings = dataset.warnings
                # One transaction; chunked statements avoid SQLite parameter limits.
                for start in range(0, len(features), 200):
                    session.execute(insert(FeatureRecord), features[start : start + 200])
            logger.info(
                "processing_completed",
                extra={
                    "file_id": file_id,
                    "request_id": request_id,
                    "stage": "commit",
                    "duration_ms": duration,
                    "feature_count": len(features),
                },
            )
            return get_file(self.sessions, file_id)
        except Exception as exc:
            if isinstance(exc, ServiceError):
                error = exc
            elif isinstance(exc, SQLAlchemyError):
                error = storage_error(exc)
                if error.status == 500:
                    logger.exception(
                        "database_programming_error",
                        extra={"file_id": file_id, "request_id": request_id},
                    )
            elif isinstance(exc, OSError):
                error = ServiceError("STORAGE_UNAVAILABLE", "Upload storage is unavailable.", 503)
            else:
                error = ServiceError(
                    "INTERNAL_ERROR", "An unexpected processing error occurred.", 500
                )
                logger.exception(
                    "processing_failed", extra={"file_id": file_id, "request_id": request_id}
                )
            error.file_id = file_id
            logger.warning(
                "processing_rejected",
                extra={
                    "file_id": file_id,
                    "request_id": request_id,
                    "stage": "failure",
                    "error_code": error.code,
                    "duration_ms": (time.monotonic() - started) * 1000,
                },
            )
            if file_id:
                self.mark_failed(file_id, error)
            raise error from exc
        finally:
            if directory is not None:
                try:
                    cleanup_directory(directory)
                except OSError:
                    logger.exception(
                        "cleanup_failed", extra={"file_id": file_id, "request_id": request_id}
                    )

    def mark_failed(self, file_id, error):
        try:
            with self.sessions.begin() as session:
                record = session.get(FileRecord, file_id)
                # Never overwrite an already committed success (e.g. failed response retrieval).
                if record and record.status == "PROCESSING":
                    record.status = "FAILED"
                    record.completed_at = now()
                    record.error = issue(error.code, error.message)
        except SQLAlchemyError:
            logger.exception("failure_record_unavailable", extra={"file_id": file_id})
