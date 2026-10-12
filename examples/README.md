# Stario examples

Runnable reference apps for Stario. Clone the [stario repository](https://github.com/bobowski/stario) and run one locally, or copy an example directory into your own project. Each example runs with `uv run python -m …` — `serve()` is the only server entry point.

| Example | Size | Start here? |
|---------|------|-------------|
| **[tiles](tiles/)** | Single `main.py` | Yes — collaborative board, SSE + Relay |
| **[hello-world](hello-world/)** | Single `main.py` | One route, one HTML page |
| **[jinja](jinja/)** | `main.py` + Jinja templates | Shared counter with live SSE updates |
| **[chat-room](chat-room/)** | Multi-file `app/features/*` | Larger layout with SQLite and tests |

## Quick start (tiles)

```bash
git clone https://github.com/bobowski/stario.git
cd stario/examples/tiles
uv sync
uv run python -m main
```

Open http://127.0.0.1:8000 — paint in two tabs to see live sync.
