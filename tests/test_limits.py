import asyncio
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.main import create_app
from app.middleware import RequestGuard
from tests.conftest import upload


def test_actual_request_bytes_without_content_length(settings):
    settings.max_request_bytes = 10
    called = False
    messages = []

    async def downstream(scope, receive, send):
        nonlocal called
        called = True

    chunks = iter(
        [
            {"type": "http.request", "body": b"123456", "more_body": True},
            {"type": "http.request", "body": b"789012", "more_body": False},
        ]
    )

    async def receive():
        return next(chunks)

    async def send(message):
        messages.append(message)

    scope = {"type": "http", "method": "POST", "path": "/api/files/", "headers": []}
    asyncio.run(RequestGuard(downstream, settings)(scope, receive, send))
    assert not called
    assert messages[0]["status"] == 413
    assert json.loads(messages[1]["body"])["error"]["code"] == "REQUEST_TOO_LARGE"


@pytest.mark.parametrize(
    "field,value,code",
    [
        ("max_features", 1, "FEATURE_LIMIT"),
        ("max_coordinates", 5, "COORDINATE_LIMIT"),
        ("max_file_bytes", 100, "FILE_TOO_LARGE"),
        ("max_expanded_bytes", 100, "ARCHIVE_LIMIT"),
        ("max_zip_members", 1, "ARCHIVE_LIMIT"),
    ],
)
def test_upload_limits(settings, samples, field, value, code):
    setattr(settings, field, value)
    with TestClient(create_app(settings)) as client:
        response = upload(client, samples / "projected_polygons.zip")
        assert response.status_code == 413
        assert response.json()["error"]["code"] == code
        with client.app.state.engine.connect() as connection:
            assert connection.scalar(text("SELECT count(*) FROM features")) == 0
        assert not list(settings.uploads_dir.iterdir())


def test_feature_limit_partial_success(settings, samples):
    settings.max_feature_coordinates = 5
    with TestClient(create_app(settings)) as client:
        response = upload(client, samples / "projected_polygons.zip")
        assert response.status_code == 201
        assert response.json()["status"] == "COMPLETED_WITH_ERRORS"
        assert response.json()["measured_count"] == 1
        assert response.json()["error_count"] == 2


def test_cleanup_failure_does_not_reverse_success(client, samples, monkeypatch):
    def fail_cleanup(directory):
        raise OSError("simulated cleanup failure")

    monkeypatch.setattr("app.processing.cleanup_directory", fail_cleanup)
    response = upload(client, samples / "projected_lines.zip")
    assert response.status_code == 201
    assert client.get(response.headers["location"]).json()["status"] == "COMPLETED"


def test_reader_failure_discards_results(client, samples, monkeypatch):
    from app.errors import ServiceError
    from app.processing import read_layers

    def broken(*args):
        yield from read_layers(*args)
        raise ServiceError("DATASET_READ_FAILED", "simulated failure")

    monkeypatch.setattr("app.processing.read_layers", broken)
    response = upload(client, samples / "projected_polygons.zip")
    assert response.status_code == 422
    file_id = response.json()["error"]["file_id"]
    assert client.get(f"/api/files/{file_id}/").json()["status"] == "FAILED"
    with client.app.state.engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM features")) == 0


def test_sqlite_pragmas(client):
    with client.app.state.engine.connect() as connection:
        for name, value in [
            ("journal_mode", "wal"),
            ("foreign_keys", 1),
            ("synchronous", 2),
            ("busy_timeout", 5000),
        ]:
            assert connection.scalar(text(f"PRAGMA {name}")) == value


def test_missing_driver_startup(settings, monkeypatch):
    monkeypatch.setattr("app.runtime.pyogrio.list_drivers", lambda **kwargs: {})
    with pytest.raises(RuntimeError, match="Required GDAL readers unavailable"):
        with TestClient(create_app(settings)):
            pass


def test_cooperative_deadline(settings, samples):
    settings.processing_seconds = 0.000000001
    with TestClient(create_app(settings)) as client:
        response = upload(client, samples / "survey.kml")
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "PROCESSING_TIMEOUT"
        assert not list(settings.uploads_dir.iterdir())


def test_database_error_mapping():
    from sqlalchemy.exc import OperationalError

    from app.database import storage_error

    assert (
        storage_error(OperationalError("query", {}, Exception("database is locked"))).code
        == "DATABASE_BUSY"
    )
    assert (
        storage_error(OperationalError("query", {}, Exception("disk I/O error"))).code
        == "STORAGE_UNAVAILABLE"
    )
