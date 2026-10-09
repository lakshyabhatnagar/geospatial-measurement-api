import json

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, event, select, text, update
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.errors import ServiceError, issue
from app.models import FileRecord, now


def create_database(settings: Settings):
    engine = create_engine(
        settings.database_url,
        connect_args={"check_same_thread": False},
        json_serializer=lambda obj: json.dumps(obj, allow_nan=False),
    )

    @event.listens_for(engine, "connect")
    def configure(connection, _):
        connection.autocommit = True
        cursor = connection.cursor()
        for pragma in (
            "foreign_keys=ON",
            "busy_timeout=5000",
            "journal_mode=WAL",
            "synchronous=FULL",
        ):
            cursor.execute(f"PRAGMA {pragma}")
        cursor.close()
        connection.autocommit = False

    return engine, sessionmaker(engine, expire_on_commit=False)


def verify_schema(engine):
    config = Config("alembic.ini")
    head = ScriptDirectory.from_config(config).get_current_head()
    with engine.connect() as connection:
        current = MigrationContext.configure(connection).get_current_revision()
        if current != head or current is None:
            raise RuntimeError("Database schema is not current. Run: alembic upgrade head")
        connection.execute(text("SELECT 1 FROM files LIMIT 1"))


def recover_interrupted(sessions):
    with sessions.begin() as session:
        session.execute(
            update(FileRecord)
            .where(FileRecord.status == "PROCESSING")
            .values(
                status="FAILED",
                completed_at=now(),
                error=issue(
                    "PROCESS_INTERRUPTED", "Processing was interrupted; upload the file again."
                ),
            )
        )


def get_file(sessions, file_id: str):
    with sessions() as session:
        record = session.scalar(select(FileRecord).where(FileRecord.id == file_id))
        if record is None:
            raise ServiceError("FILE_NOT_FOUND", "The file does not exist.", 404)
        return record


def storage_error(error: SQLAlchemyError) -> ServiceError:
    if isinstance(error, OperationalError):
        if "locked" in str(error).lower():
            return ServiceError("DATABASE_BUSY", "The database is busy; retry shortly.", 503)
        return ServiceError("STORAGE_UNAVAILABLE", "The database is unavailable.", 503)
    return ServiceError("INTERNAL_ERROR", "An unexpected database error occurred.", 500)
