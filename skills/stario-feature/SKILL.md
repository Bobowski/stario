---
name: stario-feature
description: Add or remove one screen (page or resource) in a Stario app. Use when the user asks for a new page, a new route, a new section of the app, or asks to delete one. Walks the urls -> data -> views -> handlers -> main -> tests sequence from AGENTS.md.
---

# Add or remove a screen

Read `AGENTS.md` at the repo root first. This skill is the step list for one screen. The guide holds the rules.

## Before you start

Answer three questions:

1. What is the URL of the screen? One screen, one folder: `app/features/<screen>/`. The folder name is the noun on the URL: `/settings` → `settings`, `/rooms/{room_id}` → `room`.
2. Does the screen have an action? No action → static page, one `GET`. Any action → live screen with `GET`, `GET .../live`, and `POST` commands.
3. Does the screen own tables? Yes → `data.py`. No → read another feature's `data.py`.

## Add a static page

1. `mkdir app/features/<screen>` and `touch app/features/<screen>/__init__.py`. Then `urls.py`: one `Route("GET /path")`.
2. `app/features/<screen>/views.py`: `render(state) -> HtmlElement`, wrapped in `page(title, body)` from `app/common/shell.py`.
3. `app/features/<screen>/handlers.py`: one factory and `register_<screen>(app, ...)`.
4. `app/main.py`: call `register_<screen>(app, ...)` before `yield`.
5. `tests/test_<screen>.py`: `GET` the route, assert `200` and one string the page must show.
6. Run `uv run ruff check . && uv run pyright && uv run pytest`.

## Add a live screen

1. Create the folder with `__init__.py`. Then `urls.py`: the page route, the stream route, and one `POST` route per command.

   ```python
   SETTINGS = Route("GET /settings")
   SETTINGS_LIVE = Route("GET /settings/live")
   SET_THEME = Route("POST /settings/theme")
   ```

2. `data.py` when the screen owns tables: `SCHEMA` with `CREATE TABLE IF NOT EXISTS`, a frozen dataclass per row shape, and query functions named by verb (`list_*`, `get_*`, `add_*`, `set_*`, `remove_*`).

3. `views.py`: one `render(state)` that returns the full document. On the element that holds the content: `data.signals({...}, if_missing=True)` for the inputs, and `data.init(at.get(SETTINGS_LIVE.href(), retry="always"))`. Each command button: `data.on("click", at.post(SET_THEME.href()))`. Each input: `data.bind("name")`. Protect user-controlled UI state with `data.preserve_attr("open")`.

4. `handlers.py`: one class with `__init__(deps)`, `show`, `live`, one method per command, and `register(app)`.

   - `show`: load state, `responses.html(w, render(state))`.
   - `live`: `async with relay.subscribe(SUBJECT) as events:`, `sse = SSE(w)`, send `render(state)` once, then `async for _ in c.alive(events):` send `render(state)` again.
   - A command: `read_signals`, validate, `raise HttpException(400, ...)` when malformed, `responses.empty(w)`, change the store, `relay.publish(SUBJECT, "what")`.
   - A command that moves the user: `SSE(w).navigate(OTHER.href(...))` from an `at.post` action, or `responses.redirect(w, OTHER.href(...), 303)` from a plain form `POST`.

5. `app/main.py`: build the dependency if new (schema applied once), then `Screen(deps).register(app)`.

6. `tests/test_<screen>.py`: the page loads; one command repaints the stream; one malformed command returns `400`. Use the test shape from `AGENTS.md`.

7. Run `uv run ruff check . && uv run pyright && uv run pytest`.

## Link from another screen

Import the route: `from app.features.settings.urls import SETTINGS`. Use `SETTINGS.href()` in `h.A({"href": ...})`. Do not import the other screen's views.

## Read another screen's data

Import its `data.py`: `from app.features.room import data as room_data`. Call its `list_*` or `get_*` functions. Do not copy its queries. Do not write to its tables from your feature; call its `add_*` or `set_*` functions, or add one there.

## Remove a screen

1. Delete `app/features/<screen>/`.
2. Delete `tests/test_<screen>.py`.
3. Remove the `register` call from `app/main.py`.
4. Search for imports of the deleted `urls.py` and `data.py` and fix each caller.
5. If the screen owned tables, decide with the developer: drop them in a migration, or leave the data.
6. Run the checks. Pyright finds every dangling import.

## When the folder grows

A `handlers.py` over about 300 lines, or a `views.py` over about 400 lines, usually hides a second screen. Look for a second URL noun. Split by screen, not by layer. Do not create `views2.py` or `helpers.py`.
