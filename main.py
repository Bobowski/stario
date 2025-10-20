import asyncio
from typing import Annotated, Awaitable

from stario import Datastar, Stario
from stario.html import button, h1, p
from stario.logging import Logger
from stario.toys import toy_page

app = Stario()


@app.query("/")
async def home(ds: Datastar, log: Logger):
    log.info("Home called")
    return toy_page(
        h1("Home"),
        p("Welcome to the home page"),
        button(
            ds.fetch_on("click", "/action", method="post"),
            "Click me",
        ),
    )


# async def action(auth: Annotated[str, ParseHeader("datastar-request")]):
#     print(auth)
#     return None


async def dependency(logger: Logger):

    logger.info("Dependency called")

    await asyncio.sleep(1.5)

    logger.info("Dependency called after sleep")

    return "dependency"


@app.detached_command("/action")
async def action(
    logger: Logger, dependency: Annotated[Awaitable[str], dependency, "lazy"]
):
    logger.info("Action called, in backgrount logger?")

    dep_value = await dependency
    logger.info("Dependency resolved", dependency=dep_value)
    await asyncio.sleep(10)
    logger.info("Action called, in backgrount? after sleep")
    return None
