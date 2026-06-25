"""CLI help text: epilogs and `STARIO_*` environment variable reference."""

SERVER_ENV_EPILOG = """\
Server environment variables (Stario does not load .env files; use your shell or tooling):

Listen:
  STARIO_HOST=127.0.0.1
  STARIO_PORT=8000
  STARIO_LOOP=asyncio|uvloop
  STARIO_UNIX_SOCKET=          (empty = TCP)
  STARIO_UNIX_SOCKET_MODE=660  (octal; after bind on unix socket)
  STARIO_BACKLOG=2048
  STARIO_REUSE_ADDR=1          (TCP only; set 0 to disable SO_REUSEADDR)
  STARIO_GRACEFUL_SHUTDOWN_TIMEOUT=5

Telemetry (STARIO_TRACER plus json/sqlite backend tuning):
  STARIO_TRACER=auto|tty|json|noop|sqlite|module:callable
    auto — tty when stdout is a TTY, otherwise json
  STARIO_TRACERS_JSON_FLUSH_INTERVAL=0.125
  STARIO_TRACERS_JSON_MAX_PENDING_SPANS=...
  STARIO_TRACERS_JSON_MAX_BATCH_SPANS=...
  STARIO_TRACERS_SQLITE=stario-traces.sqlite3
  STARIO_TRACERS_SQLITE_FLUSH_INTERVAL=...
  STARIO_TRACERS_SQLITE_MAX_PENDING_SPANS=...
  STARIO_TRACERS_SQLITE_MAX_BATCH_SPANS=...

Request limits:
  STARIO_REQUESTS_MAX_HEADER_BYTES=65536
  STARIO_REQUESTS_MAX_BODY_BYTES=10485760
  STARIO_REQUESTS_HEADER_TIMEOUT=5
  STARIO_REQUESTS_BODY_TIMEOUT=30
  STARIO_REQUESTS_KEEP_ALIVE_TIMEOUT=5
  STARIO_REQUESTS_MAX_PIPELINED_REQUESTS=8

Compression:
  STARIO_COMPRESS_MIN_SIZE=512
  STARIO_COMPRESS_ZSTD_LEVEL=3
  STARIO_COMPRESS_ZSTD_WINDOW_LOG=   (optional)
  STARIO_COMPRESS_BROTLI_LEVEL=4
  STARIO_COMPRESS_BROTLI_WINDOW_LOG= (optional)
  STARIO_COMPRESS_GZIP_LEVEL=6
  STARIO_COMPRESS_GZIP_WINDOW_BITS=  (optional)

Examples:
  STARIO_PORT=9000 STARIO_TRACER=json stario serve main:bootstrap
  STARIO_LOOP=uvloop stario watch main:bootstrap --watch app/
"""

_ROOT_EXAMPLES = """\
Examples:
  stario serve main:bootstrap
  stario watch main:bootstrap
  STARIO_TRACER=json stario watch main:bootstrap --watch app/
  STARIO_PORT=9000 stario serve app.main:bootstrap

Example apps: https://github.com/bobowski/stario/tree/main/examples"""

_SERVE_EXAMPLES = """\
Examples:
  stario serve main:bootstrap
  STARIO_TRACER=noop stario serve main:bootstrap
  STARIO_PORT=9000 stario serve app.main:bootstrap"""

_WATCH_EXAMPLES = """\
Examples:
  stario watch main:bootstrap
  STARIO_TRACER=json stario watch main:bootstrap
  stario watch main:bootstrap --watch app/
  stario watch main:bootstrap --watch main.py
  stario watch main:bootstrap --watch app/ --watch-ignore data/
  stario watch main:bootstrap --watch-ignore '*.db'"""

ROOT_EPILOG = f"{_ROOT_EXAMPLES}\n\n{SERVER_ENV_EPILOG}"
SERVE_EPILOG = f"{_SERVE_EXAMPLES}\n\n{SERVER_ENV_EPILOG}"
WATCH_EPILOG = f"{_WATCH_EXAMPLES}\n\n{SERVER_ENV_EPILOG}"
