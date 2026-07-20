# Companion refresh service image (see refresh-service.py). A tiny Python service
# that the site's "Rafraichir maintenant" button and >1-year auto-refresh call
# through Caddy (POST /api/refresh/<cis>). It is kept in a SEPARATE container from
# the read-only Caddy web server so the public server never needs write access:
# this one gets only narrow writable mounts (data/rcp, dist/rcp, the scrape
# manifest) and nothing else. Build context is the repo root (see compose).
FROM python:3.12-slim

# These mirror the PEP 723 dependency header of refresh-service.py, which is the
# source of truth. If you change the deps there, change them here too. Installing
# at build time means the read-only runtime container needs no package cache and
# no network just to import them. pymupdf (fitz) is for the EMA /eu/ lane: the
# service imports ema_pdf.py, which converts EMA product-information PDFs to HTML.
RUN pip install --no-cache-dir httpx lxml brotli loguru click "pymupdf>=1.24"

WORKDIR /app
# The service imports build.py, scrape-rcp.py and (for the EMA /eu/ lane)
# scrape-ema.py + ema_pdf.py by path, and renders pages from the RCP template, so
# all of those plus src/rcp.html must be in the image. build.py and scrape-rcp.py
# both import the shared bdpm.py helper module, so it ships too. These sources live
# in src/ in the repo; we COPY them into ./src/ so the container mirrors the repo
# layout (build.py's ROOT = its parent's parent = /app, with data/ + dist/ mounted at
# /app/data + /app/dist). Everything else (data, dist) is bind-mounted by compose.
COPY src/build.py src/scrape-rcp.py src/scrape-ema.py src/ema_pdf.py src/refresh-service.py src/bdpm.py ./src/
COPY src/rcp.html ./src/rcp.html

# Read-only rootfs at runtime: don't try to write .pyc; log unbuffered so lines
# reach `docker logs` immediately.
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

# Stamp the git commit this image was built from, so deploy.sh can read it back
# off the running container and confirm the VPS is running current code. This
# guards against a stale/mismatched build, e.g. one baked from a mid-Syncthing
# working tree (a new refresh-service.py alongside an old scrape-rcp.py). Declared
# this late on purpose: when the SHA changes it only rebuilds this trivial
# metadata layer, never the pip-install / COPY layers above. compose passes it via
# build.args (deploy.sh -> GIT_SHA); a plain build with no SHA in the environment
# falls back to "unknown" (the label is purely informational).
ARG GIT_SHA=unknown
LABEL git.sha=$GIT_SHA

EXPOSE 8460
# Listen on all interfaces so Caddy (same compose network) can reach it; it is
# NOT published to the host (see compose: no ports:), only reachable via the proxy.
CMD ["python", "src/refresh-service.py", "--host", "0.0.0.0", "--port", "8460"]
