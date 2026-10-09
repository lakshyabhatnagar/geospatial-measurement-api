import io
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

import pytest
import shapely
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select

from app.main import create_app
from app.models import FeatureRecord, FileRecord
from tests.conftest import upload


def test_projected_measurements_and_pagination(client, samples):
    response = upload(client, samples / "projected_polygons.zip")
    assert response.status_code == 201, response.text
    data = response.json()
    assert data["status"] == "COMPLETED"
    assert data["feature_count"] == data["measured_count"] == 3
    assert data["crs"] == "EPSG:32643"
    assert data["layers"][0]["fields"][0]["name"] == "name"
    page = client.get(response.headers["location"] + "measurements/").json()
    assert [f["measurement"]["value"] for f in page["items"]] == pytest.approx(
        [10000, 9600, 20000], abs=1e-5
    )
    assert page["items"][0]["geometry_crs"] == "OGC:CRS84"
    x, y = page["items"][0]["geometry"]["coordinates"][0][0]
    assert x == pytest.approx(75)
    assert 18 < y < 19
    assert page["items"][0]["properties"]["parcel_id"] == 1
    selected = client.get(response.headers["location"] + "measurements/?limit=1&offset=1").json()
    assert selected["total"] == 3
    assert selected["items"][0]["feature_index"] == 1
    assert client.get(response.headers["location"] + "measurements/?limit=201").status_code == 422


def test_line_and_geographic_equivalence(client, samples):
    for filename, expected in [("projected_lines.zip", 500), ("geographic_polygons.zip", 10000)]:
        response = upload(client, samples / filename)
        assert response.status_code == 201, response.text
        item = client.get(response.headers["location"] + "measurements/").json()["items"][0]
        assert item["measurement"]["value"] == pytest.approx(expected, abs=1e-5)


def test_kml_all_layers_attributes_z_preserved(client, samples):
    response = upload(client, samples / "survey.kml")
    assert response.status_code == 201, response.text
    data = response.json()
    assert data["feature_count"] == 3
    assert data["measured_count"] == 2
    assert data["not_applicable_count"] == 1
    page = client.get(response.headers["location"] + "measurements/").json()
    assert len({f["layer_name"] for f in page["items"]}) == 2
    point = next(f for f in page["items"] if f["geometry_type"] == "Point")
    assert point["measurement"] is None
    assert len(point["geometry"]["coordinates"]) == 2
    with client.app.state.sessions() as session:
        row = session.get(FeatureRecord, (data["id"], point["feature_index"]))
        assert shapely.has_z(shapely.from_wkb(row.original_wkb))


def test_source_crs_resolution(client, samples):
    response = upload(client, samples / "missing_crs.zip")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "CRS_REQUIRED"
    file_id = response.json()["error"]["file_id"]
    assert client.get(f"/api/files/{file_id}/").json()["status"] == "FAILED"
    assert client.get(f"/api/files/{file_id}/measurements/").status_code == 409
    response = upload(client, samples / "missing_crs.zip", source_crs="EPSG:4326")
    assert response.status_code == 201, response.text
    assert response.json()["crs_origin"] == "USER_SUPPLIED"
    response = upload(client, samples / "projected_lines.zip", source_crs="EPSG:4326")
    assert response.json()["error"]["code"] == "CRS_CONFLICT"
    response = upload(client, samples / "missing_crs.zip", source_crs="guess")
    assert response.json()["error"]["code"] == "INVALID_SOURCE_CRS"


def archive_bytes(entries):
    buffer = io.BytesIO()
    with ZipFile(buffer, "w", ZIP_DEFLATED) as archive:
        for name, data in entries:
            archive.writestr(name, data)
    return buffer.getvalue()


@pytest.mark.parametrize(
    "entries,code",
    [
        ([("../x.shp", b"bad")], "UNSAFE_ARCHIVE"),
        ([("/x.shp", b"bad")], "UNSAFE_ARCHIVE"),
        ([("A.shp", b"a"), ("a.SHP", b"b")], "AMBIGUOUS_ARCHIVE"),
        ([("x.shp", b"x")], "SHAPEFILE_COMPONENTS_MISSING"),
        ([("x.shp", b"x"), ("y.shp", b"y")], "SHAPEFILE_COUNT"),
    ],
)
def test_archive_rejections(client, entries, code):
    response = client.post("/api/files/", files={"file": ("bad.zip", archive_bytes(entries))})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == code


def test_symlink_archive_rejected(client):
    member = ZipInfo("evil.shp")
    member.create_system = 3
    member.external_attr = 0o120777 << 16
    response = client.post(
        "/api/files/", files={"file": ("bad.zip", archive_bytes([(member, b"/etc/passwd")]))}
    )
    assert response.json()["error"]["code"] == "UNSAFE_ARCHIVE"


