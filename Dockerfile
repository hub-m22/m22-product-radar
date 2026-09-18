FROM python:3.12-slim
ENV PYTHONUTF8=1 PYTHONUNBUFFERED=1 RADAR_HOST=0.0.0.0 RADAR_PORT=8022 RADAR_PDF_FONT_PATH=/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf \
    TZ=Europe/Moscow RADAR_IN_DOCKER=1
# tzdata — расписание сборов считается по московскому времени; шрифт — для PDF-отчётов с кириллицей
RUN apt-get update && apt-get install -y --no-install-recommends fonts-dejavu-core tzdata && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
VOLUME ["/app/data"]
EXPOSE 8022
# Docker сам перезапускает контейнер, если /health не отвечает (см. docker-compose.yml, restart: always)
HEALTHCHECK --interval=60s --timeout=10s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8022/health', timeout=8).status == 200 else 1)"
CMD ["python", "-m", "radar", "serve"]
