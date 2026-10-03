# Custom Caddy image for justelesRCP.
#
# The stock caddy:2-alpine ships no rate-limit module, but the exposed /api/*
# scraping endpoints need per-IP throttling (see the rate_limit blocks in
# docker/Caddyfile). So build Caddy with the caddy-ratelimit plugin via xcaddy,
# then drop the single static binary onto the SAME alpine base the stock image
# uses, so everything else (our entrypoint.sh, the wget healthcheck, the file
# layout, the NET_BIND_SERVICE file cap) is byte-for-byte the stock behaviour.
#
# The build needs network access to fetch the Go module (github.com + the Go
# module proxy), so it runs on whatever host does `docker compose up --build`.
# There is NO COPY from the build context, so the context can stay tiny (compose
# points it at this docker/ dir, not the repo root the refresh/embed images use).
#
# That network access is the fragile part, and it is why deploy.sh does NOT rebuild
# this image on an ordinary deploy (only with --rebuild-web): everything this
# container serves is a bind mount (Caddyfile, entrypoint.sh, ../dist), so the image
# is just the binary and a deploy never needs a fresh one. On a VPS whose Docker
# bridge advertises IPv6 without a route, xcaddy dies with
#   go: module github.com/mholt/caddy-ratelimit: Get "https://proxy.golang.org/...":
#   dial tcp [2a00:...]:443: connect: network is unreachable
# If you do need to rebuild there, either make the module proxy reachable (build with
# the host network: `docker compose build --no-cache web` after adding
# `network: host` under the web service's `build:`) or build the image on a machine
# with working egress and ship it (`docker save justelesrcp-web | ssh VPS 'sudo
# docker load'`).
# Both the Caddy release and the plugin are pinned, so a rebuild is reproducible and
# never picks up a new upstream builder image on its own; bump them together on
# purpose (the plugin tag list: https://proxy.golang.org/github.com/mholt/caddy-ratelimit/@v/list).
ARG CADDY_VERSION=2.11.6

FROM caddy:${CADDY_VERSION}-builder-alpine AS builder
RUN xcaddy build \
	--with github.com/mholt/caddy-ratelimit@v0.1.0

FROM caddy:${CADDY_VERSION}-alpine
# Replace the stock binary with the plugin-enabled one; same path, so the base
# image's ENTRYPOINT/CMD and our entrypoint.sh keep working unchanged.
COPY --from=builder /usr/bin/caddy /usr/bin/caddy
