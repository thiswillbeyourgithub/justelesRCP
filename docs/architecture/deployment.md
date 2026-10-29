# Deployment and hardening

Moved verbatim out of `CLAUDE.md` (with Claude Code) to keep that file short. Read this before touching this area, and keep it up to date when you change it.

- Caddy listens on **:8459 plain HTTP**; TLS is expected to be terminated by an
  upstream reverse proxy you already run. There is no ACME/TLS config here.
- **The exposed `/api/*` scraping endpoints are rate-limited at the edge.** The
  `web` image is NOT the stock `caddy:2-alpine`: it is a custom xcaddy build
  (`docker/web.Dockerfile`, compose `build: context: .`) that bakes in the
  `caddy-ratelimit` plugin (the stock image has no rate-limit module), so the
  Caddyfile can `rate_limit` `/api/*` and `/api/sem/*` **per client IP** (returns
  429 over the limit). Because requests arrive via your upstream TLS proxy, a
  global-options `servers { trusted_proxies static {$TRUSTED_PROXIES:private_ranges} }`
  is what lets `{http.request.client_ip}` (the rate-limit key) resolve to the real
  visitor instead of the proxy IP; set `TRUSTED_PROXIES` if your proxy is on a
  public IP. The limits are tunable via `API_RATE_EVENTS`/`API_RATE_WINDOW` and
  `SEM_RATE_EVENTS`/`SEM_RATE_WINDOW` (env, read by Caddy). The `@br_fallback` route (a non-brotli client asking for a br-only `/rcp/`/`/eu/` page, which the refresh service decompresses) has its own per-IP zone too, `PLAIN_RATE_EVENTS`/`PLAIN_RATE_WINDOW` (default 60/1m), since the catalog is enumerable via the sitemap. All three routes share one Caddyfile snippet, `(api_limits)` (`import api_limits <zone> <events> <window>`), which applies the per-IP `rate_limit` zone plus a
  `request_body { max_size 16KB }` (reject oversized POSTs at the edge); add a new service route through it rather than copying the blocks. The two
  **detailed** stats endpoints (`/api/stats`, `/api/sem/stats`, which expose raw queue
  internals + host RAM) are **blocked** (`respond 404`, exact-path `handle` before the
  wildcards) so they are reachable only from inside the docker network, not publicly.
  The **public** `/status` page instead consumes the curated `/api/summary` +
  `/api/sem/summary` endpoints (progress %, counts, ETA, uptime; NO host internals),
  which fall through to the wildcard proxies and are rate-limited like any other API
  call (see the /status bullet in [release-notes-and-status.md](release-notes-and-status.md)). These edge limits COMPLEMENT
  the refresh service's global hourly scrape ceiling (`REFRESH_DEMAND_HOURLY_MAX`,
  which caps the aggregate) and its per-CIS min-interval floor; keep all three.
  The `order rate_limit before reverse_proxy` global option is required (the plugin
  directive is non-standard). `web.Dockerfile` pins both the Caddy release
  (`CADDY_VERSION`, 2.11.7) and the plugin (`caddy-ratelimit`, at a commit past v0.1.0
  for `ipv6_prefix 64`: every zone counts an IPv6 visitor per /64), so a rebuild is
  reproducible; bump them together on purpose. A web image predating that commit
  refuses the Caddyfile, so the first deploy after it needs `--rebuild-web`. The refresh/embed images pin their
  Python base (`python:3.14.9-slim`) and their direct pip deps the same way. **The `web` image is ONLY that binary**:
  the Caddyfile, `entrypoint.sh` and `../dist` are all bind mounts, so a Caddyfile
  change needs a `restart`/`up --force-recreate`, NOT a rebuild. Rebuild it (`up
  --build web`, or `deploy.sh --rebuild-web`) only after a `web.Dockerfile` change or
  to pick up a newer Caddy/plugin. That distinction matters because the xcaddy build
  is the ONE step needing outbound internet on the deploy host (Go module proxy), and
  it fails on a VPS whose Docker bridge advertises IPv6 without a route ("dial tcp
  [2a00:…]:443: network is unreachable"); rebuilding it on every deploy also re-ran
  silently whenever upstream published a new `caddy:2-builder-alpine` (cache bust).
  So `deploy.sh` builds `refresh embed` explicitly and leaves `web` alone by default;
  `web.Dockerfile`'s header documents the two escape hatches (`network: host` on the
  build, or build elsewhere and `docker save | ssh … docker load`). Keep the contract
  in sync across
  `docker/web.Dockerfile`, the global block + the `(api_limits)` snippet + the `/api/*`, `/api/sem/*` and `@br_fallback`
  `handle`s in `docker/Caddyfile`, the `web` `build:` in `docker/docker-compose.yml`,
  and the rate-limit knobs in `docker/env.example`.
