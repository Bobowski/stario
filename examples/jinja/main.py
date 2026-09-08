"""
Shared counter with Jinja templates, Datastar, and SSE.

Run with: uv run stario watch main:bootstrap
      or: uv run stario serve main:bootstrap
"""

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

import stario.responses as responses
from stario import App, Context, Relay, Route, Span, Writer
from stario.datastar import DATASTAR_CDN_URL, SSE

HOME = Route.get("/")
SUBSCRIBE = Route.get("/subscribe")
INCREMENT = Route.post("/increment")
RESET = Route.post("/reset")

templates = Environment(
    loader=FileSystemLoader(Path(__file__).parent / "templates"),
    autoescape=True,
    undefined=StrictUndefined,
)


async def bootstrap(app: App, span: Span):
    span.attr("app.name", "jinja")
    count = 0
    relay = Relay[None]()

    def counter_view() -> str:
        return templates.get_template("counter.html.jinja").render(count=count)

    async def home(c: Context, w: Writer) -> None:
        page = templates.get_template("home.html.jinja").render(
            title="Shared counter",
            count=count,
            datastar_url=DATASTAR_CDN_URL,
            subscribe_url=SUBSCRIBE.href(),
            increment_url=INCREMENT.href(),
            reset_url=RESET.href(),
        )
        responses.html(w, page)

    async def subscribe(c: Context, w: Writer) -> None:
        # Subscribe before the first render so updates cannot fall into a gap.
        async with relay.subscribe("counter") as live:
            sse = SSE(w)
            sse.patch_elements(counter_view())
            async for _ in c.alive(live):
                sse.patch_elements(counter_view())

    async def increment(c: Context, w: Writer) -> None:
        nonlocal count
        count += 1
        relay.publish("counter", None)
        responses.empty(w)

    async def reset(c: Context, w: Writer) -> None:
        nonlocal count
        count = 0
        relay.publish("counter", None)
        responses.empty(w)

    app.add(HOME, home)
    app.add(SUBSCRIBE, subscribe)
    app.add(INCREMENT, increment)
    app.add(RESET, reset)
    yield
