from alembic import context

from app.config import Settings
from app.database import create_database
from app.models import Base

settings = Settings()
settings.data_dir.mkdir(parents=True, exist_ok=True)

if context.is_offline_mode():
    context.configure(
        url=settings.database_url,
        target_metadata=Base.metadata,
        literal_binds=True,
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()
else:
    engine, _ = create_database(settings)
    with engine.connect() as connection:
        context.configure(
            connection=connection, target_metadata=Base.metadata, render_as_batch=True
        )
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()
