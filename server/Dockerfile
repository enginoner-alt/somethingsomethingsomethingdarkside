# API + CPU workers. The GPU (WhisperX) worker gets its own CUDA image in M2.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

RUN useradd --create-home --uid 10001 app
WORKDIR /srv

COPY pyproject.toml ./
COPY app ./app
RUN pip install .

COPY alembic.ini ./
COPY alembic ./alembic
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

USER app
EXPOSE 8000
ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["api"]
