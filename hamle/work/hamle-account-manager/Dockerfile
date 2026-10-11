FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    GUNICORN_CMD_ARGS="--workers 1 --threads 4 --timeout 60 --graceful-timeout 15 --max-requests 1000 --max-requests-jitter 100 --limit-request-line 4094 --limit-request-fields 50 --limit-request-field_size 16384"

WORKDIR /app
RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-tur fonts-dejavu-core ffmpeg \
    && rm -rf /var/lib/apt/lists/*
COPY requirements-cloud.txt /app/requirements-cloud.txt
RUN python -m pip install --no-cache-dir --only-binary=:all: -r /app/requirements-cloud.txt \
    && python -m pip check

# The context is created from a strict file allowlist by package_cloud_source.py.
# Preserve helper sibling paths: cloud_providers resolves these from __file__.
COPY work/ /app/work/
COPY SOURCE_MANIFEST.json /app/SOURCE_MANIFEST.json
RUN cd /app/work/hamle-account-manager && python internal_ocr_smoke.py
RUN cd /app/work/hamle-account-manager && python wat_media_smoke.py
RUN chmod -R a-w /app
USER 65532:65532
WORKDIR /app/work/hamle-account-manager

# Safe default: help only, no credential/provider access. Jobs explicitly supply
# python worker.py --mode verify|review. Control overrides to gunicorn with
# --bind :$PORT cloud_control:create_app(); PORT expansion belongs to deployment.
CMD ["python", "worker.py", "--help"]
