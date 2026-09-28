import stario.responses as responses
from stario import App, Route, Span


async def plaintext(_c, w):
    responses.text(w, "Hello, World!")


async def bootstrap(app: App, span: Span):
    app.add(Route("GET /plaintext"), plaintext)
    yield
