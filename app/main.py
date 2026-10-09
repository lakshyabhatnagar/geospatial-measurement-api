import json
import logging
from contextlib import asynccontextmanager
from typing import Annotated
from uuid import UUID

import anyio
from fastapi import FastAPI, File, Form, Query, Request, Response, UploadFile
from fastapi.exceptions import RequestValidationError
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse

from app.config import Settings
from app.database import (
    create_database,
    get_file,
    recover_interrupted,
    storage_error,
    verify_schema,
)
from app.errors import ServiceError, error_body
from app.middleware import RequestGuard
from app.models import FeatureRecord
from app.processing import ProcessingService
from app.runtime import (
    acquire_instance_lock,
    cleanup_abandoned,
    configure_geospatial,
    verify_drivers,
)
from app.schemas import ErrorResponse, FeatureResponse, FileResponse, Measurement, MeasurementPage


class JsonFormatter(logging.Formatter):
    def format(self, record):
        data = {"level": record.levelname, "message": record.getMessage()}
        for key in ("request_id", "file_id", "stage", "duration_ms", "feature_count", "error_code"):
            if hasattr(record, key):
                data[key] = getattr(record, key)
        if record.exc_info:
            data["exception"] = self.formatException(record.exc_info)
        return json.dumps(data)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(application):
        handler = logging.StreamHandler()
        handler.setFormatter(JsonFormatter())
        logger = logging.getLogger("app")
        logger.setLevel(logging.INFO)
        logger.addHandler(handler)
        instance_lock = None
        engine = None
        try:
            instance_lock = acquire_instance_lock(settings)
            configure_geospatial()
            engine, sessions = create_database(settings)
            verify_schema(engine)
            recover_interrupted(sessions)
            cleanup_abandoned(settings)
            application.state.engine = engine
            application.state.sessions = sessions
            application.state.processor = ProcessingService(settings, sessions)
            yield
        finally:
            if engine is not None:
                engine.dispose()
            if instance_lock is not None:
                instance_lock.close()
            logger.removeHandler(handler)

    app = FastAPI(
        title="Geospatial File Measurement API",
        version="0.1.0",
        lifespan=lifespan,
        description=(
            "Upload local survey data. Measurements are projected 2D estimates "
            "in metres and square metres."
        ),
    )
    app.add_middleware(RequestGuard, settings=settings)
    responses = {status: {"model": ErrorResponse} for status in (404, 409, 413, 415, 422, 500, 503)}

    @app.exception_handler(ServiceError)
    async def service_exception(request, exc):
        headers = {"Retry-After": "5"} if exc.status == 503 else None
        return JSONResponse(
            error_body(exc, request.state.request_id), status_code=exc.status, headers=headers
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exception(request, exc):
        details = [
            {"location": list(item["loc"]), "type": item["type"], "message": item["msg"]}
            for item in exc.errors()
        ]
        return await service_exception(
            request, ServiceError("INVALID_REQUEST", "Request validation failed.", 422, details)
        )

    @app.exception_handler(HTTPException)
    async def http_exception(request, exc):
        return await service_exception(
            request, ServiceError("HTTP_ERROR", str(exc.detail), exc.status_code)
        )

    @app.exception_handler(SQLAlchemyError)
    async def database_exception(request, exc):
        error = storage_error(exc)
        if error.status == 500:
            logging.getLogger("app").exception(
                "database_programming_error", extra={"request_id": request.state.request_id}
            )
        return await service_exception(request, error)

    @app.exception_handler(Exception)
    async def internal_exception(request, exc):
        logging.getLogger("app").exception(
            "unhandled_error", extra={"request_id": request.state.request_id}
        )
        return await service_exception(
            request, ServiceError("INTERNAL_ERROR", "An unexpected error occurred.", 500)
        )

    @app.post("/api/files/", status_code=201, response_model=FileResponse, responses=responses)
    async def upload(
        request: Request,
        response: Response,
        file: Annotated[UploadFile, File(description="KML or ZIP containing one Shapefile")],
        source_crs: Annotated[
            str | None, Form(description="EPSG identifier used only when CRS metadata is missing")
        ] = None,
    ):
        form = await request.form()
        if (
            len(form.getlist("file")) != 1
            or len(form.getlist("source_crs")) > 1
            or set(form) - {"file", "source_crs"}
        ):
            raise ServiceError("INVALID_REQUEST", "Send exactly one file and optional source_crs.")
        filename = (file.filename or "").replace("\\", "/").rsplit("/", 1)[-1]
        if not filename or len(filename) > 255 or any(ord(c) < 32 for c in filename):
            raise ServiceError(
                "INVALID_FILENAME", "A valid filename of at most 255 characters is required."
            )
        record = await anyio.to_thread.run_sync(
            request.app.state.processor.process,
            file.file,
            filename,
            source_crs,
            request.state.request_id,
        )
        response.headers["Location"] = f"/api/files/{record.id}/"
        return record

    @app.get("/api/files/{file_id}/", response_model=FileResponse, responses=responses)
    def file_info(request: Request, file_id: UUID):
        return get_file(request.app.state.sessions, str(file_id))

    @app.get(
        "/api/files/{file_id}/measurements/", response_model=MeasurementPage, responses=responses
    )
    def measurements(
        request: Request,
        file_id: UUID,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
    ):
        file = get_file(request.app.state.sessions, str(file_id))
        if file.status == "PROCESSING":
            raise ServiceError(
                "RESULTS_NOT_READY", "File is still processing.", 409, file_id=file.id
            )
        if file.status == "FAILED":
            raise ServiceError(
                "PROCESSING_FAILED",
                "File processing failed.",
                409,
                details=[file.error] if file.error else [],
                file_id=file.id,
            )
        with request.app.state.sessions() as session:
            rows = session.scalars(
                select(FeatureRecord)
                .where(FeatureRecord.file_id == file.id)
                .order_by(FeatureRecord.feature_index)
                .limit(limit)
                .offset(offset)
            ).all()
            items = []
            for row in rows:
                measurement = None
                if row.measurement_status == "MEASURED":
                    measurement = Measurement(
                        kind=row.measurement_kind,
                        value=row.measurement_value,
                        unit=row.measurement_unit,
                        measurement_crs=row.measurement_crs,
                    )
                items.append(
                    FeatureResponse(
                        feature_index=row.feature_index,
                        source_feature_id=row.source_feature_id,
                        layer_index=row.layer_index,
                        layer_name=row.layer_name,
                        geometry_type=row.geometry_type,
                        geometry=row.geometry,
                        source_crs=file.crs,
                        properties=row.properties,
                        measurement_status=row.measurement_status,
                        measurement=measurement,
                        errors=row.errors,
                        warnings=row.warnings,
                    )
                )
        return MeasurementPage(
            file_id=file.id, total=file.feature_count, limit=limit, offset=offset, items=items
        )

    @app.get("/health/")
    def health():
        return {"status": "ok"}

    @app.get("/ready/", responses=responses)
    def ready(request: Request):
        try:
            verify_drivers()
            with request.app.state.engine.connect() as connection:
                connection.execute(text("SELECT 1 FROM files LIMIT 1"))
        except (RuntimeError, SQLAlchemyError) as exc:
            raise ServiceError(
                "NOT_READY", "Database or geospatial runtime is unavailable.", 503
            ) from exc
        return {"status": "ready"}

    return app


app = create_app()
