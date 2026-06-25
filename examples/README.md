# Stario examples

Runnable reference apps for Stario 4. Clone the [stario repository](https://github.com/bobowski/stario) and run one locally, or copy an example directory into your own project. Each example pins `stario>=4,<5` in `pyproject.toml`. The CLI (`stario serve`, `stario watch`) only runs apps — it does not scaffold projects.

| Example | Size | Start here? |
|---------|------|-------------|
| **[tiles](tiles/)** | Single `main.py` | Yes — collaborative board, SSE + Relay |
| **[hello-world](hello-world/)** | Single `main.py` | Minimal counter |
| **[chat-room](chat-room/)** | Multi-file `app/features/*` | Larger layout with SQLite and tests |

## Quick start (tiles)

```bash
git clone https://github.com/bobowski/stario.git
cd stario/examples/tiles
uv sync
uv run stario watch main:bootstrap
```

Open http://127.0.0.1:8000 — paint in two tabs to see live sync.
