# Production image: Python + Stockfish. The same image runs three services (see deploy/docker-compose.yml):
#   web        uvicorn app.main:app        (JOB_MODE=external: it only queues engine work)
#   worker     python -m app.worker        (runs imports, Prep Checks and previews)
#   scheduler  python -m app.scheduler     (daily backup, weekly restore test, weekly email)
FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends stockfish \
 && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    STOCKFISH_PATH=/usr/games/stockfish \
    DB_PATH=/data/plateau.db \
    COOKIE_SECURE=1

WORKDIR /app
COPY requirements.txt requirements-prod.txt ./
RUN pip install --no-cache-dir -r requirements-prod.txt
COPY app app
COPY sample_data sample_data
COPY scripts scripts

RUN useradd --create-home appuser && mkdir -p /data && chown appuser /data
USER appuser
VOLUME /data
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4).status == 200 else 1)" || exit 1
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