@pytest.mark.parametrize(
    "data,code",
    [
        (b"not XML", "INVALID_KML"),
        (
            b'<kml xmlns="http://www.opengis.net/kml/2.2"><NetworkLink/></kml>',
            "EXTERNAL_LINK_UNSUPPORTED",
        ),
        (
            b'<!DOCTYPE kml [<!ENTITY a "bad">]><kml xmlns="http://www.opengis.net/kml/2.2"/>',
            "INVALID_KML",
        ),
        (b'<kml xmlns="http://www.opengis.net/kml/2.2"><Document/></kml>', "EMPTY_DATASET"),
    ],
)
def test_kml_validation(client, data, code):
    response = client.post("/api/files/", files={"file": ("bad.kml", data)})
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == code


def test_error_contract_health_and_not_found(client):
    assert client.get("/health/").status_code == client.get("/ready/").status_code == 200
    for response in (
        client.get(f"/api/files/{uuid.uuid4()}/"),
        client.get("/api/files/not-a-uuid/"),
        client.post("/api/files/"),
        client.get("/missing"),
    ):
        assert "request_id" in response.json()["error"]
        assert response.headers["x-request-id"] == response.json()["error"]["request_id"]
    assert client.post("/api/files/", files={"file": ("x.csv", b"x")}).status_code == 415


def test_restart_persistence_and_interruption_recovery(settings, samples):
    with TestClient(create_app(settings)) as client:
        response = upload(client, samples / "projected_lines.zip")
        location = response.headers["location"]
        interrupted = str(uuid.uuid4())
        with client.app.state.sessions.begin() as session:
            session.add(FileRecord(id=interrupted, filename="x.kml", format="KML"))
        directory = settings.uploads_dir / "upload-abandoned"
        directory.mkdir()
    with TestClient(create_app(settings)) as client:
        assert client.get(location).json()["status"] == "COMPLETED"
        assert (
            client.get(f"/api/files/{interrupted}/").json()["error"]["code"]
            == "PROCESS_INTERRUPTED"
        )
        assert not directory.exists()


def test_commit_failure_rolls_back(client, samples):
    engine = client.app.state.engine

    def fail_insert(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO features"):
            raise RuntimeError("simulated insert failure")

    event.listen(engine, "before_cursor_execute", fail_insert)
    try:
        response = upload(client, samples / "projected_polygons.zip")
    finally:
        event.remove(engine, "before_cursor_execute", fail_insert)
    assert response.status_code == 500
    file_id = response.json()["error"]["file_id"]
    assert client.get(f"/api/files/{file_id}/").json()["status"] == "FAILED"
    with client.app.state.sessions() as session:
        assert session.scalar(select(func.count()).select_from(FeatureRecord)) == 0


def test_second_upload_busy_and_get_available(client, samples, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    original = client.app.state.processor.process

    def slow(*args):
        entered.set()
        assert release.wait(10)
        return original(*args)

    monkeypatch.setattr(client.app.state.processor, "process", slow)
    with ThreadPoolExecutor() as executor:
        task = executor.submit(upload, client, samples / "survey.kml")
        try:
            assert entered.wait(5)
            response = upload(client, samples / "survey.kml")
            assert response.status_code == 503
            assert response.json()["error"]["code"] == "SERVICE_BUSY"
            assert client.get("/health/").status_code == 200
        finally:
            release.set()
        assert task.result().status_code == 201


def test_single_instance(settings):
    with TestClient(create_app(settings)), pytest.raises(RuntimeError, match="Another API"):
        with TestClient(create_app(settings)):
            pass


def test_unreadable_prj_fallback(client, samples):
    with ZipFile(samples / "projected_lines.zip") as archive:
        entries = [
            (name, b"invalid WKT" if name.endswith(".prj") else archive.read(name))
            for name in archive.namelist()
        ]
    response = client.post(
        "/api/files/",
        files={"file": ("broken.zip", archive_bytes(entries))},
        data={"source_crs": "EPSG:32643"},
    )
    assert response.status_code == 201, response.text
    assert response.json()["crs_origin"] == "USER_SUPPLIED"


def test_points_only_completed(client):
    data = (
        b'<kml xmlns="http://www.opengis.net/kml/2.2"><Document><Placemark>'
        b"<Point><coordinates>75,18</coordinates></Point></Placemark></Document></kml>"
    )
    response = client.post("/api/files/", files={"file": ("point.kml", data)})
    assert response.status_code == 201
    assert response.json()["status"] == "COMPLETED"
    assert response.json()["not_applicable_count"] == 1


def test_all_invalid_completed_with_errors(client, tmp_path):
    from shapely.geometry import Polygon

    from scripts.generate_samples import shape_zip

    path = tmp_path / "invalid.zip"
    shape_zip(
        path,
        [
            Polygon(
                [
                    (500000, 2000000),
                    (500100, 2000100),
                    (500000, 2000100),
                    (500100, 2000000),
                    (500000, 2000000),
                ]
            )
        ],
    )
    response = upload(client, path)
    assert response.status_code == 201
    assert response.json()["status"] == "COMPLETED_WITH_ERRORS"
    assert response.json()["error_count"] == 1
