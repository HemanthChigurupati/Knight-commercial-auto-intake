# One container: the FastAPI backend serving the built React app.
#   docker build -t knight-intake .
#   docker run -p 8000:8000 knight-intake                 # demo mode, no credentials
FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends tesseract-ocr \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY backend/requirements.txt backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

COPY backend backend
COPY frontend/dist frontend/dist
COPY fixtures fixtures
COPY rules rules
COPY samples samples

ENV APP_MODE=demo \
    DATA_DIR=/app/data \
    PYTHONUNBUFFERED=1
WORKDIR /app/backend
EXPOSE 8000
CMD ["sh", "-c", "uvicorn app.api.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
