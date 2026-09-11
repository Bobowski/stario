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


class Counter:
    """In-memory counter state for this demo process."""

    def __init__(self, count: int = 0) -> None:
        self.count = count

    def increment(self) -> None:
        self.count += 1

    def reset(self) -> None:
        self.count = 0


def counter_view(counter: Counter) -> str:
    return templates.get_template("counter.html.jinja").render(count=counter.count)


def home(counter: Counter):
    async def handler(c: Context, w: Writer) -> None:
        page = templates.get_template("home.html.jinja").render(
            title="Shared counter",
            count=counter.count,
            datastar_url=DATASTAR_CDN_URL,
            subscribe_url=SUBSCRIBE.href(),
            increment_url=INCREMENT.href(),
            reset_url=RESET.href(),
        )
        responses.html(w, page)

    return handler


def subscribe(counter: Counter, relay: Relay[None]):
    async def handler(c: Context, w: Writer) -> None:
        # Subscribe before the first render so updates cannot fall into a gap.
        async with relay.subscribe("counter") as live:
            sse = SSE(w)
            sse.patch_elements(counter_view(counter))
            async for _ in c.alive(live):
                sse.patch_elements(counter_view(counter))

    return handler


def increment(counter: Counter, relay: Relay[None]):
    async def handler(c: Context, w: Writer) -> None:
        counter.increment()
        relay.publish("counter", None)
        responses.empty(w)

    return handler


def reset(counter: Counter, relay: Relay[None]):
    async def handler(c: Context, w: Writer) -> None:
        counter.reset()
        relay.publish("counter", None)
        responses.empty(w)

    return handler


async def bootstrap(app: App, span: Span):
    span.attr("app.name", "jinja")
    counter = Counter()
    relay = Relay[None]()

    app.add(HOME, home(counter))
    app.add(SUBSCRIBE, subscribe(counter, relay))
    app.add(INCREMENT, increment(counter, relay))
    app.add(RESET, reset(counter, relay))
    yield