- The container posture shared by all three services (`restart`, `env_file`, `read_only`, `cap_drop`, `security_opt`, `logging`) lives once in the `x-hardened` YAML anchor of `docker/docker-compose.yml`, merged into each with `<<: *hardened` (healthcheck cadence likewise via `x-healthcheck-timing`); a new service should merge it too. The container runs `read_only: true`, `cap_drop: ALL`,
  `no-new-privileges`, with tmpfs for Caddy's scratch dirs (`/tmp`, `/config`,
  `/data`, and `/gen` for the rendered `app-config.js`). Keep it that way; if
  Caddy needs a new writable path, add a tmpfs mount rather than dropping
  read-only.
- `cap_drop: ALL` is paired with `cap_add: [NET_BIND_SERVICE]`, and that one cap
  must stay. The Caddy binary (whether stock `caddy:2-alpine` or our xcaddy build,
  which is copied onto that same base) ships with `setcap
  cap_net_bind_service=+ep`; the effective bit makes `execve()` fail with EPERM
  ("Operation not permitted", crash loop at the `exec caddy` line of
  entrypoint.sh) if the cap is absent from the bounding set that `cap_drop: ALL`
  empties. We don't bind a privileged port (we listen on 8459), but the binary's
  file cap still has to be satisfiable at exec time. Do not remove it.
- A strict CSP (`default-src 'self'`, plus `form-action 'self'; object-src 'none'`, which do not inherit from it) and a `Permissions-Policy` denying every device API (camera, microphone, geolocation, ...: no page uses one) are set in the Caddyfile. The site uses no
  external fonts, scripts, or CDNs by design. The ONLY escape hatch is the umami
  origin: `entrypoint.sh` derives `ANALYTICS_ORIGIN` from `ANALYTICS_URL` and the
  Caddyfile adds it to `script-src`/`connect-src` (empty when analytics is off).
  Keep it self-contained otherwise so the CSP holds. Since per-drug semantic search
  moved server-side, the browser no longer instantiates any WebAssembly, so the old
  `'wasm-unsafe-eval'` relaxation is GONE and `script-src` is back to `'self'` (+ the
  optional umami origin); the query POST to `/api/sem/embed` is same-origin, covered
  by `connect-src 'self'`. The `/vendor` + `/models` immutable-cache matchers are also
  gone (the ~120 Mo model is no longer served to browsers, only mounted into the embed
  container), so ALL responses are plain `Cache-Control: no-cache`.
- **`docker/.env` is gitignored** (real analytics ids); `docker/env.example` is
  the committed template. Compose loads it via `env_file`; there is deliberately
  no `environment:` block (it would shadow `env_file` with empty defaults).
- `ANALYTICS_URL` must point at the umami **script** (`.../script.js`), not the
  instance base URL. `entrypoint.sh` validates this at startup (reachable AND
  serves JavaScript) and refuses to start otherwise, so a misconfig fails loud.
