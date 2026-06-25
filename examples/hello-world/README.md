# Hello world

Minimal counter with Datastar signals — the smallest runnable Stario example.

For the full realtime pattern (SSE, Relay, multiplayer), start with [tiles](../tiles/) instead.

## Run

```bash
git clone https://github.com/bobowski/stario.git
cd stario/examples/hello-world
uv sync
uv run stario watch main:bootstrap
```

Open http://127.0.0.1:8000.
