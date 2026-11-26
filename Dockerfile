FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 GHOSTFORGE_MODE=production
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends libglib2.0-0 fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*
COPY requirements-local.txt .
COPY ghostreel/requirements.txt ghostreel/requirements.txt
COPY videoeditor/requirements.txt videoeditor/requirements.txt
RUN pip install --no-cache-dir -r requirements-local.txt
COPY local.py cloud_*.py ./
COPY ghostreel/ ghostreel/
COPY videoeditor/ videoeditor/
RUN useradd -r -u 10001 -m forge && mkdir -p /data/cloud /data/input /data/output /data/downloads \
    && chown -R forge:forge /data /app
USER forge
EXPOSE 8782
CMD ["uvicorn", "local:app", "--host", "0.0.0.0", "--port", "8782"]