- **The refresh service is a second, separately-hardened container** (compose
  `refresh`, `docker/refresh.Dockerfile`), kept apart from `web` precisely so the
  web server can stay fully read-only. It is `read_only: true`, `cap_drop: ALL`,
  `no-new-privileges`, tmpfs `/tmp`, and is NOT published to the host (no `ports:`);
  it is only reachable through Caddy's `/api/*` proxy. Its writable mounts are the
  narrow paths it must write, in two matching triples (one per lane): the ANSM lane
  writes `data/rcp`, `dist/rcp`, and the `data/.scrape-manifest.json` file; the EMA
  `/eu/` lane writes `data/eu`, `dist/eu`, and the SEPARATE
  `data/.scrape-ema-manifest.json` file. Everything else is mounted read-only: the
  CIS->name map (`data/CIS_bdpm.txt`) plus the backlink-index inputs
  (`data/CIS_COMPO_bdpm.txt`, `data/CIS_GENER_bdpm.txt`, `data/drugs_frequency.jsonl`),
  which the EMA lane also reuses for its cap-meta + crawl ordering.
  Note the single-file mounts (both manifests, `data/CIS_bdpm.txt`,
  and those three backlink files) must exist as real files on the host before `up`,
  else Docker auto-creates a *directory* in their place. The manifest still crashes
  the service if it is a directory, but the CIS_bdpm/COMPO/GENER/frequency reads are
  now `is_file`-guarded (`load_names` tolerates it; `build_xref_index` /
  `bdpm.column_tokens` degrade to fewer/no backlinks) rather than raising
  IsADirectoryError. `deploy.sh` handles all of them (heals a stray directory, writes
  `{}` for both manifests, `mkdir`s `data/eu`, syncs `data/eu` overlays up/down like
  `data/rcp`, and rsyncs `CIS_bdpm.txt` + the three backlink files since
  the main rsync excludes `/data`). It runs as
  `${REFRESH_UID:-1000}:${REFRESH_GID:-1000}` so
  the overlays and rebuilt pages it writes stay owned by the host user (clean
  ownership + Syncthing). The manifest is bind-mounted as a single file, so it must
  exist on the host before first `up` (run `scrape-rcp.py` once, or
  `touch data/.scrape-manifest.json`), else Docker creates a *directory* in its
  place. `deploy.sh` heals this automatically (removes a stray directory and writes
  `{}` before `up`), and `scrape.load_manifest` now tolerates a directory / empty /
  invalid file (returns `{}`) so a misconfig degrades instead of crash-looping the
  service; but a manual `docker compose up` on a fresh host still needs the file to
  pre-exist. Because the container's `/app/data` is read-only except that one file,
  `scrape.save_manifest` writes it in place when its atomic temp+rename cannot work
  (a `.tmp` sibling is EROFS / renaming onto a bind-mount point is EBUSY); keep that
  fallback. Runtime knobs come from `docker/.env` (`env_file`) as `REFRESH_*` / `RCP_OVERLAY_GZIP`.
  The refresh AND embed images' build context is the repo ROOT (compose `context:
  ..`), so a root `.dockerignore` is REQUIRED to exclude `dist/` (~720M), `data/`
  (~260M) and the semantic-search `models/` (~120M, mounted read-only at runtime, NOT
  COPYd); without it every `up --build` ships ~1GB+ to the daemon and can fail the
  build on a small VPS ("no space left on device"), leaving no containers. The refresh
  Dockerfile bakes `src/build.py` / `src/scrape-rcp.py` / `src/scrape-ema.py` /
  `src/ema_pdf.py` / `src/refresh-service.py` / `src/bdpm.py` / `src/svc_http.py` / `src/rcp.html` (COPYd
  into `/app/src/`), and pip-installs `pymupdf` (fitz)
  alongside the other deps because `ema_pdf.py` needs it for the EMA PDF conversion.
- **The embed service is a THIRD, separately-hardened container** (compose `embed`,
  `docker/embed.Dockerfile`), same posture as `refresh` (`read_only: true`,
  `cap_drop: ALL`, `no-new-privileges`, tmpfs `/tmp`, no `ports:`, reached only via
  Caddy's `/api/sem/*` proxy) and run as `${EMBED_UID:-1000}:${EMBED_GID:-1000}` so
  the `.vec.json` it writes stay host-owned. Its ONLY writable mounts are `dist/rcp` +
  `dist/eu` (the sidecars); everything else is read-only: `models` (the encoder, so it
  can stay out of the image + `.dockerignore`-excluded build context) and the crawled
  overlays `data/rcp` + `data/eu` (the only text it embeds; the 2022 baseline CSV is
  not mounted at all). NO manifest mount: staleness is the content hash baked in each
  `.vec.json`. Because the embed service (not `build.py`) writes those sidecars ON THE
  VPS, they are runtime artifacts, NOT build outputs: a deploy must NOT mirror-delete
  them (`deploy.sh`'s main rsync `--exclude='*.vec.json*'` both skips shipping an
  empty/stale local copy AND, since rsync protects excluded paths from `--delete`, keeps
  a normal deploy from wiping the live index). To regenerate them on purpose after a
  segmentation change (see `_CHUNK_FORMAT_VERSION`) OR a model swap (see
  `onnx_embed.RUNTIME_MODEL`), there are TWO paths: `deploy.sh --re-embed` deletes the
  VPS sidecars so the embed service's reconcile sweep re-embeds every crawled page on the
  VPS (grinds through ~15k pages there), OR `deploy.sh --push-vectors` ships vectors
  pre-baked LOCALLY (`uv run src/embed-rcp.py`, same weights so identical vectors) UP to
  the VPS via a dedicated additive rsync (the model-gated reconcile self-heals any page
  you did not bake). Both are gitignored deploy.sh modes; either way, do NOT re-add
  `*.vec.json` to the rsync mirror. The Dockerfile bakes `src/build.py` / `src/bdpm.py` /
  `src/onnx_embed.py` / `src/embed-service.py` / `src/svc_http.py` / `src/rcp.html` (COPYd into `/app/src/`)
  and pip-installs `onnxruntime` + `tokenizers` (pinned: onnxruntime 1.31.0, whose int8 CPU vectors were
  bit-identical to 1.30.0's on 2026-10-22, so the bump re-embedded nothing; the
  PEP 723 headers of `embed-service.py`/`onnx_embed.py` pin the same version, and
  `embed-rcp.py`'s `onnxruntime-gpu` MUST match it, so pre-baked and served vectors
  come from the same runtime: bump all of them together)
  (NOT torch, so ~300 Mo not ~2 Go). It is fully optional: `up web refresh` omits it
  and the search box degrades to "indisponible". PRIVACY: it embeds the reader's query
  same-origin but logs NO query content (counts/latency only) and drops it right after
  encoding; the operator *could* in principle observe queries where the old browser
  design made that impossible, a deliberate trade for dropping the 120 Mo download.

- **Every container has a hard RAM ceiling and a kill priority** (`mem_limit` =
  `memswap_limit`, plus `oom_score_adj` in `docker/docker-compose.yml`): web 256m / -500,
  refresh 1536m / 500, embed 2g / 800, the sizes overridable as `WEB_MEM_LIMIT` /
  `REFRESH_MEM_LIMIT` / `EMBED_MEM_LIMIT` in `docker/.env` (compose interpolation, like
  `REFRESH_UID`). Why: the VPS has no swap, and when RAM ran out it did not OOM-kill
  anything, it thrashed (the kernel evicts executable pages, sshd included) until nothing
  answered. A container now dies inside its own cgroup and `restart:` brings it back.
  `oom_score_adj` is host-global, not relative to this compose file. Each also has a `pids_limit` (threads count: web 128, refresh 256, embed 256, sized for onnxruntime's per-core threads plus one thread per in-flight request) so a runaway cannot exhaust the host PID table. The embed service
  is the one that grows: onnxruntime's CPU arena kept every batch's peak forever (3.8 GB
  resident at batch 32 measured locally), so the service now runs with the arena OFF
  (`Encoder(cpu_arena=False)`, `EMBED_CPU_ARENA`, ~630 MB between pages) and a batch of 8
  (`EMBED_BATCH_SIZE`, peak ~1.05 GB against 2.3 GB at 32, ~25% slower page embedding).
  The offline `embed-rcp.py` keeps the arena (speed, and it is not long-lived). `batch_size` bounds the row count, not the cost: a batch is also capped at `onnx_embed.BATCH_CHAR_BUDGET` (rows x longest-row-chars squared, via `_batch_bounds`), because one page's 46 linearised table rows of ~3300 chars peaked at 3.6 GB in a batch of 8 and OOM-killed the embed container in a restart loop that embedded nothing for days (1.3 GB with the budget). The int8 graph quantises activations per batch, so the split moves those long rows' vectors slightly (cosine ~0.97); nothing re-embeds over it. Keep in
  sync across the three services in `docker/docker-compose.yml`, the memory section +
  the two embed knobs in `docker/env.example`, `cpu_arena` in `onnx_embed.Encoder`, and
  `--batch-size`/`--cpu-arena`/`BATCH_DEFAULT` in `embed-service.py`.
