FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt requirements-shazam.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY playlist_extractor/ ./playlist_extractor/
COPY scan_streams.py session_report.py recognition_cache.py ./
ENTRYPOINT ["python", "-m", "playlist_extractor"]
CMD ["--help"]
