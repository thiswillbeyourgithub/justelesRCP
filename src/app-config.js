// Runtime app configuration: the optional umami metrics tag plus the optional
// "work in progress" dev banner.
//
// Named generically on purpose: a file/path containing "analytics" (or "track",
// "stats", ...) gets blocked by many content filters and forward proxies, which
// would 404 it before it reaches the browser. Keeping the URL neutral lets the
// request through; app-init.js + dev-banner.js consume it.
//
// This committed copy is the LOCAL-DEV fallback. When the site is served
// straight from ./dist (uv run build.py, then any static server) there is no
// container environment to inject, so the feature fields stay empty and the
// umami tag + DEV banner stay off.
//
// In the CONTAINER this file is NOT served. docker/entrypoint.sh renders an
// equivalent file from the environment (ANALYTICS_* + DEV from docker/.env,
// STARTED_AT stamped at start) into a writable tmpfs at container start, and
// docker/Caddyfile serves THAT for /app-config.js. Rendering it once at startup
// (rather than templating on every request) means Caddy never parses this JS as
// a template. Keep the keys below in sync with the heredoc in
// docker/entrypoint.sh.
window.__APP_CONFIG__ = {
  url: '',
  websiteId: '',
  sri: '',
  dnt: '',
  dev: '',
  startedAt: '',
  sourceUrl: 'https://justelesrcp.example', // TODO: real public site / repo URL
};
