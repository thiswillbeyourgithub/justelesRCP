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
# no network just to import them.
RUN pip install --no-cache-dir httpx lxml brotli loguru click

WORKDIR /app
# The service imports build.py and scrape-rcp.py by path and renders pages from
# the RCP template, so all three scripts plus src/rcp.html must be in the image.
# Everything else (data, dist) is bind-mounted at runtime by compose.
COPY build.py scrape-rcp.py refresh-service.py ./
COPY src/rcp.html ./src/rcp.html

# Read-only rootfs at runtime: don't try to write .pyc; log unbuffered so lines
# reach `docker logs` immediately.
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

EXPOSE 8460
# Listen on all interfaces so Caddy (same compose network) can reach it; it is
# NOT published to the host (see compose: no ports:), only reachable via the proxy.
CMD ["python", "refresh-service.py", "--host", "0.0.0.0", "--port", "8460"]
