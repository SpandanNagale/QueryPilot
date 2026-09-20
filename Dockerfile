FROM python:3.11-slim AS base

RUN useradd --create-home --uid 10001 appuser
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --only-binary=:all: -r requirements.txt

COPY app/ ./app/
COPY scripts/ ./scripts/
COPY frontend/ ./frontend/
COPY config/ ./config/
COPY alembic.ini .
COPY alembic/ ./alembic/

RUN python -m scripts.seed_db

RUN chown -R appuser:appuser /app
USER appuser

EXPOSE 8000
ENV PORT=8000
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
