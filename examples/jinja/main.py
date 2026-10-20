"""
Shared counter with Jinja templates, Datastar, and SSE.

Run with: uv run python -m main
"""

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from stario import App, Context, Relay, Route, Span, Writer
from stario.datastar import DATASTAR_CDN_URL, SSE

HOME = Route("GET", "/")
LIVE = Route("GET", "/live")
INCREMENT = Route("POST", "/increment")
RESET = Route("POST", "/reset")

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
            subscribe_url=LIVE.href(),
            increment_url=INCREMENT.href(),
            reset_url=RESET.href(),
        )
        w.html(page)

    return handler


def live(counter: Counter, relay: Relay[None]):
    async def handler(c: Context, w: Writer) -> None:
        # Subscribe before the first render so updates cannot fall into a gap.
        async with relay.subscribe("counter") as events:
            sse = SSE(w)
            sse.patch_elements(counter_view(counter))
            async for _ in c.alive(events):
                sse.patch_elements(counter_view(counter))

    return handler


def increment(counter: Counter, relay: Relay[None]):
    async def handler(c: Context, w: Writer) -> None:
        counter.increment()
        relay.publish("counter", None)
        w.empty()

    return handler


def reset(counter: Counter, relay: Relay[None]):
    async def handler(c: Context, w: Writer) -> None:
        counter.reset()
        relay.publish("counter", None)
        w.empty()

    return handler


async def bootstrap(app: App, span: Span):
    span.attr("app.name", "jinja")
    counter = Counter()
    relay = Relay[None]()

    app.add(HOME, home(counter))
    app.add(LIVE, live(counter, relay))
    app.add(INCREMENT, increment(counter, relay))
    app.add(RESET, reset(counter, relay))
    yield


if __name__ == "__main__":
    import asyncio

    from stario import serve

    asyncio.run(serve(bootstrap))
