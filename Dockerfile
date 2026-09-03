# Runs once per release as an Argo PreSync hook Job, then exits.
#
# Same base image and same Artifactory plumbing as keep-api-gateway, so this
# builds on the same runners with the same approvals. Everything the gateway
# needs for *serving* is dropped: no gunicorn, no Prometheus multiproc dir, no
# EXPOSE, no src/. This container opens one connection, runs DDL, and exits.
FROM 8200artifactory.d8200.mil/docker-virtual/vena-889/base-images/keep-api-backend-base:1.0.1

WORKDIR /app

# Corporate PyPI mirrors - use http:// to bypass SSL MITM proxy
ENV PIP_INDEX_URL="http://8200artifactory.d8200.mil/artifactory/api/pypi/pypi-official/simple/"
ENV PIP_TRUSTED_HOST="8200artifactory.d8200.mil"
ENV PIP_EXTRA_INDEX_URL="http://8200artifactory.d8200.mil/artifactory/api/pypi/baldar-pypi/simple/"
# Disable SSL verification for pip (corporate MITM proxy)
ENV PIP_NO_CHECK_SSL=1

# Poetry requires auth to access Artifactory PyPI.
# Passed as build args, NOT baked in as ENV: an ENV credential is readable from
# the image with `docker history` by anyone who can pull it, and lives forever in
# the git history of this file.
#   docker build \
#     --build-arg POETRY_HTTP_BASIC_PYPI_USERNAME="$ARTIFACTORY_USER" \
#     --build-arg POETRY_HTTP_BASIC_PYPI_PASSWORD="$ARTIFACTORY_TOKEN" .
ENV POETRY_REPOSITORIES_PYPI_URL="http://8200artifactory.d8200.mil/artifactory/api/pypi/pypi-official/simple/"
ARG POETRY_HTTP_BASIC_PYPI_USERNAME
ARG POETRY_HTTP_BASIC_PYPI_PASSWORD
# Disable SSL verification for httpx (used by Poetry)
ENV PYTHONHTTPSVERIFY=0
# Use /usr/local/bin/python3.11 explicitly - base image has TWO Python installs.
ENV PATH="/usr/local/bin:$PATH"

ENV PYTHONUNBUFFERED=1

# Dependencies first, so a revision-only change reuses this layer.
COPY pyproject.toml ./
# Lock is generated at build time, same as the gateway (Poetry 1.x in the base
# image regenerates from a Poetry 2.x lock). No confluent-kafka here, so none of
# the gateway's ABI-warning workaround is needed and failures propagate normally.
RUN poetry config virtualenvs.create false && \
    poetry lock --no-interaction && \
    poetry install --no-interaction --no-ansi --no-root --without dev

# The schema itself.
COPY alembic.ini /app/alembic.ini
COPY migrations /app/migrations
COPY keep_migrations /app/keep_migrations

# Installs THIS package, which is what creates the `keep-migrate` executable
# from [tool.poetry.scripts]. Must come after the COPY above; --only-root skips
# the dependencies already installed in the layer above.
RUN poetry install --no-interaction --no-ansi --only-root

# No default target: the chart passes --target explicitly so the schema version
# is declared in git rather than baked into the image.
# Equivalent if the console script is ever unavailable:
#   ENTRYPOINT ["python", "-m", "keep_migrations.cli"]
ENTRYPOINT ["keep-migrate"]
CMD ["--target", "head"]
