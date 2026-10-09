---
name: stario-tailwind-daisyui
description: Style a Stario app with Tailwind CSS v4 and DaisyUI through the standalone tailwindcss binary, with no Node. Use when the user mentions Tailwind, DaisyUI, utility classes, themes, or wants the app to look finished without writing CSS.
---

# Tailwind and DaisyUI without Node

Tailwind compiles `frontend/src/input.css` into `static/css/output.css`. The standalone binary does the work. `npm`, `npx`, and `@tailwindcss/cli` are not used. DaisyUI gives component classes (`btn`, `card`, `modal`, `drawer`) on top of Tailwind, so most screens need no custom CSS.

## Install once

```bash
mkdir -p frontend/src static/css
# Tailwind standalone CLI: pick the asset for your OS from
# https://github.com/tailwindlabs/tailwindcss/releases/latest
curl -fL -o tailwindcss https://github.com/tailwindlabs/tailwindcss/releases/latest/download/tailwindcss-macos-arm64
chmod +x tailwindcss
# DaisyUI bundles for the standalone CLI
curl -fL -o frontend/daisyui.mjs https://github.com/saadeghi/daisyui/releases/latest/download/daisyui.mjs
curl -fL -o frontend/daisyui-theme.mjs https://github.com/saadeghi/daisyui/releases/latest/download/daisyui-theme.mjs
```

Linux: `tailwindcss-linux-x64` or `tailwindcss-linux-arm64`. Intel Mac: `tailwindcss-macos-x64`. Add `tailwindcss` to `.gitignore`. Commit the two `.mjs` files and `static/css/output.css`.

## `frontend/src/input.css`

```css
@import "tailwindcss";

@source not "../../tailwindcss";
@source not "../daisyui{,*}.mjs";
@source "../../app/**/*.py";

@plugin "../daisyui.mjs" {
  themes: light --default, dark --prefersdark;
}
```

`@source "../../app/**/*.py"` tells Tailwind to scan the Python views for class names. A class that is not written as a literal in a `.py` file is not generated. Build class lists with `classes("btn", "btn-primary")`, not with string concatenation.

A custom theme:

```css
@plugin "../daisyui.mjs" {
  themes: false;
}

@plugin "../daisyui-theme.mjs" {
  name: "app";
  default: true;
  color-scheme: light;
  --color-base-100: #f7f6f1;
  --color-base-content: #1a1f24;
  --color-primary: #3d4a6b;
  --color-primary-content: #f7f6f1;
}
```

## Build

```bash
./tailwindcss -i frontend/src/input.css -o static/css/output.css --watch   # dev
./tailwindcss -i frontend/src/input.css -o static/css/output.css --minify  # before commit
```

Run it next to `uv run stario watch app.main:bootstrap`. `Assets` fingerprints `output.css`, so `STYLE_CSS = ASSETS.href("css/output.css")` in `app/assets.py`.

## The shell

```python
@baked
def page(title: str, body: HtmlElement) -> HtmlElement:
    return h.HtmlDocument(
        {"lang": "en", "data-theme": "light", "data-preserve-attr": "data-theme"},
        h.Head(
            h.Meta({"charset": "utf-8"}),
            h.Meta({"name": "viewport", "content": "width=device-width, initial-scale=1"}),
            h.Title(title),
            h.Link({"rel": "stylesheet", "href": STYLE_CSS}),
            ModuleScript(DATASTAR_JS),
        ),
        h.Body(classes("min-h-screen", "bg-base-100", "text-base-content"), body),
    )
```

`data-theme` on `<html>` selects the DaisyUI theme. `data-preserve-attr="data-theme"` keeps a theme the user switched when the stream morphs the document.

## Classes in views

```python
h.Button(classes("btn", "btn-primary"), data.on("click", at.post(SAVE.href())), "Save")
h.Input(classes("input", "input-bordered", "w-full"), {"type": "text"}, data.bind("title"))
h.Div(classes("card", "bg-base-200", "shadow"), h.Div(classes("card-body"), ...))
```

Rules:

- DaisyUI component class first, Tailwind utilities after. `btn btn-primary w-full`.
- Theme colors only: `bg-base-100`, `bg-base-200`, `text-base-content`, `bg-primary`, `text-primary-content`. No raw `bg-gray-100`. The theme stays switchable.
- Layout with `flex`, `grid`, `gap-*`, `p-*`. No custom CSS for layout.
- Custom CSS goes into `input.css` under `@layer components` only when a DaisyUI class does not exist. Name it after the app: `.app-stage`.
- A modal is `<dialog class="modal">` with `data.preserve_attr("open")`. The server never renders it open.
- A drawer on wide screens: `drawer lg:drawer-open`. The toggle is a checkbox the client owns; add `data.ignore_morph()` to the checkbox or `data.preserve_attr("checked")`.

## Checks

The page loads a stylesheet: `assert 'css/output' in r.text` in the page test. When a class does not apply in the browser, check that the literal appears in a `.py` file under `app/`, then rebuild.
