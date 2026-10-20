# syntax=docker/dockerfile:1
# Some dependencies (miniaudio, via pyatv) have no arm64 wheel: build wheels
# here with a compiler, and keep the compiler out of the final image.
FROM --platform=linux/arm64 python:3.12-slim-bookworm AS wheels
RUN apt-get update && apt-get install -y --no-install-recommends build-essential && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip wheel --no-cache-dir --wheel-dir /wheels -r requirements.txt

FROM --platform=linux/arm64 python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    MPLCONFIGDIR=/tmp/matplotlib

WORKDIR /app
COPY requirements.txt .
# /data holds the Apple TV pairing; a new volume copies this ownership
RUN --mount=type=bind,from=wheels,source=/wheels,target=/wheels \
    pip install --no-index --find-links /wheels -r requirements.txt \
 && useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin dash \
 && mkdir /data && chown dash /data && chmod 700 /data

COPY kindle_dash ./kindle_dash

USER dash
EXPOSE 8000
HEALTHCHECK --interval=60s --timeout=5s --start-period=60s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"

CMD ["python", "-m", "kindle_dash.app"]
