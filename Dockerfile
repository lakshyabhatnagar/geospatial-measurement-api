FROM ghcr.io/astral-sh/uv:0.12.23@sha256:61d393e44e249f2e4b526b6c7ddcecce245946826e608e11c93ad4f5bba55b21 AS uv
FROM python:3.12.15-slim-bookworm@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PROJ_NETWORK=OFF \
    GEO_DATA_DIR=/app/data PATH=/app/.venv/bin:$PATH
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY app ./app
COPY migrations ./migrations
COPY alembic.ini ./
RUN uv sync --frozen --no-dev && \
    .venv/bin/python -c "from app.runtime import verify_drivers; verify_drivers()" && \
    useradd --uid 10001 --create-home geo && mkdir -p /app/data && chown geo:geo /app/data
USER geo
EXPOSE 10000
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s \
    CMD-SHELL python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.getenv('PORT','10000')+'/ready/',timeout=3)"
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port \"${PORT:-10000}\" --workers 1"]
