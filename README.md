<p align="center">
  <picture>
    <img alt="stario-logo" src="https://raw.githubusercontent.com/bobowski/stario/main/docs/img/stario.png" style="height: 200px; width: auto;">
  </picture>
</p>

<p align="center">
  <em>Real-time hypermedia for Python 3.14+</em>
</p>

---

**Documentation**: [stario.dev](https://stario.dev) · **Source**: [github.com/bobowski/stario](https://github.com/bobowski/stario)

---

## What is Stario?

Stario is a Python web framework for **real-time hypermedia**. While most frameworks treat HTTP as request → response, Stario treats connections as ongoing conversations - open an SSE stream, push DOM patches, sync reactive signals.

## Why Stario?

- **Real-time first** - SSE streaming, DOM patching, reactive signals built-in
- **Hypermedia** - Native [Datastar](https://data-star.dev/) integration, no JavaScript frameworks needed
- **Simple** - Go-style handlers `(Context, Writer) → None`
- **Fast** - Built on `httptools` with zstd/brotli/gzip compression

## Get Started

Install with `uv add stario` or `pip install stario`, then run `stario init` to create a new project. Requires **Python 3.14+**.

See the [documentation](https://stario.dev) for tutorials, API reference, and how-to guides.

---

<p align="center"><em>Stario: Real-time hypermedia, made simple.</em></p>
