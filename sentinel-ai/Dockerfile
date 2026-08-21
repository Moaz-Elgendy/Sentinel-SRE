FROM python:3.12-slim AS base

# Prevent Python from writing .pyc files and buffering stdout (so logs
# reach Loki/`docker logs` immediately).
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# NOTE: unlike citizen-service we install NO compilers and NO psql headers.
# Sentinel talks to Prometheus/Loki/Kubernetes over HTTP and stores its own
# state in SQLite (stdlib), so every dependency here ships a pure-python or
# manylinux wheel. Keeping gcc out of the image also means the container has
# no toolchain for an attacker (or a confused LLM) to build anything with.
#
# Deliberately absent from this image: kubectl, curl, bash scripts. The only
# path to the Kubernetes API is the `kubernetes` python client used by
# app/lifecycle/remediation.py.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# SQLite incident history lives here. Mount a PVC (or at minimum an
# emptyDir) at /data; without a volume the incident history is lost on pod
# restart, which degrades the LEARNING phase but does not break anything.
RUN mkdir -p /data && chown -R 1000:1000 /data

# Run as a non-root user.
RUN useradd --create-home --uid 1000 appuser
USER appuser

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/healthz')" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
