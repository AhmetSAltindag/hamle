FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt
COPY core.py firestore_store.py main.py ./
USER 65532:65532
ENTRYPOINT ["python", "-B", "main.py"]
CMD ["run"]
