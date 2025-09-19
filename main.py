import asyncio
from typing import Annotated

from stario import Query, Stario
from stario.parameters import QueryParam
from stario.toys import ToyPage


async def home(q: Annotated[int, QueryParam()] = 1):
    return ToyPage(
        """
        <h2>Realtime responses!</h2>
        <div data-on-load="@get('/online-counter')">
            This shows how long the connection has been open.
        </div>
        <div id="online-counter"></div>
        """
    )


async def online_counter():
    duration = 0
    interval = 0.01

    while True:
        yield f"<div id='online-counter'>Online since: {duration:.2f}s</div>"
        duration += interval
        await asyncio.sleep(interval)


# url routes
app = Stario(
    Query("/", home),
    Query("/online-counter", online_counter),
)
