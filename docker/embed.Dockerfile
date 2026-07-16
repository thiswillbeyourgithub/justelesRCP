# Warm server-side embedder image (see embed-service.py). A small Python service
# that keeps ONE ONNX sentence encoder resident and:
#   - embeds the reader's semantic-search query on request (POST /api/sem/embed),
#     so the browser downloads NO model (the old ~120 MB per-visitor load is gone);
#   - (re-)embeds each CRAWLED page's sections in the background, writing the served
#     dist/<rcp|eu>/<slug>.vec.json sidecars (never the frozen 2022 baseline).
# Caddy reverse-proxies /api/sem/* to it; it is NOT published to the host. Kept in a
# SEPARATE container from the read-only web server, like the refresh service, and
# given only narrow writable mounts (dist/rcp, dist/eu). Build context is the repo
# root (see compose), so the root .dockerignore keeps data/dist/models out of it.
FROM python:3.12-slim

# These mirror the PEP 723 dependency header of embed-service.py, which is the source
# of truth. If you change the deps there, change them here too. onnxruntime +
# tokenizers run the int8 e5-small encoder WITHOUT torch (a ~300 MB image, not ~2 GB);
# lxml + brotli are pulled in via build.py (section_chunks parses HTML, compress()
# writes the .br sibling). The model itself is NOT baked: it is mounted read-only from
# the host ./models at runtime (download-model.sh fetches it), so it stays out of the
# image and out of the .dockerignore-excluded build context.
RUN pip install --no-cache-dir onnxruntime tokenizers numpy "lxml>=5.0" "brotli>=1.1" loguru click

WORKDIR /app
# The service imports build.py (section_chunks / quantize / overlay readers /
# compress / write_vec_json) and onnx_embed.py (the warm encoder) by path. build.py
# imports the shared bdpm.py, and renders nothing here but reads src/rcp.html lazily
# in code paths we don't hit; ship it anyway for parity/safety with the refresh
# image. Everything else (data, dist, models) is bind-mounted at runtime by compose.
COPY build.py bdpm.py onnx_embed.py embed-service.py ./
COPY src/rcp.html ./src/rcp.html

# Read-only rootfs at runtime: don't try to write .pyc; log unbuffered so lines reach
# `docker logs` immediately.
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

# Stamp the git commit this image was built from, so deploy.sh can read it back off
# the running container and confirm the VPS runs current code (same rationale as
# refresh.Dockerfile). Declared late so a SHA change only rebuilds this trivial
# metadata layer, never the pip-install / COPY layers above.
ARG GIT_SHA=unknown
LABEL git.sha=$GIT_SHA

EXPOSE 8461
# Listen on all interfaces so Caddy (same compose network) can reach it; it is NOT
# published to the host (see compose: no ports:), only reachable via the proxy.
CMD ["python", "embed-service.py", "--host", "0.0.0.0", "--port", "8461"]
