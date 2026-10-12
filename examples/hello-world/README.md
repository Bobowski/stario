# Hello world

One route, one handler, one HTML page — the smallest runnable Stario app.

This example is Stario only. It does not load Datastar. For the live board
(SSE, Relay, multiplayer), start with [tiles](../tiles/).

## Run

```bash
git clone https://github.com/bobowski/stario.git
cd stario/examples/hello-world
uv sync
uv run python -m main
```

Open http://127.0.0.1:8000.
