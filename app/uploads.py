import shutil
import stat
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO
from xml.etree.ElementTree import ParseError
from zipfile import ZIP_DEFLATED, ZIP_STORED, BadZipFile, ZipFile

from defusedxml.common import DefusedXmlException
from defusedxml.ElementTree import iterparse

from app.config import Settings
from app.errors import ServiceError, issue


@dataclass
class Dataset:
    path: Path
    format: str
    warnings: list[dict]
    placemark_count: int | None = None


def check_deadline(deadline: float):
    if time.monotonic() > deadline:
        raise ServiceError(
            "PROCESSING_TIMEOUT", "Processing exceeded its cooperative deadline.", 503
        )


def stage_upload(
    stream: BinaryIO, filename: str, directory: Path, settings: Settings, deadline: float
) -> Dataset:
    suffix = Path(filename).suffix.lower()
    if suffix not in {".zip", ".kml"}:
        raise ServiceError("UNSUPPORTED_FILE_TYPE", "Upload a .kml or a zipped Shapefile.", 415)
    path = directory / f"upload{suffix}"
    count = 0
    with path.open("wb") as output:
        while chunk := stream.read(64 * 1024):
            check_deadline(deadline)
            count += len(chunk)
            if count > settings.max_file_bytes:
                raise ServiceError("FILE_TOO_LARGE", "Uploaded file exceeds its byte limit.", 413)
            output.write(chunk)
    if count == 0:
        raise ServiceError("EMPTY_FILE", "The uploaded file is empty.")
    if suffix == ".zip":
        return Dataset(extract_shapefile(path, directory, settings, deadline), "SHAPEFILE", [])
    warnings, placemarks = validate_kml(path, deadline)
    return Dataset(path, "KML", warnings, placemarks)


def extract_shapefile(path: Path, directory: Path, settings: Settings, deadline: float) -> Path:
    allowed = {".shp", ".shx", ".dbf", ".prj", ".cpg"}
    try:
        with ZipFile(path) as archive:
            members = archive.infolist()
            if len(members) > settings.max_zip_members:
                raise ServiceError("ARCHIVE_LIMIT", "Too many ZIP members.", 413)
            seen = set()
            files = {}
            declared = 0
            for member in members:
                name = member.filename.replace("\\", "/")
                entry = PurePosixPath(name)
                if (
                    entry.is_absolute()
                    or ".." in entry.parts
                    or ":" in name
                    or "\x00" in name
                    or not name
                ):
                    raise ServiceError("UNSAFE_ARCHIVE", "The archive contains an unsafe path.")
                key = str(entry).casefold()
                if key in seen:
                    raise ServiceError(
                        "AMBIGUOUS_ARCHIVE", "Duplicate archive paths are not allowed."
                    )
                seen.add(key)
                mode = member.external_attr >> 16
                kind = stat.S_IFMT(mode)
                if kind not in (0, stat.S_IFREG, stat.S_IFDIR):
                    raise ServiceError(
                        "UNSAFE_ARCHIVE", "Only regular files and directories are allowed."
                    )
                if member.flag_bits & 1 or member.compress_type not in (ZIP_STORED, ZIP_DEFLATED):
                    raise ServiceError(
                        "UNSUPPORTED_ARCHIVE", "Encrypted or unsupported ZIP entries."
                    )
                declared += member.file_size
                if declared > settings.max_expanded_bytes:
                    raise ServiceError("ARCHIVE_LIMIT", "Expanded ZIP exceeds its byte limit.", 413)
                if not member.is_dir():
                    files[key] = member
            shapes = [key for key in files if key.endswith(".shp")]
            if len(shapes) != 1:
                raise ServiceError("SHAPEFILE_COUNT", "The ZIP must contain exactly one Shapefile.")
            stem = shapes[0][:-4]
            if not all(stem + ext in files for ext in (".shp", ".shx", ".dbf")):
                raise ServiceError(
                    "SHAPEFILE_COMPONENTS_MISSING",
                    "Matching .shp, .shx and .dbf files are required.",
                )
            expanded = 0
            for extension in sorted(allowed):
                member = files.get(stem + extension)
                if member is None:
                    continue
                # Generated paths avoid trusting the archive's directory structure.
                target = directory / ("dataset" + extension)
                with archive.open(member) as source, target.open("wb") as output:
                    while chunk := source.read(64 * 1024):
                        check_deadline(deadline)
                        expanded += len(chunk)
                        if expanded > settings.max_expanded_bytes:
                            raise ServiceError(
                                "ARCHIVE_LIMIT", "Expanded ZIP exceeds its byte limit.", 413
                            )
                        output.write(chunk)
    except (BadZipFile, EOFError, RuntimeError, NotImplementedError) as exc:
        raise ServiceError("INVALID_ARCHIVE", "The ZIP cannot be read completely.") from exc
    return directory / "dataset.shp"


def validate_kml(path: Path, deadline: float) -> tuple[list[dict], int]:
    namespaces = {
        "http://www.opengis.net/kml/2.2",
        "http://earth.google.com/kml/2.0",
        "http://earth.google.com/kml/2.1",
        "http://earth.google.com/kml/2.2",
    }
    presentation = set()
    placemarks = 0
    depth = 0
    try:
        with path.open("rb") as stream:
            for event, element in iterparse(
                stream,
                events=("start", "end"),
                forbid_dtd=True,
                forbid_entities=True,
                forbid_external=True,
            ):
                check_deadline(deadline)
                tag = element.tag.split("}")[-1]
                if event == "start":
                    if depth == 0 and (
                        tag != "kml"
                        or not element.tag.startswith("{")
                        or element.tag[1:].split("}")[0] not in namespaces
                    ):
                        raise ServiceError("INVALID_KML", "Expected a KML root and namespace.")
                    depth += 1
                    if depth > 64:
                        raise ServiceError(
                            "XML_DEPTH_LIMIT", "KML nesting exceeds the supported limit.", 413
                        )
                    if tag == "NetworkLink":
                        raise ServiceError(
                            "EXTERNAL_LINK_UNSUPPORTED", "KML NetworkLink is not supported."
                        )
                    if tag == "Placemark":
                        placemarks += 1
                    if tag in {
                        "Style",
                        "StyleMap",
                        "GroundOverlay",
                        "ScreenOverlay",
                        "PhotoOverlay",
                    }:
                        presentation.add(tag)
                else:
                    depth -= 1
                    element.clear()
    except (ParseError, DefusedXmlException) as exc:
        raise ServiceError(
            "INVALID_KML", "KML is malformed or contains forbidden XML declarations."
        ) from exc
    warnings = (
        [
            issue(
                "KML_PRESENTATION_IGNORED",
                f"Presentation elements ignored: {', '.join(sorted(presentation))}",
            )
        ]
        if presentation
        else []
    )
    return warnings, placemarks


def cleanup_directory(directory: Path):
    if directory.is_symlink():
        directory.unlink()
    elif directory.exists():
        shutil.rmtree(directory)
