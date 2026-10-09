import fcntl
import logging
import os

import pyogrio
import pyproj

from app.uploads import cleanup_directory

logger = logging.getLogger(__name__)


def verify_drivers():
    drivers = pyogrio.list_drivers(read=True)
    missing = {"ESRI Shapefile", "LIBKML"} - drivers.keys()
    if missing:
        raise RuntimeError(f"Required GDAL readers unavailable: {', '.join(sorted(missing))}")
    if tuple(int(v) for v in pyproj.proj_version_str.split(".")[:2]) < (9, 2):
        raise RuntimeError("PROJ >= 9.2 is required for strict transformations")


def configure_geospatial():
    verify_drivers()
    os.environ["PROJ_NETWORK"] = "OFF"
    pyproj.network.set_network_enabled(False)
    disabled = " ".join(
        name for name in pyogrio.list_drivers() if name not in {"ESRI Shapefile", "LIBKML"}
    )
    pyogrio.set_gdal_config_options(
        {
            "GDAL_SKIP": disabled,
            "LIBKML_EXTERNAL_STYLE": "NO",
            "LIBKML_RESOLVE_STYLE": "NO",
            "LIBKML_READ_GROUND_OVERLAY": "FALSE",
            "PROJ_NETWORK": "OFF",
        }
    )


def acquire_instance_lock(settings):
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    lock = (settings.data_dir / "service.lock").open("a")
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock.close()
        raise RuntimeError(
            "Another API process owns this database; run exactly one worker."
        ) from None
    return lock


def cleanup_abandoned(settings):
    settings.uploads_dir.mkdir(parents=True, exist_ok=True)
    for directory in settings.uploads_dir.glob("upload-*"):
        try:
            cleanup_directory(directory)
        except OSError:
            logger.exception("cleanup_failed", extra={"stage": "startup_cleanup"})
