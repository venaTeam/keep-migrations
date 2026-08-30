# Runs once per release as an Argo PreSync hook Job, then exits.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    POETRY_VIRTUALENVS_CREATE=false

WORKDIR /app

RUN pip install --no-cache-dir poetry==1.8.3

COPY pyproject.toml ./
RUN poetry install --no-interaction --no-root --without dev

COPY alembic.ini ./
COPY migrations ./migrations
COPY keep_migrations ./keep_migrations

RUN poetry install --no-interaction --only-root

# No default target: the chart passes --target explicitly so the schema version
# is declared in git rather than baked into the image.
ENTRYPOINT ["keep-migrate"]
CMD ["--target", "head"]
