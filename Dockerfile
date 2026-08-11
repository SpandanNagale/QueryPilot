FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY scripts/ ./scripts/
COPY frontend/ ./frontend/

EXPOSE 8000

CMD ["sh", "-c", "test -f data/querypilot.db || python -m scripts.seed_db; uvicorn app.main:app --host 0.0.0.0 --port 8000"]
