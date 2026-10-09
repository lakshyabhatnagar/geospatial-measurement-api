from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from scripts.generate_samples import build_samples


@pytest.fixture(scope="session")
def samples(tmp_path_factory):
    directory = tmp_path_factory.mktemp("samples")
    build_samples(directory)
    return directory


@pytest.fixture
def settings(tmp_path, monkeypatch):
    directory = tmp_path / "data"
    monkeypatch.setenv("GEO_DATA_DIR", str(directory))
    config = Config("alembic.ini")
    command.upgrade(config, "head")
    return Settings(data_dir=directory)


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings)) as instance:
        yield instance


def upload(client, path: Path, **data):
    with path.open("rb") as stream:
        return client.post("/api/files/", files={"file": (path.name, stream)}, data=data)
