# Tiles

Collaborative painting board — the recommended **first Stario app**. Single-file realtime hypermedia: HTTP for the page and commands, SSE for live updates, in-process Relay between tabs.

## Run

```bash
git clone https://github.com/bobowski/stario.git
cd stario/examples/tiles
uv sync
uv run stario watch main:bootstrap
```

Open http://127.0.0.1:8000 — paint in two tabs to see live sync.

## Next steps

- [Realtime tiles tutorial](https://stario.dev/docs/tutorials/realtime-tiles) walks through the code
- [hello-world](../hello-world/) — smaller counter if you want less surface area first
- [chat-room](../chat-room/) — multi-file `app/features/*` layout when you outgrow one file
