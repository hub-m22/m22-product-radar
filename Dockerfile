FROM python:3.12-slim
ENV PYTHONUTF8=1 PYTHONUNBUFFERED=1 RADAR_HOST=0.0.0.0 RADAR_PORT=8022 RADAR_PDF_FONT_PATH=/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf
RUN apt-get update && apt-get install -y --no-install-recommends fonts-dejavu-core && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
VOLUME ["/app/data"]
EXPOSE 8022
CMD ["python", "-m", "radar", "serve"]
